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

"""Runs a case of type sandbox-benchmark: ai-sandbox-benchmark's tests on its
substrate provider, through agent-substrate/env deployed for them by ate_env,
as the comparator Job of sandbox-benchmark/manifests/comparator-job.yaml, and
waits for it. The case's `comparator` names the tests by function name, how
many times each runs and how many warmup runs go before; `env-worker-count`
and `actor-memory` size env's pool and the environments."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from string import Template
from typing import Callable

from . import RUNNER_DIR, ate_env, images, jobs, reports, run_info, workloads
from .cluster import Cluster
from .log import ON_CLOUD_RUN, log
from .process import stream

MANIFEST = RUNNER_DIR.parent / "sandbox-benchmark" / "manifests" / "comparator-job.yaml"
AI_SANDBOX_BENCHMARK_DIR = ate_env.DEPENDENCY_REPOS / "ai-sandbox-benchmark"
# Have to match metadata.name and namespace in the manifest.
JOB = "sandbox-benchmark-comparator"
NAMESPACE = "benchmarking"
COMPARATOR_IMAGE = "sandbox-benchmark-comparator"
# The comparator image is large; its first pull on a node takes a while.
POD_START_TIMEOUT_SECS = 600
WAIT_TIMEOUT_SECS = 3600
# The manifest's terminationGracePeriodSeconds and a margin.
STOP_TIMEOUT_SECS = 150
# The share of runs, 0 to 1, a case may fail before its run counts as failed.
DEFAULT_MAX_FAILURE_RATIO = 0.01
# ate-env manifest's own default.
DEFAULT_ENV_WORKER_COUNT = 5
CASE_KEYS = {
    "type", "infra", "repeats", "skip-substrate-install", "sandbox-class", "env-worker-count", "actor-memory",
    "max-failure-ratio", "comparator",
}  # fmt: skip
COMPARATOR_KEYS = {"tests", "runs", "warmup-runs"}


class SandboxBenchmarkError(RuntimeError):
    pass


class SandboxBenchmarkTimeout(SandboxBenchmarkError):
    """The run outlived WAIT_TIMEOUT_SECS and is still going. On a dev infra
    the next run would recreate env's pool under it, so a series stops here."""


def missing_requirements(cloud_build: bool = False) -> list[str]:
    missing = images.missing_requirements(cloud_build) + ate_env.missing_requirements()
    if not (AI_SANDBOX_BENCHMARK_DIR / "comparator.py").is_file():
        missing.append(f"the ai-sandbox-benchmark submodule in {AI_SANDBOX_BENCHMARK_DIR}")
    return list(dict.fromkeys(missing))


def test_names() -> set[str] | None:
    """The tests the submodule defines, as the comparator finds them; None
    without the submodule, which missing_requirements reports."""
    tests_dir = AI_SANDBOX_BENCHMARK_DIR / "tests"
    if not tests_dir.is_dir():
        return None
    return {
        m[1]
        for path in tests_dir.glob("*.py")
        for m in re.finditer(r"^def (test_\w+)\(", path.read_text(), re.MULTILINE)
    }


