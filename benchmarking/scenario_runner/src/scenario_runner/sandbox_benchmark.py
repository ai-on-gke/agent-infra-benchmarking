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

"""Runs a case of type sandbox-benchmark: ai-sandbox-benchmark's tests on each
of its providers in turn, each as a run of the comparator Job of
sandbox-benchmark/manifests/comparator-job.yaml, and combines their results
into those of one comparator run of them all. The case's `comparator` names
the providers, the tests by function name, how many times each runs and how
many warmup runs go before. The substrate provider runs them through
agent-substrate/env deployed for them by ate_env, whose pool and environments
`env-worker-count`, `actor-memory` and `actor-cpu` size; local runs them in the
comparator's own pod; e2b, daytona, modal, anthropic and openai in their
clouds, with the PROVIDER_KEYS of the scenario-runner's environment."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from string import Template
from typing import Callable

from google.cloud import storage

from . import RUNNER_DIR, ate_env, images, jobs, reports, run_info, workloads
from . import sandbox_benchmark_combine as combine
from . import sandbox_benchmark_cost_sheet as costs
from .cluster import Cluster
from .log import ON_CLOUD_RUN, log
from .process import failure, stream

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
    "type", "infra", "repeats", "repeat-cluster", "warmup-repeats", "skip-substrate-install", "sandbox-class",
    "env-worker-count", "actor-memory", "actor-cpu", "max-failure-ratio", "comparator", "cost-sheet", "provider",
    "lifecycle-mode",
}  # fmt: skip
COMPARATOR_KEYS = {"providers", "tests", "runs", "warmup-runs"}
PROVIDERS = ("substrate", "local", "e2b", "daytona", "modal", "anthropic", "openai")
# The API keys a provider needs, read from the scenario-runner's environment
# and handed to the comparator in the Secret PROVIDER_SECRET, which the
# manifest's env reads; have to match it.
PROVIDER_KEYS = {
    "e2b": ("E2B_API_KEY",),
    "daytona": ("DAYTONA_API_KEY",),
    "modal": ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
}
PROVIDER_SECRET = "sandbox-benchmark-providers"


class SandboxBenchmarkError(RuntimeError):
    pass


class SandboxBenchmarkTimeout(SandboxBenchmarkError):
    """The run outlived WAIT_TIMEOUT_SECS and is still going. On a dev infra
    the next run would recreate env's pool under it, so a series stops here."""


