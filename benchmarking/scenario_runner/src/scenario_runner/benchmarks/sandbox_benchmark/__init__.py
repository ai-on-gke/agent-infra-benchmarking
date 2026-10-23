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
provider, as a run of the comparator Job of
sandbox-benchmark/manifests/comparator-job.yaml. The case's `sandbox-benchmark`
names the provider, the tests by function name, how many times each runs and
how many warmup runs go before. The substrate provider runs them through
agent-substrate/env deployed for them by ate_env, whose pool and environments
its `env-worker-count`, `actor-memory` and `actor-cpu` size; local runs them in
the comparator's own pod; e2b, daytona, modal, anthropic and openai in their
clouds, with the PROVIDER_KEYS of the scenario-runner's environment, e2b's,
modal's and daytona's sandboxes sized by their `cpu` and `memory`, openai's by
its `container-size` tier. A case's
variants, each typically another provider, report side by side, and summarize
combines their results into those of one comparator run of them all."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Callable

from google.cloud import storage

from ... import LABEL_RE, RUNNER_DIR, git, images, reports, workloads
from ...infra.cluster import Cluster
from ...infra.cluster_configs import CommonCluster, from_infra
from ...log import ON_CLOUD_RUN, log
from ...process import stream
from ...reports import run_info
from ...scenarios.install import INSTALL, SANDBOX_CLASS, SUBSTRATE, control_plane, sandbox_class
from ...scenarios.variants import VARIANT, group_record
from .. import jobs
from . import ate_env, combine
from . import cost_sheet as costs

MANIFEST = RUNNER_DIR.parent / "sandbox-benchmark" / "manifests" / "comparator-job.yaml"
AI_SANDBOX_BENCHMARK_DIR = ate_env.DEPENDENCY_REPOS / "ai-sandbox-benchmark"
# Have to match metadata.name and namespace in the manifest.
JOB = "sandbox-benchmark-comparator"
NAMESPACE = jobs.NAMESPACE
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
# The case's mapping of its type, and what it may state.
TYPE = "sandbox-benchmark"
KEYS = {"infra", INSTALL, "provider", "tests", "runs", "warmup-runs", "max-failure-ratio", "cost-sheet"}
# The images.json files of the images a run pulls.
IMAGE_FILES = [images.SANDBOX_BENCHMARK_IMAGES]
PROVIDERS = ("substrate", "local", "e2b", "daytona", "modal", "anthropic", "openai")
# What a provider's mapping may state besides its name, and their defaults.
PROVIDER_OPTIONS = {
    "substrate": {
        "env-worker-count": DEFAULT_ENV_WORKER_COUNT,
        "actor-memory": ate_env.DEFAULT_ACTOR_MEMORY,
        "actor-cpu": ate_env.DEFAULT_ACTOR_CPU,
    },
    # The sandbox's size, the provider's own when unset; e2b's in whole vCPUs,
    # daytona's in whole vCPUs and GiB.
    "e2b": {"cpu": None, "memory": None},
    "modal": {"cpu": None, "memory": None},
    "daytona": {"cpu": None, "memory": None},
    # One of CONTAINER_SIZES, OpenAI's default when unset; see
    # https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted
    "openai": {"container-size": None},
}
CONTAINER_SIZES = ("small", "medium", "large")
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


def provider_name(case: dict) -> str:
    """The case's provider, checked or not."""
    provider = (case.get(TYPE) or {}).get("provider")
    return provider.get("name", "substrate") if isinstance(provider, dict) else "substrate"