def whole(value: object, least: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= least


def check_case(name: str, case: dict, deploy_workloads: bool) -> dict:
    """The case with its defaults filled in."""
    if not reports.LABEL_RE.fullmatch(name):
        raise ValueError(f"case name {name!r} is not a valid label value")
    unknown = case.keys() - CASE_KEYS
    if unknown:
        raise ValueError(f"case {name} has unknown {', '.join(sorted(unknown))}")
    if case.get("sandbox-class", "gvisor") not in workloads.SANDBOX_CONFIGS:
        raise ValueError(f"case {name}'s `sandbox-class` must be one of {', '.join(workloads.SANDBOX_CONFIGS)}")
    count = case.get("env-worker-count", DEFAULT_ENV_WORKER_COUNT)
    if not whole(count, 1):
        raise ValueError(f"case {name}'s `env-worker-count` must be a whole number from 1, got {count!r}")
    memory = case.get("actor-memory", ate_env.DEFAULT_ACTOR_MEMORY)
    if not isinstance(memory, str) or workloads.memory_mib(memory) is None:
        raise ValueError(f"case {name}'s `actor-memory` must be a Kubernetes quantity such as 1Gi, got {memory!r}")
    ratio = case.get("max-failure-ratio", DEFAULT_MAX_FAILURE_RATIO)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
        raise ValueError(f"case {name}'s `max-failure-ratio` must be a number from 0 to 1, got {ratio!r}")

    comparator = case.get("comparator")
    if not isinstance(comparator, dict):
        raise ValueError(f"case {name} has no `comparator` mapping")
    unknown = comparator.keys() - COMPARATOR_KEYS
    if unknown:
        raise ValueError(f"case {name}'s `comparator` has unknown {', '.join(sorted(unknown))}")
    tests = comparator.get("tests")
    if not isinstance(tests, list) or not tests or not all(isinstance(t, str) for t in tests):
        raise ValueError(f"case {name}'s `comparator.tests` must be a list of test function names")
    known = test_names()
    if known is not None and set(tests) - known:
        raise ValueError(
            f"case {name}'s `comparator.tests` has unknown {', '.join(sorted(set(tests) - known))};"
            f" there are {', '.join(sorted(known))}"
        )
    if not whole(comparator.get("runs"), 1):
        raise ValueError(f"case {name}'s `comparator.runs` must be a whole number from 1")
    if not whole(comparator.get("warmup-runs", 0), 0):
        raise ValueError(f"case {name}'s `comparator.warmup-runs` must be a whole number")
    return {
        "sandbox-class": "gvisor",
        "env-worker-count": DEFAULT_ENV_WORKER_COUNT,
        "actor-memory": ate_env.DEFAULT_ACTOR_MEMORY,
        "max-failure-ratio": DEFAULT_MAX_FAILURE_RATIO,
        **case,
        "comparator": {"warmup-runs": 0, **comparator},
    }


def commit(path: object) -> str | None:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def wait_started(cluster: Cluster) -> None:
    """Waits for the Job's pod to be running, or done, as kubectl logs --follow
    of one still starting returns at once."""
    log(f"sandbox-benchmark: waiting for the {JOB} pod to start")
    deadline = time.monotonic() + POD_START_TIMEOUT_SECS
    phase = cluster.kubectl("get", "pods", f"--selector=job-name={JOB}", "-o=jsonpath={.items[0].status.phase}",
                            namespace=NAMESPACE)  # fmt: skip
    while time.monotonic() < deadline:
        if subprocess.run(phase, capture_output=True, text=True).stdout in ("Running", "Succeeded", "Failed"):
            return
        time.sleep(2)
    try:
        stream(cluster.kubectl("describe", "job", JOB, namespace=NAMESPACE))
    except subprocess.CalledProcessError:
        pass
    raise SandboxBenchmarkError(f"the {JOB} pod did not start within {POD_START_TIMEOUT_SECS}s")


def wait_for_job(cluster: Cluster, stop_logs: Callable[[int], None]) -> None:
    log("sandbox-benchmark: waiting for the run to finish")
    deadline = time.monotonic() + WAIT_TIMEOUT_SECS
    while time.monotonic() < deadline:
        succeeded, failed = jobs.job_status(cluster, JOB, NAMESPACE)
        if succeeded or failed:
            stop_logs(10)
            if failed:
                # run_comparator.py's exit status says why, and run.json what it counted.
                raise SandboxBenchmarkError(f"{JOB} failed: kubectl logs job/{JOB} -n {NAMESPACE}")
            log(f"sandbox-benchmark: {JOB} completed: kubectl logs job/{JOB} -n {NAMESPACE}")
            return
        time.sleep(5)
    raise SandboxBenchmarkTimeout(f"timed out after {WAIT_TIMEOUT_SECS}s; the Job is left running")


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
    already pushed; without deploy_workloads, the env and template already
    deployed. Without docker_login, a local build uses the registry
    credentials Docker already has. The reports go under report_prefix in
    report_bucket, by default the cluster's own. Expects the cluster's
    credentials in its kubeconfig."""
    case = check_case(name, case, deploy_workloads)
    sandbox_class = case["sandbox-class"]
    # env's workers would never schedule past the sandboxed pool's taint.
    if sandbox_class != cluster.sandbox_class:
        raise SandboxBenchmarkError(
            f"case {name} is {sandbox_class}, but cluster {cluster.name}'s sandboxed pool is"
            f" {cluster.sandbox_class}; set the cluster's sandbox_class to match"
        )
    run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dt%H%M%Sz}-{images.tag()}"
    if not reports.LABEL_RE.fullmatch(run_id):
        raise ValueError(f"run id {run_id!r} is not a valid label value")
    bucket = report_bucket or cluster.bucket
    # Before the build, which is the slow part.
    reports.refuse_reused(bucket, report_prefix, run_id, name)

    if build:
        images.build_and_push(cluster, [images.SANDBOX_BENCHMARK_IMAGES], cloud_build=cloud_build, login=docker_login)
        # env's images are only read when it is deployed.
        if deploy_workloads:
            ate_env.build_images(cluster, cloud_build=cloud_build, login=docker_login)
    # By digest, so the Job runs exactly the image built from this commit even
    # if the tag is pushed again meanwhile; run.json records it.
    comparator_image = images.pin(
        images.image_ref(images.SANDBOX_BENCHMARK_IMAGES, cluster.docker_repo, COMPARATOR_IMAGE)
    )

    if deploy_workloads:
        ate_env.deploy(cluster, sandbox_class, case["env-worker-count"], case["actor-memory"])
    template = workloads.deployed_template(cluster, ate_env.TEMPLATE, ate_env.ENV_ATESPACE)
    # Matters when the template was not deployed by this run (--no-deploy-workloads).
    want = workloads.SANDBOX_CONFIGS[sandbox_class][1]
    have = (template.get("sandboxConfig") or {}).get("sandboxClass")
    if have != want:
        raise SandboxBenchmarkError(
            f"actor template {ate_env.ENV_ATESPACE}/{ate_env.TEMPLATE} is {have or 'of no sandbox class'},"
            f" not {want}; deploy it again"
        )

    info = run_info.collect(
        cluster, case, ate_env.TEMPLATE, template, {COMPARATOR_IMAGE: comparator_image}, bucket,
        pool=(ate_env.NAMESPACE, ate_env.POOL),
    )  # fmt: skip
    info["env_commit"] = commit(ate_env.ENV_DIR)
    info["ai_sandbox_benchmark_commit"] = commit(AI_SANDBOX_BENCHMARK_DIR)
    comparator = case["comparator"]
    values = {
        "COMPARATOR_IMAGE": comparator_image,
        "BENCHMARK_RUNNER_KSA": cluster.runner_ksa,
        "BENCHMARK_TESTS": ",".join(comparator["tests"]),
        "BENCHMARK_RUNS": str(comparator["runs"]),
        "BENCHMARK_WARMUP_RUNS": str(comparator["warmup-runs"]),
        "MAX_FAILURE_RATIO": str(case["max-failure-ratio"]),
        "BENCHMARK_RUN_ID": run_id,
        "BENCHMARK_RECORD_NAME": name,
        "BENCHMARK_CASE": test_case or name,
        "BENCHMARK_REPEAT": str(repeat),
        # What run_comparator.py writes into run.json; base64, as it is JSON in YAML.
        "BENCHMARK_RUN_INFO": base64.b64encode(json.dumps(info).encode()).decode(),
        "BENCHMARK_REPORT_BUCKET": bucket,
        "BENCHMARK_REPORT_PREFIX": report_prefix,
        "SUBSTRATE_ENV_API_ADDR": ate_env.API_ADDR,
        "SUBSTRATE_ATESPACE": ate_env.ENV_ATESPACE,
        "SUBSTRATE_TEMPLATE": f"{ate_env.ENV_ATESPACE}/{ate_env.TEMPLATE}",
    }

    stream(
        cluster.kubectl("apply", "-f", "-"), input=f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {NAMESPACE}\n"
    )
    # A Job's pod template is immutable. Foreground, so its pod is gone too and
    # the job-name lookup only sees this run's.
    log(f"sandbox-benchmark: deleting any previous {JOB}")
    stream(
        cluster.kubectl(
            "delete", "job", JOB, "--ignore-not-found", "--cascade=foreground", "--wait", namespace=NAMESPACE
        )
    )
    log(
        f"sandbox-benchmark: running {name} (run {run_id}):\n"
        f"  comparator: {comparator_image}\n"
        f"  tests: {values['BENCHMARK_TESTS']} ({comparator['runs']} runs, {comparator['warmup-runs']} warmup)\n"
        f"  reports: gs://{bucket}/{report_prefix}/{run_id}/{name}/"
    )
    stream(cluster.kubectl("apply", "-f", "-"), input=Template(MANIFEST.read_text()).substitute(values))
    # The environments are the run's to delete while it is still going.
    still_running = False
    try:
        wait_started(cluster)
        with jobs.follow_logs(cluster, JOB, NAMESPACE) as stop_logs:
            wait_for_job(cluster, stop_logs)
    except SandboxBenchmarkTimeout:
        still_running = True
        raise
    except (KeyboardInterrupt, SystemExit):
        # Ctrl-C, or SIGTERM through cli.run's handler: stop the run so it
        # deletes its environments and uploads run.json before the cluster goes.
        log(f"sandbox-benchmark: interrupted; stopping {JOB} so it can upload what it recorded")
        still_running = not jobs.stop(cluster, (JOB,), NAMESPACE, STOP_TIMEOUT_SECS)
        raise
    finally:
        if not still_running:
            reports.record_cleanup(cluster, ate_env.ENV_ATESPACE, bucket, report_prefix, run_id, name)
        reports.upload_logs(cluster, NAMESPACE, (JOB,), bucket, report_prefix, run_id, name)
        # A failed run's reports are kept too. On Cloud Run they'd go with the
        # container, so they stay only in the bucket.
        if ON_CLOUD_RUN:
            log(
                f"sandbox-benchmark: on Cloud Run, so the reports stay only in gs://{bucket}/{report_prefix}/{run_id}/{name}/"
            )
        else:
            reports.download_reports(bucket, report_prefix, run_id, name)