def missing_requirements() -> list[str]:
    missing = images.missing_requirements() + ate_env.missing_requirements()
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
    cpu = case.get("actor-cpu", ate_env.DEFAULT_ACTOR_CPU)
    if not isinstance(cpu, str) or not re.fullmatch(r"[1-9][0-9]*m?|[0-9]*\.[0-9]+", cpu):
        raise ValueError(f"case {name}'s `actor-cpu` must be a quoted cpu quantity such as \"2\" or \"1500m\", got {cpu!r}")
    ratio = case.get("max-failure-ratio", DEFAULT_MAX_FAILURE_RATIO)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
        raise ValueError(f"case {name}'s `max-failure-ratio` must be a number from 0 to 1, got {ratio!r}")
    if not isinstance(case.get("cost-sheet", False), bool):
        raise ValueError(f"case {name}'s `cost-sheet` must be true or false")

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
    providers = comparator.get("providers", ["substrate"])
    if (
        not isinstance(providers, list)
        or not providers
        or not set(providers) <= set(PROVIDERS)
        or len(set(providers)) != len(providers)
    ):
        raise ValueError(f"case {name}'s `comparator.providers` must be a list of distinct {', '.join(PROVIDERS)}")
    return {
        "sandbox-class": "gvisor",
        "env-worker-count": DEFAULT_ENV_WORKER_COUNT,
        "actor-memory": ate_env.DEFAULT_ACTOR_MEMORY,
        "actor-cpu": ate_env.DEFAULT_ACTOR_CPU,
        "max-failure-ratio": DEFAULT_MAX_FAILURE_RATIO,
        "cost-sheet": False,
        **case,
        "comparator": {"providers": ["substrate"], "warmup-runs": 0, **comparator},
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
        try:
            ended = jobs.job_status(cluster, JOB, NAMESPACE)
        except jobs.StatusUnread:
            ended = None
        if ended:
            stop_logs(10)
            if ended == "Failed":
                # run_comparator.py's exit status says why, and run.json what it counted.
                raise SandboxBenchmarkError(f"{JOB} failed: {jobs.logs_hint(cluster, JOB, NAMESPACE)}")
            log(f"sandbox-benchmark: {JOB} completed: {jobs.logs_hint(cluster, JOB, NAMESPACE)}")
            return
        time.sleep(5)
    raise SandboxBenchmarkTimeout(f"timed out after {WAIT_TIMEOUT_SECS}s; the Job is left running")


def run_provider(
    cluster: Cluster,
    name: str,
    case: dict,
    provider: str,
    run_id: str,
    comparator_image: str,
    image_repo: str,
    deploy_workloads: bool,
    bucket: str,
    report_prefix: str,
    test_case: str | None,
    repeat: int,
    warmup: int,
) -> None:
    """Runs the case's tests on provider, as one comparator Job, whose reports
    go under <run_id>/<name>/<provider>/."""
    keys = PROVIDER_KEYS.get(provider, ())
    missing = [key for key in keys if not os.environ.get(key)]
    if missing:
        raise SandboxBenchmarkError(f"{provider} needs {', '.join(missing)} in the environment")
    sandbox_class = case["sandbox-class"]
    comparator = case["comparator"]
    on_env = provider == "substrate"
    template = None
    if on_env:
        if deploy_workloads:
            ate_env.deploy(
                cluster, image_repo, sandbox_class, case["env-worker-count"], case["actor-memory"], case["actor-cpu"]
            )
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
        cluster, case, ate_env.TEMPLATE if on_env else None, template, {COMPARATOR_IMAGE: comparator_image}, bucket,
        pool=(ate_env.NAMESPACE, ate_env.POOL),
    )  # fmt: skip
    if on_env:
        info["env_commit"] = commit(ate_env.ENV_DIR)
    info["ai_sandbox_benchmark_commit"] = commit(AI_SANDBOX_BENCHMARK_DIR)
    info["warmup"] = warmup or None
    values = {
        "COMPARATOR_IMAGE": comparator_image,
        "BENCHMARK_RUNNER_KSA": cluster.runner_ksa,
        "BENCHMARK_PROVIDER": provider,
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
    if keys:
        # Server-side, so no last-applied-configuration annotation copies the keys.
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": PROVIDER_SECRET, "namespace": NAMESPACE},
            "data": {key: base64.b64encode(os.environ[key].encode()).decode() for key in keys},
        }
        stream(cluster.kubectl("apply", "--server-side", "-f", "-"), input=json.dumps(secret))
    # A Job's pod template is immutable. Foreground, so its pod is gone too and
    # the job-name lookup only sees this run's.
    log(f"sandbox-benchmark: deleting any previous {JOB}")
    stream(
        cluster.kubectl(
            "delete", "job", JOB, "--ignore-not-found", "--cascade=foreground", "--wait", namespace=NAMESPACE
        )
    )
    folder = f"{name}/{provider}"
    log(
        f"sandbox-benchmark: running {name} on {provider} (run {run_id}):\n"
        f"  comparator: {comparator_image}\n"
        f"  tests: {values['BENCHMARK_TESTS']} ({comparator['runs']} runs, {comparator['warmup-runs']} warmup)\n"
        f"  reports: gs://{bucket}/{report_prefix}/{run_id}/{folder}/"
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
        if keys:
            # Best-effort: the pod read it as it started, and a dev infra's
            # cluster outlives the run.
            subprocess.run(
                cluster.kubectl("delete", "secret", PROVIDER_SECRET, "--ignore-not-found", namespace=NAMESPACE),
                capture_output=True,
            )
        if on_env and not still_running:
            reports.record_cleanup(cluster, ate_env.ENV_ATESPACE, bucket, report_prefix, run_id, folder)
        reports.upload_logs(cluster, NAMESPACE, (JOB,), bucket, report_prefix, run_id, folder)


def combine_reports(
    bucket: str, report_prefix: str, run_id: str, name: str, providers: list[str], cost_sheet: bool = False
) -> None:
    """Combines the providers' reports in the bucket into results.json and
    comparison.md beside their folders, as one comparator run of them all
    would have them, and with cost_sheet cost_sheet.csv. Best-effort: each
    provider's own reports are the record either way."""
    prefix = f"{report_prefix}/{run_id}/{name}"
    sources = []
    try:
        bucket_ = storage.Client().bucket(bucket)
        for provider in providers:
            texts = {}
            for file in (combine.RESULTS_JSON, combine.RUN_JSON):
                blob = bucket_.blob(f"{prefix}/{provider}/{file}")
                texts[file] = blob.download_as_text() if blob.exists() else None
            if None in texts.values():
                log(f"sandbox-benchmark: {provider} left out of the combined results: it recorded none", "WARNING")
                continue
            sources.append(
                combine.source(
                    provider,
                    json.loads(texts[combine.RESULTS_JSON]),
                    json.loads(texts[combine.RUN_JSON]),
                    f"gs://{bucket}/{prefix}/{provider}",
                )
            )
        if not sources:
            log("sandbox-benchmark: no provider recorded results to combine", "WARNING")
            return
        combine.check(sources)
        combined = combine.combined_results(sources)
        bucket_.blob(f"{prefix}/{combine.RESULTS_JSON}").upload_from_string(
            json.dumps(combined, indent=2) + "\n", content_type="application/json"
        )
        bucket_.blob(f"{prefix}/{combine.COMPARISON_MD}").upload_from_string(
            combine.render(sources), content_type="text/markdown"
        )
        if cost_sheet:
            bucket_.blob(f"{prefix}/{costs.COST_SHEET_CSV}").upload_from_string(
                costs.csv_text(combined), content_type="text/csv"
            )
    except Exception as exc:
        log(f"sandbox-benchmark: could not combine the providers' results: {type(exc).__name__}: {exc}", "WARNING")
        return
    names = ", ".join(s["name"] for s in sources)
    log(f"sandbox-benchmark: {names} combined in gs://{bucket}/{prefix}/{combine.COMPARISON_MD}")


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
    timestamp and the commit: on each of its providers in turn, then combines
    their results, with the images built from this tree into image_repo. A
    provider whose run fails does not stop the ones after it; the run fails
    once they have run. test_case and repeat say which case it is a repeat of;
    repeat is 0 for a case run once, or a warm-up, which warmup numbers. Without
    deploy_workloads, uses the env and template already deployed. The reports
    go under report_prefix in report_bucket, by default the cluster's own,
    which keeps_bucket says outlives the run, as a dev infra's does. Expects
    the cluster's credentials in its kubeconfig."""
    case = check_case(name, case, deploy_workloads)
    sandbox_class = case["sandbox-class"]
    providers = case["comparator"]["providers"]
    # env's workers would never schedule past the sandboxed pool's taint.
    if "substrate" in providers and sandbox_class != cluster.sandbox_class:
        raise SandboxBenchmarkError(
            f"case {name} is {sandbox_class}, but cluster {cluster.name}'s sandboxed pool is"
            f" {cluster.sandbox_class}; set the cluster's sandbox_class to match"
        )
    run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dt%H%M%Sz}-{images.commit()[:12]}"
    if not reports.LABEL_RE.fullmatch(run_id):
        raise ValueError(f"run id {run_id!r} is not a valid label value")
    bucket = report_bucket or cluster.bucket
    reports.refuse_reused(bucket, report_prefix, run_id, name)
    # By digest, so the Job runs exactly the image built from this tree even
    # if the tag is pushed again meanwhile; run.json records it.
    comparator_image = images.pin(images.image_ref(images.SANDBOX_BENCHMARK_IMAGES, image_repo, COMPARATOR_IMAGE))

    failed = []
    downloaded = None
    try:
        for provider in providers:
            try:
                run_provider(
                    cluster, name, case, provider, run_id, comparator_image, image_repo, deploy_workloads, bucket,
                    report_prefix, test_case, repeat, warmup,
                )  # fmt: skip
            except SandboxBenchmarkTimeout:
                raise
            except (SandboxBenchmarkError, ate_env.EnvError, workloads.WorkloadError,
                    subprocess.CalledProcessError) as exc:  # fmt: skip
                reason = failure(exc) if isinstance(exc, subprocess.CalledProcessError) else str(exc)
                log(f"sandbox-benchmark: {name} on {provider} failed: {reason}", "ERROR")
                failed.append(provider)
        combine_reports(bucket, report_prefix, run_id, name, providers, case["cost-sheet"])
    finally:
        # A failed run's reports are kept too. On Cloud Run they'd go with the
        # container, so they stay only in the bucket.
        if ON_CLOUD_RUN:
            log(
                f"sandbox-benchmark: on Cloud Run, so the reports stay only in gs://{bucket}/{report_prefix}/{run_id}/{name}/"
            )
        else:
            downloaded = reports.download_reports(bucket, report_prefix, run_id, name)
    if downloaded is False and report_bucket is None and keeps_bucket:
        log(f"sandbox-benchmark: the reports stay in gs://{bucket}/{report_prefix}/{run_id}/{name}/", "WARNING")
    # The case's own bucket goes with its cluster, so the local copy is the
    # only one; without it the run recorded nothing that lasts.
    elif downloaded is False and report_bucket is None:
        raise SandboxBenchmarkError(
            f"the reports of {name} could not be downloaded from gs://{bucket}/{report_prefix}/{run_id}/{name}/, which"
            " is destroyed with the cluster; pass --report-bucket to keep them in a bucket of your own"
        )
    if failed:
        raise SandboxBenchmarkError(f"{name} failed on {', '.join(failed)} of {', '.join(providers)}")