def check_size(where: str, provider: dict) -> dict:
    """e2b's, modal's or daytona's provider with its `cpu` and `memory` checked."""
    cpu, memory = provider["cpu"], provider["memory"]
    daytona = provider["name"] == "daytona"
    if cpu is not None and (not isinstance(cpu, str) or not workloads.CPU_RE.fullmatch(cpu)):
        raise ValueError(f"{where}.cpu must be a quoted cpu quantity such as \"2\" or \"1500m\", got {cpu!r}")
    if cpu is not None and provider["name"] != "modal" and not workloads.cpu_cores(cpu).is_integer():
        raise ValueError(f"{where}.cpu must be whole vCPUs on {provider['name']}, got {cpu!r}")
    if memory is not None:
        mib = workloads.memory_mib(memory) if isinstance(memory, str) else None
        unit, step = ("GiB", 1024) if daytona else ("MiB", 1)
        if mib is None or mib % step:
            raise ValueError(f"{where}.memory must be a quantity of whole {unit} such as 2Gi, got {memory!r}")
    return provider


def check_provider(name: str, provider: object) -> dict:
    """The case's `provider` with its options' defaults filled in."""
    where = f"case {name}'s `{TYPE}.provider`"
    if not isinstance(provider, dict):
        raise ValueError(f"{where} must be a mapping of its `name` and options, such as {{name: e2b}}")
    if provider.get("name") not in PROVIDERS:
        raise ValueError(f"{where}.name must be one of {', '.join(PROVIDERS)}")
    options = PROVIDER_OPTIONS.get(provider["name"], {})
    unknown = provider.keys() - {"name"} - options.keys()
    if unknown:
        takes = f"takes only {', '.join(options)}" if options else "takes no options"
        raise ValueError(f"{where} has unknown {', '.join(sorted(unknown))}; {provider['name']} {takes}")
    provider = {**options, **provider}
    if provider["name"] in ("e2b", "modal", "daytona"):
        return check_size(where, provider)
    if provider["name"] == "openai" and provider["container-size"] not in (None, *CONTAINER_SIZES):
        raise ValueError(f"{where}.container-size must be one of {', '.join(CONTAINER_SIZES)}")
    if provider["name"] != "substrate":
        return provider
    count = provider["env-worker-count"]
    if not whole(count, 1):
        raise ValueError(f"{where}.env-worker-count must be a whole number from 1, got {count!r}")
    memory = provider["actor-memory"]
    if not isinstance(memory, str) or workloads.memory_mib(memory) is None:
        raise ValueError(f"{where}.actor-memory must be a Kubernetes quantity such as 1Gi, got {memory!r}")
    cpu = provider["actor-cpu"]
    if not isinstance(cpu, str) or not workloads.CPU_RE.fullmatch(cpu):
        raise ValueError(f"{where}.actor-cpu must be a quoted cpu quantity such as \"2\" or \"1500m\", got {cpu!r}")
    return provider


def check_case(name: str, case: dict, deploy_workloads: bool) -> dict:
    """The case with its defaults filled in."""
    if not LABEL_RE.fullmatch(name):
        raise ValueError(f"case name {name!r} is not a valid label value")
    options = case.get(TYPE)
    if not isinstance(options, dict):
        raise ValueError(f"case {name} has no `{TYPE}` mapping")
    unknown = options.keys() - KEYS
    if unknown:
        raise ValueError(f"case {name}'s `{TYPE}` has unknown {', '.join(sorted(unknown))}")
    ratio = options.get("max-failure-ratio", DEFAULT_MAX_FAILURE_RATIO)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
        raise ValueError(f"case {name}'s `{TYPE}.max-failure-ratio` must be a number from 0 to 1, got {ratio!r}")
    if not isinstance(options.get("cost-sheet", False), bool):
        raise ValueError(f"case {name}'s `{TYPE}.cost-sheet` must be true or false")
    tests = options.get("tests")
    if not isinstance(tests, list) or not tests or not all(isinstance(t, str) for t in tests):
        raise ValueError(f"case {name}'s `{TYPE}.tests` must be a list of test function names")
    known = test_names()
    if known is not None and set(tests) - known:
        raise ValueError(
            f"case {name}'s `{TYPE}.tests` has unknown {', '.join(sorted(set(tests) - known))};"
            f" there are {', '.join(sorted(known))}"
        )
    if not whole(options.get("runs"), 1):
        raise ValueError(f"case {name}'s `{TYPE}.runs` must be a whole number from 1")
    if not whole(options.get("warmup-runs", 0), 0):
        raise ValueError(f"case {name}'s `{TYPE}.warmup-runs` must be a whole number")
    provider = check_provider(name, options.get("provider", {"name": "substrate"}))
    # env runs on substrate, which the cluster has only with it.
    if provider["name"] == "substrate" and control_plane(case) != SUBSTRATE:
        raise ValueError(f"case {name}'s substrate provider needs `{TYPE}.{INSTALL}.{SUBSTRATE}.{SANDBOX_CLASS}`")
    cluster_config(name, case)
    return {
        **case,
        TYPE: {
            "warmup-runs": 0,
            "max-failure-ratio": DEFAULT_MAX_FAILURE_RATIO,
            "cost-sheet": False,
            **options,
            "provider": provider,
        },
    }


