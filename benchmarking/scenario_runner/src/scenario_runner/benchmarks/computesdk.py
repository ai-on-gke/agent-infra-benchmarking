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

"""Runs a case of type computesdk: ComputeSDK burst_TTI or DAX benchmark,
running on Substrate (or cloud providers) via the computesdk-bench-runner Job of
computesdk-bench/manifests/runner-job.yaml.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Callable

import yaml

from .. import LABEL_RE, RUNNER_DIR, git, images, reports, workloads
from ..control_planes import substrate, worker_pool
from ..infra.cluster import Cluster
from ..infra.cluster_configs import CommonCluster, from_infra
from ..log import ON_CLOUD_RUN, log
from ..process import output, stream
from ..reports import run_info
from ..scenarios.install import INSTALL, SANDBOX_CLASS, SUBSTRATE, control_plane, sandbox_class
from . import jobs
from .sandbox_benchmark import ate_env

MANIFEST = RUNNER_DIR.parent / "computesdk-bench" / "manifests" / "runner-job.yaml"
COMPUTESDK_DIR = ate_env.DEPENDENCY_REPOS / "computesdk-benchmarks"
JOB = "computesdk-bench-runner"
NAMESPACE = jobs.NAMESPACE
RUNNER_IMAGE = "computesdk-bench-runner"
ROOTFS_IMAGE = "computesdk-bench-rootfs"

POD_START_TIMEOUT_SECS = 600
WAIT_TIMEOUT_SECS = 3600
STOP_TIMEOUT_SECS = 150
DEFAULT_MAX_FAILURE_RATIO = 0.01

TYPE = "computesdk"
KEYS = {
    "infra",
    INSTALL,
    "benchmark",
    "provider",
    "iterations",
    "concurrency",
    "actor-memory",
    "actor-cpu",
    "max-failure-ratio",
}
IMAGE_FILES = [images.COMPUTESDK_BENCH_IMAGES, images.SANDBOX_BENCHMARK_IMAGES]

BENCHMARKS = ("burst-tti", "burst_tti", "dax")
PROVIDERS = ("substrate", "e2b", "modal", "daytona")
PROVIDER_KEYS = {
    "e2b": ("E2B_API_KEY",),
    "daytona": ("DAYTONA_API_KEY",),
    "modal": ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"),
}
PROVIDER_SECRET = "sandbox-benchmark-providers"

COMPUTESDK_ATESPACE = "ate-env"
COMPUTESDK_TEMPLATE = "computesdk-template"
COMPUTESDK_POOL = "computesdk-workerpool"
POOL_LABELS = {"workload": "computesdk-bench"}


class ComputeSDKError(RuntimeError):
    pass


class ComputeSDKTimeout(ComputeSDKError):
    """The run outlived WAIT_TIMEOUT_SECS and is still going."""


def missing_requirements() -> list[str]:
    missing = images.missing_requirements() + ate_env.missing_requirements()
    if not (COMPUTESDK_DIR / "package.json").is_file():
        missing.append(f"the computesdk-benchmarks submodule in {COMPUTESDK_DIR}")
    return list(dict.fromkeys(missing))


def whole(value: object, minimum: int = 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def check_case(name: str, case: dict, deploy_workloads: bool = True) -> dict:
    """The case with its defaults filled in."""
    if not LABEL_RE.fullmatch(name):
        raise ValueError(f"case name {name!r} is not a valid label value")
    options = case.get(TYPE)
    if not isinstance(options, dict):
        raise ValueError(f"case {name} has no `{TYPE}` mapping")
    unknown = options.keys() - KEYS
    if unknown:
        raise ValueError(f"case {name}'s `{TYPE}` has unknown {', '.join(sorted(unknown))}")

    bench = options.get("benchmark", "burst-tti")
    if bench not in BENCHMARKS:
        raise ValueError(f"case {name}'s `{TYPE}.benchmark` must be one of {', '.join(BENCHMARKS)}, got {bench!r}")

    provider = options.get("provider", "substrate")
    if provider not in PROVIDERS:
        raise ValueError(f"case {name}'s `{TYPE}.provider` must be one of {', '.join(PROVIDERS)}, got {provider!r}")

    iterations = options.get("iterations", 5 if "tti" in bench else 1)
    if not whole(iterations, 1):
        raise ValueError(f"case {name}'s `{TYPE}.iterations` must be a whole number >= 1")

    concurrency = options.get("concurrency", 3 if "tti" in bench else 1)
    if not whole(concurrency, 1):
        raise ValueError(f"case {name}'s `{TYPE}.concurrency` must be a whole number >= 1")

    if provider == "substrate" and control_plane(case) != SUBSTRATE:
        raise ValueError(f"case {name}'s substrate provider needs `{TYPE}.{INSTALL}.{SUBSTRATE}.{SANDBOX_CLASS}`")

    cluster_config(name, case)
    return {
        **case,
        TYPE: {
            "benchmark": bench.replace("_", "-"),
            "provider": provider,
            "iterations": iterations,
            "concurrency": concurrency,
            "actor-memory": options.get("actor-memory", "16Gi" if bench == "dax" else "2Gi"),
            "actor-cpu": options.get("actor-cpu", "4" if bench == "dax" else "2"),
            "max-failure-ratio": options.get("max-failure-ratio", DEFAULT_MAX_FAILURE_RATIO),
            **options,
        },
    }


def cluster_config(name: str, case: dict) -> CommonCluster:
    return from_infra(name, f"{TYPE}.infra", case[TYPE].get("infra"))


def report_folder(name: str, test_case: str, case: dict) -> str:
    benchmark = case[TYPE]["benchmark"]
    provider = case[TYPE]["provider"]
    return f"{name}/{provider}-{benchmark}"


def wait_started(cluster: Cluster) -> None:
    """Waits for the Job's pod to be running, or done, as kubectl logs --follow
    of one still starting returns at once."""
    log(f"computesdk: waiting for the {JOB} pod to start")
    deadline = time.monotonic() + POD_START_TIMEOUT_SECS
    phase = cluster.kubectl(
        "get", "pods", f"--selector=job-name={JOB}", "-o=jsonpath={.items[0].status.phase}", namespace=NAMESPACE
    )
    while time.monotonic() < deadline:
        if subprocess.run(phase, capture_output=True, text=True).stdout in ("Running", "Succeeded", "Failed"):
            return
        time.sleep(2)
    try:
        stream(cluster.kubectl("describe", "job", JOB, namespace=NAMESPACE))
    except subprocess.CalledProcessError:
        pass
    raise ComputeSDKError(f"the {JOB} pod did not start within {POD_START_TIMEOUT_SECS}s")


def wait_for_job(cluster: Cluster, stop_logs: Callable[[int], None]) -> None:
    log("computesdk: waiting for the run to finish")
    deadline = time.monotonic() + WAIT_TIMEOUT_SECS
    while time.monotonic() < deadline:
        try:
            ended = jobs.job_status(cluster, JOB, NAMESPACE)
        except jobs.StatusUnread:
            ended = None
        if ended:
            stop_logs(10)
            if ended == "Failed":
                raise ComputeSDKError(f"{JOB} failed: {jobs.logs_hint(cluster, JOB, NAMESPACE)}")
            log(f"computesdk: {JOB} completed: {jobs.logs_hint(cluster, JOB, NAMESPACE)}")
            return
        time.sleep(5)
    raise ComputeSDKTimeout(f"timed out after {WAIT_TIMEOUT_SECS}s; the Job is left running")


def commit(repo: Path) -> str | None:
    try:
        return git.head(repo) if repo.is_dir() else None
    except subprocess.CalledProcessError:
        return None


def render_stack(cluster: Cluster, sandbox_class: str, worker_count: int) -> str:
    config_name = worker_pool.SANDBOX_CONFIGS[sandbox_class]
    manifests = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": COMPUTESDK_ATESPACE},
        },
        {
            "apiVersion": "ate.dev/v1",
            "kind": "WorkerPool",
            "metadata": {
                "name": COMPUTESDK_POOL,
                "namespace": COMPUTESDK_ATESPACE,
                "labels": POOL_LABELS,
            },
            "spec": {
                "replicas": worker_count,
                "sandboxClasses": [{"name": sandbox_class, "configRef": {"name": config_name}}],
                "workerImage": f"ko://go.ate.dev/substrate/cmd/ateworker-{sandbox_class}",
                "template": {
                    "nodeSelector": {"benchmarking-worker": "true"},
                    "tolerations": [{"key": "ate.dev/sandboxClass", "operator": "Exists", "effect": "NoSchedule"}],
                },
            },
        },
    ]
    return substrate.resolve(cluster, yaml.safe_dump_all(manifests, sort_keys=False))


def render_template(
    cli: ate_env.AteEnv,
    cluster: Cluster,
    guest_image: str,
    rootfs_image: str,
    actor_memory: str,
    actor_cpu: str,
) -> str:
    manifest = output(
        cli(
            "manifest",
            "template",
            f"--template={COMPUTESDK_TEMPLATE}",
            f"--atespace={COMPUTESDK_ATESPACE}",
            f"--guest-image={guest_image}",
            f"--rootfs-image={rootfs_image}",
            f"--snapshots-bucket=gs://{cluster.bucket}/{COMPUTESDK_ATESPACE}/{COMPUTESDK_TEMPLATE}/",
        )
    )
    template_dict = yaml.safe_load(manifest)
    template_dict.pop("sandboxConfig", None)
    if "snapshotConfig" in template_dict:
        template_dict["snapshotConfig"].pop("storageLocation", None)
        template_dict["snapshotConfig"].pop("storage_location", None)
    template_dict["workerSelector"] = {"matchLabels": POOL_LABELS}
    template_dict["resources"] = {
        "limits": {
            "cpuMillicores": str(workloads.cpu_millicores(actor_cpu)),
            "memoryBytes": str(workloads.memory_bytes(actor_memory)),
        }
    }
    return yaml.safe_dump(template_dict, sort_keys=False)


def deploy_computesdk_workload(
    cluster: Cluster,
    image_repo: str,
    sandbox_class: str,
    actor_memory: str,
    actor_cpu: str,
) -> None:
    """Deploys the ActorTemplate and worker pool for ComputeSDK workloads."""
    ate_env.check_client()
    guest_image = ate_env.pinned_image(image_repo, "ate-env-guest")
    rootfs_image = images.pin(images.image_ref(images.COMPUTESDK_BENCH_IMAGES, image_repo, ROOTFS_IMAGE))

    stack = render_stack(cluster, sandbox_class, 5)

    with ate_env.ate_env() as cli:
        template_yaml = render_template(cli, cluster, guest_image, rootfs_image, actor_memory, actor_cpu)

    stream(cluster.kubectl("apply", "-f", "-"), input=stack)
    worker_pool.wait_ready(cluster, name=COMPUTESDK_POOL, namespace=COMPUTESDK_ATESPACE)

    with workloads.kubectl_ate(cluster) as ate:
        workloads.ensure_atespace(ate, COMPUTESDK_ATESPACE)
        workloads.delete_template(ate, COMPUTESDK_TEMPLATE, COMPUTESDK_ATESPACE)
        stream(ate("create", "actor-template", "-f", "-"), input=template_yaml)
        workloads.wait_golden_snapshot(ate, cluster, COMPUTESDK_TEMPLATE, 300, COMPUTESDK_ATESPACE, (COMPUTESDK_ATESPACE, COMPUTESDK_POOL))


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
    folder = report_folder(name, test_case or name, case)
    case = check_case(name, case)
    options = case[TYPE]
    provider = options["provider"]
    benchmark = options["benchmark"]

    if provider == "substrate" and sandbox_class(case) == "microvm" and not cluster.sandboxed_kvm:
        raise ComputeSDKError(
            f"case {name} is microvm, but cluster {cluster.name}'s sandboxed pool has no /dev/kvm for its guests"
        )

    run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dt%H%M%Sz}-{images.commit()[:12]}"
    if not LABEL_RE.fullmatch(run_id):
        raise ValueError(f"run id {run_id!r} is not a valid label value")

    bucket = report_bucket or cluster.bucket
    reports.refuse_reused(bucket, report_prefix, run_id, folder)

    runner_image = images.pin(images.image_ref(images.COMPUTESDK_BENCH_IMAGES, image_repo, RUNNER_IMAGE))
    on_substrate = provider == "substrate"
    template_data = None

    if on_substrate and deploy_workloads:
        deploy_computesdk_workload(
            cluster, image_repo, sandbox_class(case), options["actor-memory"], options["actor-cpu"]
        )

    if on_substrate:
        template_data = workloads.deployed_template(cluster, COMPUTESDK_TEMPLATE, COMPUTESDK_ATESPACE)

    info = run_info.collect(
        cluster, case, cluster_config(name, case), COMPUTESDK_TEMPLATE if on_substrate else None, template_data,
        {RUNNER_IMAGE: runner_image}, bucket, pool=(COMPUTESDK_ATESPACE, COMPUTESDK_POOL),
    )
    info["computesdk_benchmarks_commit"] = commit(COMPUTESDK_DIR)
    info["repeat"] = repeat or None

    values = {
        "RUNNER_IMAGE": runner_image,
        "BENCHMARK_RUNNER_KSA": cluster.runner_ksa,
        "BENCHMARK_NAME": benchmark,
        "BENCHMARK_PROVIDER": provider,
        "BENCHMARK_ITERATIONS": str(options["iterations"]),
        "BENCHMARK_CONCURRENCY": str(options["concurrency"]),
        "SUBSTRATE_ATESPACE": COMPUTESDK_ATESPACE,
        "SUBSTRATE_TEMPLATE": COMPUTESDK_TEMPLATE,
        "BENCHMARK_RUN_ID": run_id,
        "BENCHMARK_RECORD_NAME": name,
        "BENCHMARK_CASE": test_case or name,
        "BENCHMARK_REPEAT": str(repeat),
        "BENCHMARK_RUN_INFO": base64.b64encode(json.dumps(info).encode()).decode(),
        "BENCHMARK_REPORT_BUCKET": bucket,
        "BENCHMARK_REPORT_PREFIX": report_prefix,
        "BENCHMARK_REPORT_FOLDER": folder,
    }

    cluster.apply_namespace(NAMESPACE)
    keys = PROVIDER_KEYS.get(provider, ())
    if keys:
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": PROVIDER_SECRET, "namespace": NAMESPACE},
            "data": {key: base64.b64encode(os.environ[key].encode()).decode() for key in keys},
        }
        stream(cluster.kubectl("apply", "--server-side", "-f", "-"), input=json.dumps(secret))

    log(f"computesdk: deleting previous {JOB}")
    stream(cluster.kubectl("delete", "job", JOB, "--ignore-not-found", "--cascade=foreground", "--wait", namespace=NAMESPACE))

    log(
        f"computesdk: running {name} ({benchmark}) on {provider} (run {run_id}):\n"
        f"  runner: {runner_image}\n"
        f"  iterations: {options['iterations']}, concurrency: {options['concurrency']}\n"
        f"  reports: gs://{bucket}/{report_prefix}/{run_id}/{folder}/"
    )
    job_manifest = Template(MANIFEST.read_text()).substitute(values)
    stream(cluster.kubectl("apply", "-f", "-"), input=job_manifest)

    still_running = False
    try:
        wait_started(cluster)
        with jobs.follow_logs(cluster, JOB, NAMESPACE) as stop_logs:
            wait_for_job(cluster, stop_logs)
    except ComputeSDKTimeout:
        still_running = True
        raise
    except (KeyboardInterrupt, SystemExit):
        log(f"computesdk: interrupted; stopping {JOB}")
        still_running = not jobs.stop(cluster, (JOB,), NAMESPACE, STOP_TIMEOUT_SECS)
        raise
    finally:
        if keys:
            subprocess.run(
                cluster.kubectl("delete", "secret", PROVIDER_SECRET, "--ignore-not-found", namespace=NAMESPACE),
                capture_output=True,
            )
        if on_substrate and not still_running:
            reports.record_cleanup(cluster, COMPUTESDK_ATESPACE, bucket, report_prefix, run_id, folder)
        reports.upload_logs(cluster, NAMESPACE, (JOB,), bucket, report_prefix, run_id, folder)
        if not ON_CLOUD_RUN:
            reports.download_reports(bucket, report_prefix, run_id, folder)
