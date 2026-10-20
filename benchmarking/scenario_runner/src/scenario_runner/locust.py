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
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from string import Template
from typing import Callable, Iterator

from google.cloud import storage

from . import RUNNER_DIR, images, run_info, worker_pool, workloads
from .cluster import Cluster
from .log import ON_CLOUD_RUN, log
from .process import output, stream

MANIFESTS_DIR = RUNNER_DIR.parent / "locust" / "manifests"
# Where a local run copies the master's uploads to, as <run-id>/<case>/ beneath
# it, as they are under the report prefix in the bucket.
REPORTS_DIR = RUNNER_DIR.parent.parent / "reports"
# The default report prefix, which a case's own bucket deletes reports under
# after its report_retention_days.
REPORT_PREFIX = "reports"
# Have to match metadata.name and namespace in the manifests.
MASTER_JOB = "locust-master"
WORKER_JOB = "boomer-worker"
NAMESPACE = "benchmarking"
MASTER_HOST = f"{MASTER_JOB}.{NAMESPACE}.svc.cluster.local"
USER_CLASS = "runworkload"
POD_START_TIMEOUT_SECS = 300
WAIT_TIMEOUT_SECS = 3600
# How long the Jobs' status may be unreadable before the run is given up on;
# a zonal cluster's API is down for minutes while GKE patches its control plane.
STATUS_ERROR_LIMIT_SECS = 600
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
# How long after the run its actors get to be gone before the ones left are
# recorded in cleanup.json.
ACTOR_DELETE_WAIT_SECS = 60
# The run id and case name end up as label values.
LABEL_RE = re.compile(r"[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?")


class LocustError(RuntimeError):
    pass


class LocustStillRunning(LocustError):
    """The run may still be going: it outlived WAIT_TIMEOUT_SECS, or its Jobs'
    status could not be read. Its actors are left alone. On a dev infra the
    next run would recreate the pool under it, so a series on one stops here."""


