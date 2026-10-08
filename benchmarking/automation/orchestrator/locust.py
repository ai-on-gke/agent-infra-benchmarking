# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Runs a case of type locust as the locust master and boomer worker Jobs of
locust/manifests/, on a worker pool and workload deployed for it, and waits
for both. Every key of the case's `master` and `worker` becomes `--key=value`
on that process, or `--key` for true."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from string import Template
from typing import Callable, Iterator

from . import AUTOMATION_DIR, images, worker_pool, workloads
from .cluster import Cluster
from .log import log
from .process import stream

MANIFESTS_DIR = AUTOMATION_DIR.parent / "locust" / "manifests"
# Have to match metadata.name and namespace in the manifests.
MASTER_JOB = "locust-master"
WORKER_JOB = "boomer-worker"
NAMESPACE = "benchmarking"
MASTER_HOST = f"{MASTER_JOB}.{NAMESPACE}.svc.cluster.local"
USER_CLASS = "runworkload"
# Created by infra/clusters/common, and named in the bucket's grant.
RUNNER_KSA = "benchmark-runner"
POD_START_TIMEOUT_SECS = 300
WAIT_TIMEOUT_SECS = 3600
# The run id and case name end up as label values.
LABEL_RE = re.compile(r"[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?")


class LocustError(RuntimeError):
    pass


def missing_requirements(cloud_build: bool = False) -> list[str]:
    missing = images.missing_requirements(cloud_build) + worker_pool.missing_requirements()
    return list(dict.fromkeys(missing + workloads.missing_requirements()))


def flag_value(value: object) -> str:
    # As jq's string interpolation spells it: false, null, compact JSON.
    return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))


def flags(values: dict) -> list[str]:
    return [f"--{key}" if value is True else f"--{key}={flag_value(value)}" for key, value in values.items()]


def check_case(name: str, case: dict, deploy_workloads: bool) -> dict:
    """The case with its defaults filled in."""
    if not LABEL_RE.fullmatch(name):
        raise ValueError(f"case name {name!r} is not a valid label value")
    master, worker = case.get("master"), case.get("worker")
    if worker is None:
        worker = {}
    if not isinstance(master, dict) or not isinstance(worker, dict):
        raise ValueError(f"case {name}'s `master` and `worker` must be mappings of flags")
    if "run-time" not in master or "users" not in master:
        raise ValueError(f"case {name}'s `master` must state `run-time` and `users`")
    count = case.get("worker-count", 1)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError(f"case {name}'s `worker-count` must be a whole number, got {count!r}")
    if case.get("sandbox-class", "gvisor") not in workloads.SANDBOX_CONFIGS:
        raise ValueError(f"case {name}'s `sandbox-class` must be one of {', '.join(workloads.SANDBOX_CONFIGS)}")
    if deploy_workloads:
        template = worker.get("workload-template") or worker.get("workload-name")
        if not template:
            raise ValueError(f"case {name}'s `worker` names no `workload-template` or `workload-name` to deploy")
        workloads.check_templates([template])
    # spawn-rate defaults to the user count, putting the whole load on at once.
    return {
        "worker-count": 1,
        "sandbox-class": "gvisor",
        **case,
        "master": {**master, "spawn-rate": master.get("spawn-rate", master["users"])},
        "worker": worker,
    }


def master_args(case: dict) -> list[str]:
    return [
        "-f",
        f"/app/tests/{USER_CLASS}.py",
        "--headless",
        "--master",
        "--expect-workers",
        "1",
        "--expect-workers-max-wait",
        str(POD_START_TIMEOUT_SECS),
        *flags(case["master"]),
    ]


def worker_args(case: dict) -> list[str]:
    return [f"--master-host={MASTER_HOST}", f"--user-class={USER_CLASS}", *flags(case["worker"])]


def kubectl(cluster: Cluster, *args: str) -> list[str]:
    return workloads.kubectl(cluster, *args, namespace=NAMESPACE)


def apply(cluster: Cluster, manifest: str) -> None:
    # Without --namespace, which kubectl would refuse worker-job.yaml's
    # RoleBinding in ate-system over.
    stream(["kubectl", "apply", "-f", "-", f"--kubeconfig={cluster.kubeconfig}"], input=manifest)


def render(manifest: str, values: dict[str, str]) -> str:
    return Template((MANIFESTS_DIR / manifest).read_text()).substitute(values)


def job_status(cluster: Cluster, job: str) -> tuple[int, int]:
    """The Job's succeeded and failed pod counts; none for one not found."""
    proc = subprocess.run(
        kubectl(cluster, "get", "job", job, "-o=jsonpath={.status.succeeded}|{.status.failed}"),
        capture_output=True,
        text=True,
    )
    succeeded, _, failed = proc.stdout.partition("|") if proc.returncode == 0 else ("", "", "")
    return int(succeeded or 0), int(failed or 0)


def wait_master_ready(cluster: Cluster) -> None:
    """Boomer tries the master once at startup and doesn't retry, so the worker
    goes in only once this returns."""
    log(f"locust: waiting for the {MASTER_JOB} pod to be ready")
    deadline = time.monotonic() + POD_START_TIMEOUT_SECS
    ready = kubectl(
        cluster,
        "get",
        "pods",
        f"--selector=job-name={MASTER_JOB}",
        '-o=jsonpath={.items[0].status.conditions[?(@.type=="Ready")].status}',
    )
    while time.monotonic() < deadline:
        if subprocess.run(ready, capture_output=True, text=True).stdout == "True":
            return
        if any(job_status(cluster, MASTER_JOB)):
            raise LocustError(f"{MASTER_JOB} ended before it was ready: kubectl logs job/{MASTER_JOB} -n {NAMESPACE}")
        time.sleep(2)
    try:
        stream(kubectl(cluster, "describe", "job", MASTER_JOB))
    except subprocess.CalledProcessError:
        pass
    raise LocustError(f"the {MASTER_JOB} pod was not ready within {POD_START_TIMEOUT_SECS}s")


