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
for both. Every key of the case's `locust.master` and `locust.worker` becomes
`--key=value` on that process, or `--key` for true."""

from __future__ import annotations

import base64
import json
import subprocess
import time
from datetime import datetime, timezone
from string import Template
from typing import Callable

from . import RUNNER_DIR, images, jobs, reports, run_info, worker_pool, workloads
from .cluster import Cluster
from .cluster_configs import CommonCluster, OpenclawCluster, from_infra
from .install import AGENT_SANDBOX, INSTALL, SUBSTRATE, control_plane, sandbox_class
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
# The case's mapping of its type, and what it may state.
TYPE = "locust"
KEYS = {
    "infra", INSTALL, "worker-count", "locust-workers", "actor-memory", "max-failure-ratio", "lifecycle-mode", "master",
    "worker",
}


class LocustError(RuntimeError):
    pass


class LocustStillRunning(LocustError):
    """The run may still be going: it outlived WAIT_TIMEOUT_SECS, or its Jobs'
    status could not be read. Its actors are left alone. On a dev infra the
    next run would recreate the pool under it, so a series on one stops here."""


class LocustTimeout(LocustStillRunning):
    """The run outlived WAIT_TIMEOUT_SECS."""


def missing_requirements() -> list[str]:
    missing = images.missing_requirements() + worker_pool.missing_requirements()
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
    options = case.get(TYPE)
    if not isinstance(options, dict):
        raise ValueError(f"case {name} has no `{TYPE}` mapping")
    unknown = options.keys() - KEYS
    if unknown:
        raise ValueError(f"case {name}'s `{TYPE}` has unknown {', '.join(sorted(unknown))}")
    if control_plane(case) is None:
        raise ValueError(f"case {name}'s `{TYPE}.{INSTALL}` must name {SUBSTRATE} or {AGENT_SANDBOX}, which run it")
    master, worker = options.get("master"), options.get("worker")
    if worker is None:
        worker = {}
    if not isinstance(master, dict) or not isinstance(worker, dict):
        raise ValueError(f"case {name}'s `{TYPE}.master` and `{TYPE}.worker` must be mappings of flags")
    if "run-time" not in master or "users" not in master:
        raise ValueError(f"case {name}'s `{TYPE}.master` must state `run-time` and `users`")
    count = options.get("worker-count", 1)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError(f"case {name}'s `{TYPE}.worker-count` must be a whole number, got {count!r}")
    ratio = options.get("max-failure-ratio", DEFAULT_MAX_FAILURE_RATIO)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
        raise ValueError(f"case {name}'s `{TYPE}.max-failure-ratio` must be a number from 0 to 1, got {ratio!r}")
    pods = options.get("locust-workers", 1)
    if not isinstance(pods, int) or isinstance(pods, bool) or pods < 1:
        raise ValueError(f"case {name}'s `{TYPE}.locust-workers` must be a whole number from 1, got {pods!r}")
    template = worker.get("workload-template") or worker.get("workload-name")
    if deploy_workloads:
        if not template:
            raise ValueError(f"case {name}'s `{TYPE}.worker` names no `workload-template` or `workload-name` to deploy")
        workloads.check_templates([template])
    workload = worker.get("workload-name") or template
    memory = options.get("actor-memory", workloads.default_actor_memory(workload))
    if not isinstance(memory, str) or workloads.memory_mib(memory) is None:
        raise ValueError(
            f"case {name}'s `{TYPE}.actor-memory` must be a Kubernetes memory quantity such as 512Mi, got {memory!r}"
        )
    on = sandbox_class(case)
    need = workloads.min_actor_memory_mib(workload, on)
    if need and workloads.memory_mib(memory) < need:
        raise ValueError(
            f"case {name}'s `{TYPE}.actor-memory` {memory} is less than {workload} needs on {on}, {need}Mi"
        )
    config = cluster_config(name, case)
    # mock-llm's pods select and tolerate only its pool, and stay pending without it.
    if template in workloads.MOCK_LLM_TEMPLATES and not isinstance(config, OpenclawCluster):
        raise ValueError(
            f"case {name}'s workload {template} calls mock-llm, which runs on a pool of its own:"
            f" set `{TYPE}.infra.cluster.configuration: {OpenclawCluster.CONFIGURATION}`"
        )
    # spawn-rate defaults to the user count, putting the whole load on at once.
    return {
        **case,
        TYPE: {
            "worker-count": 1,
            "max-failure-ratio": DEFAULT_MAX_FAILURE_RATIO,
            "locust-workers": 1,
            **options,
            "actor-memory": memory,
            "master": {**master, "spawn-rate": master.get("spawn-rate", master["users"])},
            "worker": worker,
        },
    }


def cluster_config(name: str, case: dict) -> CommonCluster:
    """The cluster the case runs on: its `locust.infra.cluster`, common or
    openclaw."""
    return from_infra(
        name,
        f"{TYPE}.infra",
        case[TYPE].get("infra"),
        (CommonCluster, OpenclawCluster),
    )


def master_args(options: dict) -> list[str]:
    """The master's arguments, of the case's checked `locust`."""
    return [
        "-f",
        f"/app/tests/{USER_CLASS}.py",
        "--headless",
        "--master",
        "--expect-workers",
        str(options["locust-workers"]),
        "--expect-workers-max-wait",
        str(POD_START_TIMEOUT_SECS),
        # run_master.py judges the run by its max-failure-ratio instead.
        "--exit-code-on-error",
        "0",
        *flags(options["master"]),
    ]


def worker_args(options: dict) -> list[str]:
    return [f"--master-host={MASTER_HOST}", f"--user-class={USER_CLASS}", *flags(options["worker"])]


def apply(cluster: Cluster, manifest: str) -> None:
    # Without --namespace, which kubectl would refuse worker-job.yaml's
    # RoleBinding in ate-system over.
    stream(cluster.kubectl("apply", "-f", "-"), input=manifest)


def render(manifest: str, values: dict[str, str]) -> str:
    return Template((MANIFESTS_DIR / manifest).read_text()).substitute(values)


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
            ended = jobs.job_status(cluster, MASTER_JOB, NAMESPACE)
        except jobs.StatusUnread:
            ended = None
        if ended:
            hint = jobs.logs_hint(cluster, MASTER_JOB, NAMESPACE)
            raise LocustError(f"{MASTER_JOB} ended before it was ready: {hint}")
        time.sleep(2)
    try:
        stream(cluster.kubectl("describe", "job", MASTER_JOB, namespace=NAMESPACE))
    except subprocess.CalledProcessError:
        pass
    raise LocustError(f"the {MASTER_JOB} pod was not ready within {POD_START_TIMEOUT_SECS}s")


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
            statuses = {job: jobs.job_status(cluster, job, NAMESPACE) for job in (MASTER_JOB, WORKER_JOB)}
        except jobs.StatusUnread as exc:
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
                hints = "; ".join(jobs.logs_hint(cluster, job, NAMESPACE) for job in failed)
                raise LocustError(f"{' and '.join(failed)} failed: {hints}")
            log(f"locust: {MASTER_JOB} and {WORKER_JOB} completed: {jobs.logs_hint(cluster, MASTER_JOB, NAMESPACE)}")
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
    return jobs.stop(cluster, (MASTER_JOB, WORKER_JOB), NAMESPACE, STOP_TIMEOUT_SECS)


def run(
    cluster: Cluster,
    name: str,
    case: dict,
    run_id: str | None = None,
    *,
    image_repo: str,
    deploy_workloads: bool = True,
    report_bucket: str | None = None,
    report_prefix: str = reports.REPORT_PREFIX,
    test_case: str | None = None,
    repeat: int = 0,
    warmup: int = 0,
    keeps_bucket: bool = False,
) -> None:
    """Runs the case, recorded under name, as part of run_id, by default a UTC
    timestamp and the commit, with the images built from this tree into
    image_repo. test_case and repeat say which case it is a repeat of; repeat
    is 0 for a case run once, or a warm-up, which warmup numbers. Without
    deploy_workloads, uses the mock-llm and ActorTemplates already deployed.
    The worker pool is recreated either way.
    The reports go under report_prefix in report_bucket, by default the
    cluster's own, which keeps_bucket says outlives the run, as a dev infra's
    does. Expects the cluster's credentials in its kubeconfig."""
    case = check_case(name, case, deploy_workloads)
    if sandbox_class(case) == "microvm" and not cluster.sandboxed_kvm:
        raise LocustError(
            f"case {name} is microvm, but cluster {cluster.name}'s sandboxed pool has no /dev/kvm for its guests"
        )
    run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dt%H%M%Sz}-{images.commit()[:12]}"
    if not reports.LABEL_RE.fullmatch(run_id):
        raise ValueError(f"run id {run_id!r} is not a valid label value")
    bucket = report_bucket or cluster.bucket
    reports.refuse_reused(bucket, report_prefix, run_id, name)
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

    provider = control_plane(case)
    options = case[TYPE]
    template_name = options["worker"].get("workload-template") or options["worker"].get("workload-name")
    workload = options["worker"].get("workload-name") or template_name
    template = None
    if provider == AGENT_SANDBOX:
        from . import agent_sandbox
        if deploy_workloads:
            agent_sandbox.deploy_workload_and_pool(
                cluster, workload, options["worker-count"], image_repo, sandbox_class(case)
            )
        else:
            agent_sandbox.recreate_warm_pool(cluster, workload, options["worker-count"])
        options = {
            **options,
            "worker": {
                **options["worker"],
                "provider": "agent-sandbox",
                "router-url": "http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080",
                "warm-pool-name": f"{workload}-warm-pool",
                "atespace": "benchmark-workloads",
            },
        }
        boomer_volume_mounts = ""
        boomer_volumes = ""
        boomer_rbac = f"""apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: benchmarking-job-permissions-sandbox
subjects:
- kind: ServiceAccount
  name: {cluster.runner_ksa}
  namespace: benchmarking
roleRef:
  kind: ClusterRole
  name: agent-sandbox-controller-extensions
  apiGroup: rbac.authorization.k8s.io"""
    else:
        worker_pool.deploy(cluster, options["worker-count"], sandbox_class(case))
        # The golden snapshot is taken on the pool just deployed.
        if deploy_workloads:
            workloads.deploy(cluster, [template_name], sandbox_class(case), image_repo, options["actor-memory"])
        if template_name:
            template = workloads.deployed_template(cluster, template_name)
            workloads.check_deployed(template, template_name, sandbox_class(case), workload, options["actor-memory"])
            # A case may state its own allow-lists; otherwise the actors may call
            # what their template is configured to, and nothing else.
            cleartext, tls = workloads.egress_hosts(template)
            allow = {"actor-egress-allow": cleartext, "actor-egress-allow-tls": tls}
            worker = {**{key: ",".join(hosts) for key, hosts in allow.items() if hosts}, **options["worker"]}
            options = {**options, "worker": worker}
            log(
                f"locust: actors may connect to {worker.get('actor-egress-allow') or 'nothing'} over cleartext HTTP"
                f" and to {worker.get('actor-egress-allow-tls') or 'nothing'} over TLS"
            )
        boomer_volume_mounts = """        volumeMounts:
        - name: servicedns-ca
          mountPath: /run/servicedns-ca
          readOnly: true
        - name: podidentity
          mountPath: /run/podidentity.podcert.ate.dev
          readOnly: true"""
        boomer_volumes = """      volumes:
      - name: servicedns-ca
        projected:
          sources:
          - clusterTrustBundle:
              signerName: servicedns.podcert.ate.dev/identity
              labelSelector:
                matchLabels:
                  podcert.ate.dev/canarying: live
              path: ca.crt
      - name: podidentity
        projected:
          sources:
          - podCertificate:
              signerName: podidentity.podcert.ate.dev/identity
              keyType: ECDSAP256
              credentialBundlePath: credential-bundle.pem"""
        boomer_rbac = f"""apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: benchmarking-job-permissions
  namespace: ate-system
subjects:
- kind: ServiceAccount
  name: {cluster.runner_ksa}
  namespace: benchmarking
roleRef:
  kind: Role
  name: atelet-endpointslices
  apiGroup: rbac.authorization.k8s.io"""

    # run.json records the worker's flags as run.
    case = {**case, TYPE: options}
    info = run_info.collect(cluster, case, cluster_config(name, case), template_name, template, image_refs, bucket)
    info["warmup"] = warmup or None
    values = {
        "LOCUST_IMAGE": image_refs["locust-master"],
        "BOOMER_IMAGE": image_refs["boomer-worker"],
        "BENCHMARK_RUNNER_KSA": cluster.runner_ksa,
        "BENCHMARK_RUN_ID": run_id,
        "BENCHMARK_RECORD_NAME": name,
        "BENCHMARK_CASE": test_case or name,
        "BENCHMARK_PROVIDER": provider,
        "BENCHMARK_REPEAT": str(repeat),
        "BENCHMARK_ONLY_SUMMARY": "false",
        "MAX_FAILURE_RATIO": str(options["max-failure-ratio"]),
        # What the master writes into run.json; base64, as it is JSON in YAML.
        "BENCHMARK_RUN_INFO": base64.b64encode(json.dumps(info).encode()).decode(),
        # Where the master uploads its CSVs once it exits.
        "BENCHMARK_REPORT_BUCKET": bucket,
        "BENCHMARK_REPORT_PREFIX": report_prefix,
        "LOCUST_WORKERS": str(options["locust-workers"]),
        # JSON is a YAML flow sequence.
        "LOCUST_MASTER_ARGS": json.dumps(master_args(options)),
        "BOOMER_WORKER_ARGS": json.dumps(worker_args(options)),
        "BOOMER_VOLUME_MOUNTS": boomer_volume_mounts,
        "BOOMER_VOLUMES": boomer_volumes,
        "BOOMER_RBAC": boomer_rbac,
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
        with jobs.follow_logs(cluster, MASTER_JOB, NAMESPACE) as stop_logs:
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
            if provider == AGENT_SANDBOX:
                from . import agent_sandbox
                rec = agent_sandbox.cleanup(cluster, "benchmark-workloads")
                log(f"locust: agent-sandbox cleanup: {rec['claims_deleted']} claims, {rec['sandboxes_deleted']} sandboxes deleted")
            else:
                atespace = options["worker"].get("atespace") or ACTOR_ATESPACE
                reports.record_cleanup(cluster, atespace, bucket, report_prefix, run_id, name)
        reports.upload_logs(cluster, NAMESPACE, (MASTER_JOB, WORKER_JOB), bucket, report_prefix, run_id, name)
        # A failed run's reports are kept too. On Cloud Run they'd go with the
        # container, so they stay only in the bucket.
        if ON_CLOUD_RUN:
            log(f"locust: on Cloud Run, so the reports stay only in gs://{bucket}/{report_prefix}/{run_id}/{name}/")
        else:
            downloaded = reports.download_reports(bucket, report_prefix, run_id, name)
    if downloaded is False and report_bucket is None and keeps_bucket:
        log(f"locust: the reports stay in gs://{bucket}/{report_prefix}/{run_id}/{name}/", "WARNING")
    # The case's own bucket goes with its cluster, so the local copy is the
    # only one; without it the run recorded nothing that lasts.
    elif downloaded is False and report_bucket is None:
        raise LocustError(
            f"the reports of {name} could not be downloaded from gs://{bucket}/{report_prefix}/{run_id}/{name}/, which"
            " is destroyed with the cluster; pass --report-bucket to keep them in a bucket of your own"
        )
