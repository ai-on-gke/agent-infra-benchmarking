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

import base64
import json
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from string import Template
from typing import Callable, Iterator

from . import RUNNER_DIR, images, reports, run_info, worker_pool, workloads
from .cluster import Cluster
from .log import ON_CLOUD_RUN, log
from .process import stream

MANIFESTS_DIR = RUNNER_DIR.parent / "locust" / "manifests"
# Have to match metadata.name and namespace in the manifests.
MASTER_JOB = "locust-master"
WORKER_JOB = "boomer-worker"
NAMESPACE = "benchmarking"
MASTER_HOST = f"{MASTER_JOB}.{NAMESPACE}.svc.cluster.local"
USER_CLASS = "runworkload"
POD_START_TIMEOUT_SECS = 300
WAIT_TIMEOUT_SECS = 3600
# How long a stopped run's master gets to stop the worker, write its CSVs and
# upload them: its terminationGracePeriodSeconds and a margin.
STOP_TIMEOUT_SECS = 150
# The share of requests, 0 to 1, a case may fail before its run counts as
# failed, unless it states its own `max-failure-ratio`. With
# --exit-code-on-error 0 one failed request no longer fails the run; this does.
DEFAULT_MAX_FAILURE_RATIO = 0.01
# The worker's --atespace default, which its actors are created in unless the
# case's `worker` sets `atespace`.
ACTOR_ATESPACE = "benchmark"


class LocustError(RuntimeError):
    pass


class LocustTimeout(LocustError):
    """The run outlived WAIT_TIMEOUT_SECS and is still going. On a dev infra
    the next run would recreate the pool under it, so a series stops here."""


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
    if not reports.LABEL_RE.fullmatch(name):
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
    ratio = case.get("max-failure-ratio", DEFAULT_MAX_FAILURE_RATIO)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
        raise ValueError(f"case {name}'s `max-failure-ratio` must be a number from 0 to 1, got {ratio!r}")
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
        "max-failure-ratio": DEFAULT_MAX_FAILURE_RATIO,
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
        # run_master.py judges the run by its max-failure-ratio instead.
        "--exit-code-on-error",
        "0",
        *flags(case["master"]),
    ]


def worker_args(case: dict) -> list[str]:
    return [f"--master-host={MASTER_HOST}", f"--user-class={USER_CLASS}", *flags(case["worker"])]


def apply(cluster: Cluster, manifest: str) -> None:
    # Without --namespace, which kubectl would refuse worker-job.yaml's
    # RoleBinding in ate-system over.
    stream(cluster.kubectl("apply", "-f", "-"), input=manifest)


def render(manifest: str, values: dict[str, str]) -> str:
    return Template((MANIFESTS_DIR / manifest).read_text()).substitute(values)