def cluster_config(name: str, case: dict) -> CommonCluster:
    """The cluster the case runs on, its comparator Job whatever its
    provider: its `sandbox-benchmark.infra.cluster`."""
    return from_infra(name, f"{TYPE}.infra", case[TYPE].get("infra"))


def report_folder(record: str, key: str, case: dict) -> str:
    """Where under the run id the run of case key recorded as record reports:
    in <record>/<provider>/, or a variant's in <group record>/<variant>/,
    beside the others of its group."""
    if VARIANT in case:
        return f"{group_record(record, key, case)}/{case[VARIANT]['name']}"
    return f"{record}/{provider_name(case)}"


def commit(path: Path) -> str | None:
    try:
        return git.head(path)
    except subprocess.CalledProcessError:
        return None


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
    run_id: str,
    folder: str,
    comparator_image: str,
    image_repo: str,
    deploy_workloads: bool,
    bucket: str,
    report_prefix: str,
    test_case: str | None,
    repeat: int,
    warmup: int,
) -> None:
    """Runs the case's tests on its provider, as one comparator Job, whose
    reports go under <run_id>/<folder>/."""
    options = case[TYPE]
    provider = options["provider"]["name"]
    cpu, memory = options["provider"].get("cpu"), options["provider"].get("memory")
    keys = PROVIDER_KEYS.get(provider, ())
    missing = [key for key in keys if not os.environ.get(key)]
    if missing:
        raise SandboxBenchmarkError(f"{provider} needs {', '.join(missing)} in the environment")
    on_env = provider == "substrate"
    template = None
    if on_env:
        if deploy_workloads:
            env = options["provider"]
            ate_env.deploy(
                cluster, image_repo, sandbox_class(case), env["env-worker-count"], env["actor-memory"], env["actor-cpu"]
            )
        template = workloads.deployed_template(cluster, ate_env.TEMPLATE, ate_env.ENV_ATESPACE)
        # Matters when the template was not deployed by this run (--no-deploy-workloads).
        want = workloads.SANDBOX_CONFIGS[sandbox_class(case)][1]
        have = (template.get("sandboxConfig") or {}).get("sandboxClass")
        if have != want:
            raise SandboxBenchmarkError(
                f"actor template {ate_env.ENV_ATESPACE}/{ate_env.TEMPLATE} is {have or 'of no sandbox class'},"
                f" not {want}; deploy it again"
            )

    info = run_info.collect(
        cluster, case, cluster_config(name, case), ate_env.TEMPLATE if on_env else None, template, {COMPARATOR_IMAGE: comparator_image}, bucket,
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
        "BENCHMARK_TESTS": ",".join(options["tests"]),
        "BENCHMARK_RUNS": str(options["runs"]),
        "BENCHMARK_WARMUP_RUNS": str(options["warmup-runs"]),
        "MAX_FAILURE_RATIO": str(options["max-failure-ratio"]),
        "BENCHMARK_RUN_ID": run_id,
        "BENCHMARK_RECORD_NAME": name,
        "BENCHMARK_CASE": test_case or name,
        "BENCHMARK_REPEAT": str(repeat),
        # What run_comparator.py writes into run.json; base64, as it is JSON in YAML.
        "BENCHMARK_RUN_INFO": base64.b64encode(json.dumps(info).encode()).decode(),
        "BENCHMARK_REPORT_BUCKET": bucket,
        "BENCHMARK_REPORT_PREFIX": report_prefix,
        "BENCHMARK_REPORT_FOLDER": folder,
        "SUBSTRATE_ENV_API_ADDR": ate_env.API_ADDR,
        "SUBSTRATE_ATESPACE": ate_env.ENV_ATESPACE,
        "SUBSTRATE_TEMPLATE": f"{ate_env.ENV_ATESPACE}/{ate_env.TEMPLATE}",
        "SANDBOX_CPU": str(workloads.cpu_cores(cpu)) if cpu else "",
        "SANDBOX_MEMORY_MIB": str(int(workloads.memory_mib(memory))) if memory else "",
        "OPENAI_CONTAINER_SIZE": options["provider"].get("container-size") or "",
    }

    cluster.apply_namespace(NAMESPACE)
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
    log(
        f"sandbox-benchmark: running {name} on {provider} (run {run_id}):\n"
        f"  comparator: {comparator_image}\n"
        f"  tests: {values['BENCHMARK_TESTS']} ({options['runs']} runs, {options['warmup-runs']} warmup)\n"
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
    bucket: str | None, report_prefix: str, run_id: str, name: str, labels: list[str], cost_sheet: bool = False
) -> None:
    """Combines the reports in name's folders labels into perf_results.csv
    beside them, as one comparator run of them all would have them, and with
    cost_sheet cost_sheet.csv: read from and written to bucket,
    or without one the downloaded reports, and written to those too unless on
    Cloud Run. Best-effort: each run's own reports are the record either way."""
    prefix = f"{report_prefix}/{run_id}/{name}"
    local = reports.REPORTS_DIR / run_id / name
    if not bucket and ON_CLOUD_RUN:
        log(f"sandbox-benchmark: {name}'s reports are in no bucket left to combine them from", "WARNING")
        return
    sources = []
    try:
        bucket_ = storage.Client().bucket(bucket) if bucket else None
        for label in labels:
            texts = {}
            for file in (combine.RESULTS_JSON, combine.RUN_JSON):
                if bucket_:
                    blob = bucket_.blob(f"{prefix}/{label}/{file}")
                    texts[file] = blob.download_as_text() if blob.exists() else None
                else:
                    path = local / label / file
                    texts[file] = path.read_text() if path.is_file() else None
            if None in texts.values():
                log(f"sandbox-benchmark: {label} left out of the combined results: it recorded none", "WARNING")
                continue
            sources.append(
                combine.source(
                    label,
                    json.loads(texts[combine.RESULTS_JSON]),
                    json.loads(texts[combine.RUN_JSON]),
                    f"gs://{bucket}/{prefix}/{label}" if bucket_ else str(local / label),
                )
            )
        if not sources:
            log(f"sandbox-benchmark: nothing in {name} recorded results to combine", "WARNING")
            return
        combine.check(sources)
        files = {combine.PERF_RESULTS_CSV: combine.csv_text(combine.perf_results(sources))}
        if cost_sheet:
            files[costs.COST_SHEET_CSV] = combine.csv_text(costs.sheet(sources))
        for file, text in files.items():
            if bucket_:
                bucket_.blob(f"{prefix}/{file}").upload_from_string(text, content_type="text/csv")
            if not ON_CLOUD_RUN:
                local.mkdir(parents=True, exist_ok=True)
                (local / file).write_text(text)
    except Exception as exc:
        log(f"sandbox-benchmark: could not combine {name}'s results: {type(exc).__name__}: {exc}", "WARNING")
        return
    where = f"gs://{bucket}/{prefix}" if bucket else str(local)
    log(f"sandbox-benchmark: {', '.join(s['name'] for s in sources)} combined in {where}/{combine.PERF_RESULTS_CSV}")