class LocustTimeout(LocustStillRunning):
    """The run outlived WAIT_TIMEOUT_SECS."""


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
    ratio = case.get("max-failure-ratio", DEFAULT_MAX_FAILURE_RATIO)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
        raise ValueError(f"case {name}'s `max-failure-ratio` must be a number from 0 to 1, got {ratio!r}")
    pods = case.get("locust-workers", 1)
    if not isinstance(pods, int) or isinstance(pods, bool) or pods < 1:
        raise ValueError(f"case {name}'s `locust-workers` must be a whole number from 1, got {pods!r}")
    sandbox_class = case.get("sandbox-class", "gvisor")
    if sandbox_class not in workloads.SANDBOX_CONFIGS:
        raise ValueError(f"case {name}'s `sandbox-class` must be one of {', '.join(workloads.SANDBOX_CONFIGS)}")
    template = worker.get("workload-template") or worker.get("workload-name")
    if deploy_workloads:
        if not template:
            raise ValueError(f"case {name}'s `worker` names no `workload-template` or `workload-name` to deploy")
        workloads.check_templates([template])
    workload = worker.get("workload-name") or template
    memory = case.get("actor-memory", workloads.default_actor_memory(workload))
    if not isinstance(memory, str) or workloads.memory_mib(memory) is None:
        raise ValueError(
            f"case {name}'s `actor-memory` must be a Kubernetes memory quantity such as 512Mi, got {memory!r}"
        )
    need = workloads.min_actor_memory_mib(workload, sandbox_class)
    if need and workloads.memory_mib(memory) < need:
        raise ValueError(
            f"case {name}'s `actor-memory` {memory} is less than {workload} needs on {sandbox_class}, {need}Mi"
        )
    # spawn-rate defaults to the user count, putting the whole load on at once.
    return {
        "worker-count": 1,
        "sandbox-class": "gvisor",
        "max-failure-ratio": DEFAULT_MAX_FAILURE_RATIO,
        "locust-workers": 1,
        **case,
        "actor-memory": memory,
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
        str(case["locust-workers"]),
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


class StatusUnread(LocustError):
    """kubectl could not read a Job."""


def job_status(cluster: Cluster, job: str) -> str | None:
    """"Complete" or "Failed" once the Job has ended, as its conditions say,
    else None. A Job of several pods ends once all of them have succeeded, or
    one has failed. Raises StatusUnread if the Job cannot be read."""
    proc = subprocess.run(
        cluster.kubectl("get", "job", job, '-o=jsonpath={.status.conditions[?(@.status=="True")].type}',
                        namespace=NAMESPACE),
        capture_output=True,
        text=True,
    )  # fmt: skip
    if proc.returncode:
        raise StatusUnread(f"kubectl get job {job}: {proc.stderr.strip()}")
    ended = set(proc.stdout.split()) & {"Complete", "Failed"}
    return ended.pop() if ended else None


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
        try:
            ended = job_status(cluster, MASTER_JOB)
        except StatusUnread:
            ended = None
        if ended:
            raise LocustError(f"{MASTER_JOB} ended before it was ready: {logs_hint(cluster, MASTER_JOB)}")
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


def logs_hint(cluster: Cluster, job: str) -> str:
    return f"kubectl logs --selector=job-name={job} --prefix --tail=-1 -n {NAMESPACE} --kubeconfig={cluster.kubeconfig}"


def wait_for_jobs(cluster: Cluster, stop_logs: Callable[[int], None]) -> None:
    """Waits for both Jobs to end rather than the first to fail, as each one's
    log is part of the run. Gives up as LocustStillRunning once the Jobs
    cannot be read for STATUS_ERROR_LIMIT_SECS, rather than wait out the hour
    on a run whose state is unknown."""
    log("locust: waiting for the run to finish")
    deadline = time.monotonic() + WAIT_TIMEOUT_SECS
    unread_since = None
    while time.monotonic() < deadline:
        try:
            statuses = {job: job_status(cluster, job) for job in (MASTER_JOB, WORKER_JOB)}
        except StatusUnread as exc:
            unread_since = unread_since or time.monotonic()
            if time.monotonic() - unread_since >= STATUS_ERROR_LIMIT_SECS:
                raise LocustStillRunning(
                    f"the Jobs' status could not be read for {STATUS_ERROR_LIMIT_SECS}s ({exc}); they may still be"
                    " running"
                ) from None
            time.sleep(5)
            continue
        unread_since = None
        if all(statuses.values()):
            stop_logs(10)
            failed = [job for job, status in statuses.items() if status != "Complete"]
            if failed:
                raise LocustError(f"{' and '.join(failed)} failed: " + "; ".join(logs_hint(cluster, j) for j in failed))
            log(f"locust: {MASTER_JOB} and {WORKER_JOB} completed: {logs_hint(cluster, MASTER_JOB)}")
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


def refuse_reused(bucket: str, report_prefix: str, run_id: str, name: str) -> None:
    """Refuses a run id the case was already recorded under, whose reports
    the bucket or the local copy hold: they would be mixed with this run's."""
    prefix = f"{report_prefix}/{run_id}/{name}/"
    local = REPORTS_DIR / run_id / name
    if local.is_dir() and any(local.iterdir()):
        raise LocustError(f"{local} already holds reports of {name} under run {run_id}; pick another run id")
    try:
        found = next(iter(storage.Client().list_blobs(bucket, prefix=prefix, max_results=1)), None)
    except Exception as exc:
        raise LocustError(f"cannot check gs://{bucket}/{prefix} for an earlier run: {exc}") from None
    if found is not None:
        raise LocustError(f"gs://{bucket}/{prefix} already holds reports; pick another run id")


def record_cleanup(cluster: Cluster, atespace: str, bucket: str, report_prefix: str, run_id: str, name: str) -> None:
    """Deletes the actors a run left in its atespace and writes what it found
    as cleanup.json beside the reports, in the bucket and locally. The worker
    deletes its own actors as the run ends; one it lost track of, or one stuck
    DELETING (a substrate fault no delete clears), would otherwise go unrecorded
    and, on a dev infra, hold a worker the next run cannot use."""
    record = {
        "atespace": atespace,
        "listed": False,
        "deleted": 0,
        # Actor name -> the error its delete was refused with.
        "delete_failed": {},
        "wait_secs": ACTOR_DELETE_WAIT_SECS,
        "left": [],
    }
    try:
        with workloads.kubectl_ate(cluster) as ate:

            def actors() -> list[dict]:
                listed = json.loads(output(ate("get", "actors", "-a", atespace, "-o", "json")))
                return [
                    {"name": a["metadata"]["name"], "state": (a.get("status") or {}).get("state", "ACTOR_STATE_UNKNOWN")}
                    for a in listed.get("actors") or []
                ]

            # Kept current, so a list that fails later leaves the last one seen.
            left = record["left"] = actors()
            record["listed"] = True
            for actor in left:
                proc = subprocess.run(ate("delete", "actor", actor["name"], "-a", atespace, "--any-state"),
                                      capture_output=True, text=True)  # fmt: skip
                if proc.returncode:
                    record["delete_failed"][actor["name"]] = proc.stderr.strip()
            record["deleted"] = len(left) - len(record["delete_failed"])
            deadline = time.monotonic() + ACTOR_DELETE_WAIT_SECS
            while left and time.monotonic() < deadline:
                time.sleep(5)
                left = record["left"] = actors()
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        log(f"locust: could not list the actors left in {atespace}: {exc}", "WARNING")
    for actor, error in record["delete_failed"].items():
        log(f"locust: could not delete actor {actor} in {atespace}: {error}", "WARNING")
    if record["left"]:
        names = ", ".join(f"{a['name']} ({a['state']})" for a in record["left"])
        log(f"locust: {len(record['left'])} actor(s) still in {atespace} {ACTOR_DELETE_WAIT_SECS}s after"
            f" deleting them: {names}", "WARNING")  # fmt: skip
    elif record["listed"]:
        log(f"locust: no actors left in {atespace} ({record['deleted']} deleted)")
    body = json.dumps(record, indent=2) + "\n"
    try:
        storage.Client().bucket(bucket).blob(f"{report_prefix}/{run_id}/{name}/cleanup.json").upload_from_string(body)
    except Exception as exc:
        log(f"locust: could not upload cleanup.json: {exc}", "WARNING")
    if not ON_CLOUD_RUN:
        local = REPORTS_DIR / run_id / name
        local.mkdir(parents=True, exist_ok=True)
        (local / "cleanup.json").write_text(body)


def upload_logs(cluster: Cluster, bucket: str, report_prefix: str, run_id: str, name: str) -> None:
    """Puts each Job's log, as <job>.log, beside what the master uploaded to
    bucket. Taken from the pods rather than uploaded by them so a crash is in
    it too. Best-effort: a log not uploaded is still on its pod until the Job's
    TTL."""
    prefix = f"{report_prefix}/{run_id}/{name}"
    for job in (MASTER_JOB, WORKER_JOB):
        # Every pod's, each line prefixed with its pod, as the worker Job can have several.
        cmd = cluster.kubectl("logs", f"--selector=job-name={job}", "--prefix", "--tail=-1", "--max-log-requests=50",
                              namespace=NAMESPACE)  # fmt: skip
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and not proc.stdout:
            proc.returncode, proc.stderr = 1, "no pods"
        if proc.returncode != 0:
            log(f"locust: no log of {job} to upload: {proc.stderr.strip()}", "WARNING")
            continue
        uri = f"gs://{bucket}/{prefix}/{job}.log"
        try:
            storage.Client().bucket(bucket).blob(f"{prefix}/{job}.log").upload_from_string(
                proc.stdout, content_type="text/plain"
            )
        except Exception as exc:
            log(f"locust: could not upload {uri}: {exc}", "WARNING")
        else:
            log(f"locust: uploaded {uri}")


def download_reports(bucket: str, report_prefix: str, run_id: str, name: str) -> bool:
    """Copies what the master uploaded into REPORTS_DIR/<run_id>/<name>/, while
    the bucket, if it is the case's own, is still there, and returns whether
    there was anything to copy and all of it was."""
    prefix = f"{report_prefix}/{run_id}/{name}/"
    dest = REPORTS_DIR / run_id / name
    log(f"locust: downloading the reports in gs://{bucket}/{prefix} to {dest}")
    try:
        blobs = list(storage.Client().list_blobs(bucket, prefix=prefix))
        for blob in blobs:
            path = dest / blob.name.removeprefix(prefix)
            path.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(path)
            log(f"locust: downloaded {path}")
    except Exception as exc:
        log(f"locust: could not download gs://{bucket}/{prefix}: {exc}", "WARNING")
        return False
    if blobs:
        log(f"locust: downloaded {len(blobs)} report files to {dest}")
    else:
        log(f"locust: no reports in gs://{bucket}/{prefix} to download", "WARNING")
    return bool(blobs)


def run(
    cluster: Cluster,
    name: str,
    case: dict,
    run_id: str | None = None,
    *,
    image_repo: str,
    deploy_workloads: bool = True,
    report_bucket: str | None = None,
    report_prefix: str = REPORT_PREFIX,
    test_case: str | None = None,
    repeat: int = 0,
    cloud_build: bool = False,
    keeps_bucket: bool = False,
) -> None:
    """Runs the case, recorded under name, as part of run_id, by default a UTC
    timestamp and the commit, with the images build_images pushed to
    image_repo. test_case and repeat say which case it is a repeat of; repeat
    is 0 for a case run once. Without deploy_workloads, uses the mock-llm and
    ActorTemplates already deployed. The worker pool is recreated either way.
    The reports go under report_prefix in report_bucket, by default the
    cluster's own, which keeps_bucket says outlives the run, as a dev infra's
    does. cloud_build says, for run.json, where the images were built.
    Expects the cluster's credentials in its kubeconfig."""
    case = check_case(name, case, deploy_workloads)
    # The workers would never schedule past the sandboxed pool's taint.
    if case["sandbox-class"] != cluster.sandbox_class:
        raise LocustError(
            f"case {name} is {case['sandbox-class']}, but cluster {cluster.name}'s sandboxed pool is"
            f" {cluster.sandbox_class}; set the cluster's sandbox_class to match"
        )
    run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dt%H%M%Sz}-{images.commit()[:12]}"
    if not LABEL_RE.fullmatch(run_id):
        raise ValueError(f"run id {run_id!r} is not a valid label value")
    bucket = report_bucket or cluster.bucket
    refuse_reused(bucket, report_prefix, run_id, name)
    # By digest, so the Jobs run exactly the images built from this tree even
    # if the tag is pushed again meanwhile; run.json records them.
    image_refs = {
        image: images.pin(images.image_ref(images.LOCUST_IMAGES, image_repo, image))
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
        workloads.deploy(cluster, [template_name], case["sandbox-class"], image_repo, case["actor-memory"])
    template = None
    if template_name:
        template = workloads.deployed_template(cluster, template_name)
        workloads.check_deployed(template, template_name, case["sandbox-class"], workload, case["actor-memory"])
        # A case may state its own allow-lists; otherwise the actors may call
        # what their template is configured to, and nothing else.
        cleartext, tls = workloads.egress_hosts(template)
        allow = {"actor-egress-allow": cleartext, "actor-egress-allow-tls": tls}
        worker = {**{key: ",".join(hosts) for key, hosts in allow.items() if hosts}, **case["worker"]}
        case = {**case, "worker": worker}
        log(
            f"locust: actors may connect to {worker.get('actor-egress-allow') or 'nothing'} over cleartext HTTP"
            f" and to {worker.get('actor-egress-allow-tls') or 'nothing'} over TLS"
        )

    info = run_info.collect(cluster, case, template_name, template, image_refs, bucket, cloud_build)
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
        "LOCUST_WORKERS": str(case["locust-workers"]),
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
    downloaded = None
    try:
        wait_master_ready(cluster)
        with master_logs(cluster) as stop_logs:
            apply(cluster, render("worker-job.yaml", values))
            wait_for_jobs(cluster, stop_logs)
    except LocustStillRunning:
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
            record_cleanup(cluster, atespace, bucket, report_prefix, run_id, name)
        upload_logs(cluster, bucket, report_prefix, run_id, name)
        # A failed run's reports are kept too. On Cloud Run they'd go with the
        # container, so they stay only in the bucket.
        if ON_CLOUD_RUN:
            log(f"locust: on Cloud Run, so the reports stay only in gs://{bucket}/{report_prefix}/{run_id}/{name}/")
        else:
            downloaded = download_reports(bucket, report_prefix, run_id, name)
    if downloaded is False and report_bucket is None and keeps_bucket:
        log(f"locust: the reports stay in gs://{bucket}/{report_prefix}/{run_id}/{name}/", "WARNING")
    # The case's own bucket goes with its cluster, so the local copy is the
    # only one; without it the run recorded nothing that lasts.
    elif downloaded is False and report_bucket is None:
        raise LocustError(
            f"the reports of {name} could not be downloaded from gs://{bucket}/{report_prefix}/{run_id}/{name}/, which"
            " is destroyed with the cluster; pass --report-bucket to keep them in a bucket of your own"
        )