def job_status(cluster: Cluster, job: str) -> tuple[int, int]:
    """The Job's succeeded and failed pod counts; none for one not found."""
    proc = subprocess.run(
        cluster.kubectl("get", "job", job, "-o=jsonpath={.status.succeeded}|{.status.failed}", namespace=NAMESPACE),
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
    ready = cluster.kubectl(
        "get",
        "pods",
        f"--selector=job-name={MASTER_JOB}",
        '-o=jsonpath={.items[0].status.conditions[?(@.type=="Ready")].status}',
        namespace=NAMESPACE,
    )
    while time.monotonic() < deadline:
        if subprocess.run(ready, capture_output=True, text=True).stdout == "True":
            return
        if any(job_status(cluster, MASTER_JOB)):
            raise LocustError(f"{MASTER_JOB} ended before it was ready: kubectl logs job/{MASTER_JOB} -n {NAMESPACE}")
        time.sleep(2)
    try:
        stream(cluster.kubectl("describe", "job", MASTER_JOB, namespace=NAMESPACE))
    except subprocess.CalledProcessError:
        pass
    raise LocustError(f"the {MASTER_JOB} pod was not ready within {POD_START_TIMEOUT_SECS}s")


@contextmanager
def master_logs(cluster: Cluster) -> Iterator[Callable[[int], None]]:
    """Logs the master's output as it comes, and yields what stops it, after
    giving it up to that many seconds to print its last lines and end."""
    follow = cluster.kubectl("logs", "--follow", f"job/{MASTER_JOB}", namespace=NAMESPACE)
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
    raise LocustTimeout(f"timed out after {WAIT_TIMEOUT_SECS}s; the Jobs are left running")


def stop_run(cluster: Cluster) -> bool:
    """Stops a run the scenario-runner was interrupted in, and returns whether
    both Jobs are gone. Deleting the master Job sends its pod SIGTERM:
    run_master.py stops locust, which stops the worker and has it delete its
    actors, then writes and uploads the reports before the pod goes. The worker
    Job ends once its master is gone."""
    log(f"locust: interrupted; stopping {MASTER_JOB} so it can upload what it recorded")
    delete = ["delete", "job", MASTER_JOB, "--ignore-not-found", "--cascade=foreground", "--wait",
              f"--timeout={STOP_TIMEOUT_SECS}s"]  # fmt: skip
    stopped = True
    for cmd in (delete, [*delete[:2], WORKER_JOB, *delete[3:]]):
        proc = subprocess.run(cluster.kubectl(*cmd, namespace=NAMESPACE), capture_output=True, text=True)
        if proc.returncode:
            stopped = False
            log(f"locust: {' '.join(cmd[:3])} failed: {proc.stderr.strip()}", "WARNING")
    return stopped


def run(
    cluster: Cluster,
    name: str,
    case: dict,
    run_id: str | None = None,
    build: bool = True,
    deploy_workloads: bool = True,
    cloud_build: bool = False,
    docker_login: bool = True,
    report_bucket: str | None = None,
    report_prefix: str = reports.REPORT_PREFIX,
    test_case: str | None = None,
    repeat: int = 0,
) -> None:
    """Runs the case, recorded under name, as part of run_id, by default a UTC
    timestamp and the commit. test_case and repeat say which case it is a
    repeat of; repeat is 0 for a case run once. Without build, uses the images
    already pushed; without deploy_workloads, the mock-llm and ActorTemplates
    already deployed. The worker pool is recreated either way. Without
    docker_login, a local build uses the registry credentials Docker already
    has. The reports go under report_prefix in report_bucket, by default the
    cluster's own. Expects the cluster's credentials in its kubeconfig."""
    case = check_case(name, case, deploy_workloads)
    # The workers would never schedule past the sandboxed pool's taint.
    if case["sandbox-class"] != cluster.sandbox_class:
        raise LocustError(
            f"case {name} is {case['sandbox-class']}, but cluster {cluster.name}'s sandboxed pool is"
            f" {cluster.sandbox_class}; set the cluster's sandbox_class to match"
        )
    run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dt%H%M%Sz}-{images.tag()}"
    if not reports.LABEL_RE.fullmatch(run_id):
        raise ValueError(f"run id {run_id!r} is not a valid label value")
    bucket = report_bucket or cluster.bucket
    # Before the build, which is the slow part.
    reports.refuse_reused(bucket, report_prefix, run_id, name)

    if build:
        # The workload images are only read when the workloads are deployed.
        files = [images.LOCUST_IMAGES] + ([images.WORKLOADS_IMAGES] if deploy_workloads else [])
        images.build_and_push(cluster, files, cloud_build=cloud_build, login=docker_login)
    # By digest, so the Jobs run exactly the images built from this commit even
    # if the tag is pushed again meanwhile; run.json records them.
    image_refs = {
        image: images.pin(images.image_ref(images.LOCUST_IMAGES, cluster.docker_repo, image))
        for image in ("locust-master", "boomer-worker")
    }

    apply(cluster, f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {NAMESPACE}\n")
    # A Job's pod template is immutable. Foreground, so their pods are gone too
    # and the job-name lookup only sees this run's.
    log(f"locust: deleting any previous {MASTER_JOB} and {WORKER_JOB}")
    delete = ["delete", "job", MASTER_JOB, WORKER_JOB, "--ignore-not-found", "--cascade=foreground", "--wait"]
    stream(cluster.kubectl(*delete, namespace=NAMESPACE))

    worker_pool.deploy(cluster, case["worker-count"], case["sandbox-class"])
    template_name = case["worker"].get("workload-template") or case["worker"].get("workload-name")
    workload = case["worker"].get("workload-name") or template_name
    # The golden snapshot is taken on the pool just deployed.
    if deploy_workloads:
        workloads.deploy(cluster, [template_name], case["sandbox-class"])
    template = None
    if template_name:
        template = workloads.deployed_template(cluster, template_name)
        workloads.check_deployed(template, template_name, case["sandbox-class"], workload)
        # A case may state its own allow-list; otherwise the actors may call
        # what their template is configured to, and nothing else.
        if "actor-egress-allow" not in case["worker"]:
            hosts = workloads.egress_hosts(template)
            if hosts:
                case = {**case, "worker": {**case["worker"], "actor-egress-allow": ",".join(hosts)}}
        log(f"locust: actors may connect to {case['worker'].get('actor-egress-allow') or 'nothing'}")

    info = run_info.collect(cluster, case, template_name, template, image_refs, bucket)
    values = {
        "LOCUST_IMAGE": image_refs["locust-master"],
        "BOOMER_IMAGE": image_refs["boomer-worker"],
        "BENCHMARK_RUNNER_KSA": cluster.runner_ksa,
        "BENCHMARK_RUN_ID": run_id,
        "BENCHMARK_RECORD_NAME": name,
        "BENCHMARK_CASE": test_case or name,
        "BENCHMARK_REPEAT": str(repeat),
        "BENCHMARK_ONLY_SUMMARY": "false",
        "MAX_FAILURE_RATIO": str(case["max-failure-ratio"]),
        # What the master writes into run.json; base64, as it is JSON in YAML.
        "BENCHMARK_RUN_INFO": base64.b64encode(json.dumps(info).encode()).decode(),
        # Where the master uploads its CSVs once it exits.
        "BENCHMARK_REPORT_BUCKET": bucket,
        "BENCHMARK_REPORT_PREFIX": report_prefix,
        # JSON is a YAML flow sequence.
        "LOCUST_MASTER_ARGS": json.dumps(master_args(case)),
        "BOOMER_WORKER_ARGS": json.dumps(worker_args(case)),
    }
    log(
        f"locust: running {name} (run {run_id}):\n"
        f"  master: {values['LOCUST_IMAGE']} {values['LOCUST_MASTER_ARGS']}\n"
        f"  worker: {values['BOOMER_IMAGE']} {values['BOOMER_WORKER_ARGS']}\n"
        f"  reports: gs://{bucket}/{report_prefix}/{run_id}/{name}/"
    )
    apply(cluster, render("master-job.yaml", values))
    # The actors are the run's to delete while it is still going.
    still_running = False
    try:
        wait_master_ready(cluster)
        with master_logs(cluster) as stop_logs:
            apply(cluster, render("worker-job.yaml", values))
            wait_for_jobs(cluster, stop_logs)
    except LocustTimeout:
        still_running = True
        raise
    except (KeyboardInterrupt, SystemExit):
        # Ctrl-C, or SIGTERM through cli.run's handler: stop the run so it
        # uploads what it recorded before the cluster goes. Once both Jobs
        # are gone, the actors they left are cleaned up as after any run.
        still_running = not stop_run(cluster)
        raise
    finally:
        if not still_running:
            atespace = case["worker"].get("atespace") or ACTOR_ATESPACE
            reports.record_cleanup(cluster, atespace, bucket, report_prefix, run_id, name)
        reports.upload_logs(cluster, NAMESPACE, (MASTER_JOB, WORKER_JOB), bucket, report_prefix, run_id, name)
        # A failed run's reports are kept too. On Cloud Run they'd go with the
        # container, so they stay only in the bucket.
        if ON_CLOUD_RUN:
            log(f"locust: on Cloud Run, so the reports stay only in gs://{bucket}/{report_prefix}/{run_id}/{name}/")
        else:
            reports.download_reports(bucket, report_prefix, run_id, name)