def summarize(runs: list[tuple[str, str, dict]], run_id: str, bucket: str | None, report_prefix: str) -> None:
    """Combines the runs' results, each of one provider, by the folder they
    report beside one another in: a variant's with the others of its group, the
    first the one the others are compared with, under the group's record."""
    folders = {}
    for record, key, case in runs:
        name, label = report_folder(record, key, case).rsplit("/", 1)
        folder = folders.setdefault(name, {"labels": [], "cost-sheet": (case.get(TYPE) or {}).get("cost-sheet", False)})
        folder["labels"].append(label)
    for name, folder in folders.items():
        combine_reports(bucket, report_prefix, run_id, name, folder["labels"], folder["cost-sheet"])


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
    timestamp and the commit, on its provider, with the images built from this
    tree into image_repo. test_case and repeat say which case it is a repeat
    of; repeat is 0 for a case run once, or a warm-up, which warmup numbers.
    Without deploy_workloads, uses the env and template already deployed. The
    reports go under report_prefix in report_bucket, by default the cluster's
    own, which keeps_bucket says outlives the run, as a dev infra's does, in
    report_folder. Expects the cluster's credentials in its kubeconfig."""
    folder = report_folder(name, test_case or name, case)
    case = check_case(name, case, deploy_workloads)
    if case[TYPE]["provider"]["name"] == "substrate" and sandbox_class(case) == "microvm" and not cluster.sandboxed_kvm:
        raise SandboxBenchmarkError(
            f"case {name} is microvm, but cluster {cluster.name}'s sandboxed pool has no /dev/kvm for its guests"
        )
    run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dt%H%M%Sz}-{images.commit()[:12]}"
    if not LABEL_RE.fullmatch(run_id):
        raise ValueError(f"run id {run_id!r} is not a valid label value")
    bucket = report_bucket or cluster.bucket
    reports.refuse_reused(bucket, report_prefix, run_id, folder)
    # By digest, so the Job runs exactly the image built from this tree even
    # if the tag is pushed again meanwhile; run.json records it.
    comparator_image = images.pin(images.image_ref(images.SANDBOX_BENCHMARK_IMAGES, image_repo, COMPARATOR_IMAGE))

    downloaded = None
    try:
        run_provider(
            cluster, name, case, run_id, folder, comparator_image, image_repo, deploy_workloads, bucket,
            report_prefix, test_case, repeat, warmup,
        )  # fmt: skip
    finally:
        # A failed run's reports are kept too. On Cloud Run they'd go with the
        # container, so they stay only in the bucket.
        if ON_CLOUD_RUN:
            log(
                f"sandbox-benchmark: on Cloud Run, so the reports stay only in gs://{bucket}/{report_prefix}/{run_id}/{folder}/"
            )
        else:
            downloaded = reports.download_reports(bucket, report_prefix, run_id, folder)
    if downloaded is False and report_bucket is None and keeps_bucket:
        log(f"sandbox-benchmark: the reports stay in gs://{bucket}/{report_prefix}/{run_id}/{folder}/", "WARNING")
    # The case's own bucket goes with its cluster, so the local copy is the
    # only one; without it the run recorded nothing that lasts.
    elif downloaded is False and report_bucket is None:
        raise SandboxBenchmarkError(
            f"the reports of {name} could not be downloaded from gs://{bucket}/{report_prefix}/{run_id}/{folder}/, which"
            " is destroyed with the cluster; pass --report-bucket to keep them in a bucket of your own"
        )