@contextmanager
def master_logs(cluster: Cluster) -> Iterator[Callable[[int], None]]:
    """Logs the master's output as it comes, and yields what stops it, after
    giving it up to that many seconds to print its last lines and end."""
    follow = kubectl(cluster, "logs", "--follow", f"job/{MASTER_JOB}")
    proc = subprocess.Popen(follow, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    reader = threading.Thread(target=lambda: [log(line.rstrip("\n")) for line in proc.stdout], daemon=True)
    reader.start()

    def stop(grace: int = 0) -> None:
        try:
            proc.wait(grace)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
        reader.join()

    try:
        yield stop
    finally:
        stop()


def wait_for_jobs(cluster: Cluster, stop_logs: Callable[[int], None]) -> None:
    """Waits for both Jobs to end rather than the first to fail, as each one's
    log is part of the run."""
    log("locust: waiting for the run to finish")
    deadline = time.monotonic() + WAIT_TIMEOUT_SECS
    while time.monotonic() < deadline:
        statuses = {job: job_status(cluster, job) for job in (MASTER_JOB, WORKER_JOB)}
        if all(any(status) for status in statuses.values()):
            stop_logs(10)
            failed = [job for job, (succeeded, _) in statuses.items() if not succeeded]
            if failed:
                raise LocustError(
                    f"{' and '.join(failed)} failed: "
                    + "; ".join(f"kubectl logs job/{job} -n {NAMESPACE}" for job in failed)
                )
            log(f"locust: {MASTER_JOB} and {WORKER_JOB} completed: kubectl logs job/{MASTER_JOB} -n {NAMESPACE}")
            return
        time.sleep(5)
    raise LocustError(f"timed out after {WAIT_TIMEOUT_SECS}s; the Jobs are left running")


def run(
    cluster: Cluster,
    name: str,
    case: dict,
    run_id: str | None = None,
    build: bool = True,
    deploy_workloads: bool = True,
    cloud_build: bool = False,
) -> None:
    """Runs the case, recorded under name, as part of run_id, by default a UTC
    timestamp and the commit. Without build, uses the images already pushed;
    without deploy_workloads, the mock-llm and ActorTemplates already
    deployed. The worker pool is recreated either way. Expects the cluster's
    credentials in its kubeconfig."""
    case = check_case(name, case, deploy_workloads)
    run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dt%H%M%Sz}-{images.tag()}"
    if not LABEL_RE.fullmatch(run_id):
        raise ValueError(f"run id {run_id!r} is not a valid label value")
    # The images the manifests name, with or without a build.
    locust_image = images.image_ref(images.LOCUST_IMAGES, cluster.docker_repo, "locust-master")
    boomer_image = images.image_ref(images.LOCUST_IMAGES, cluster.docker_repo, "boomer-worker")

    if build:
        # The workload images are only read when the workloads are deployed.
        files = [images.LOCUST_IMAGES] + ([images.WORKLOADS_IMAGES] if deploy_workloads else [])
        images.build_and_push(cluster, files, cloud_build=cloud_build)

    apply(cluster, f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {NAMESPACE}\n")
    # A Job's pod template is immutable. Foreground, so their pods are gone too
    # and the job-name lookup only sees this run's.
    log(f"locust: deleting any previous {MASTER_JOB} and {WORKER_JOB}")
    jobs = ["job", MASTER_JOB, WORKER_JOB]
    stream(kubectl(cluster, "delete", *jobs, "--ignore-not-found", "--cascade=foreground", "--wait"))

    worker_pool.deploy(cluster, case["worker-count"], case["sandbox-class"])
    # The golden snapshot is taken on the pool just deployed.
    if deploy_workloads:
        template = case["worker"].get("workload-template") or case["worker"]["workload-name"]
        workloads.deploy(cluster, [template], case["sandbox-class"])

    values = {
        "LOCUST_IMAGE": locust_image,
        "BOOMER_IMAGE": boomer_image,
        "BENCHMARK_RUNNER_KSA": RUNNER_KSA,
        "BENCHMARK_RUN_ID": run_id,
        "BENCHMARK_RECORD_NAME": name,
        "BENCHMARK_ONLY_SUMMARY": "false",
        # Where the master uploads its CSVs once it exits.
        "BENCHMARK_REPORT_BUCKET": cluster.bucket,
        # JSON is a YAML flow sequence.
        "LOCUST_MASTER_ARGS": json.dumps(master_args(case)),
        "BOOMER_WORKER_ARGS": json.dumps(worker_args(case)),
    }
    log(
        f"locust: running {name} (run {run_id}):\n"
        f"  master: {values['LOCUST_IMAGE']} {values['LOCUST_MASTER_ARGS']}\n"
        f"  worker: {values['BOOMER_IMAGE']} {values['BOOMER_WORKER_ARGS']}\n"
        f"  reports: gs://{cluster.bucket}/reports/{run_id}/{name}/"
    )
    apply(cluster, render("master-job.yaml", values))
    wait_master_ready(cluster)
    with master_logs(cluster) as stop_logs:
        apply(cluster, render("worker-job.yaml", values))
        wait_for_jobs(cluster, stop_logs)
