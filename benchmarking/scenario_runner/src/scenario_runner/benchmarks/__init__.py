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

"""The case types, each run by the module its `type` names."""

from __future__ import annotations

from pathlib import Path

from .. import images, reports, workloads
from ..infra.cluster import Cluster
from ..infra.cluster_configs import ClusterConfig
from ..log import log
from . import locust, monitoring, noop, sandbox_benchmark
from .sandbox_benchmark import ate_env

RUNNERS = {module.TYPE: module for module in (locust, noop, sandbox_benchmark)}
# What a failed run raises besides CalledProcessError.
ERRORS = (
    locust.LocustError,
    sandbox_benchmark.SandboxBenchmarkError,
    ate_env.EnvError,
    reports.ReportError,
    workloads.WorkloadError,
    images.ImageError,
    monitoring.MonitoringError,
)
# A run that may still be going, which a series on a shared cluster stops at.
STILL_RUNNING = (locust.LocustStillRunning, sandbox_benchmark.SandboxBenchmarkTimeout)
# The types whose repeats summary.write can summarize.
SUMMARIZED = {"locust"}


def types() -> list[str]:
    return sorted(RUNNERS)


def check(name: str, case: dict) -> None:
    """Raises ValueError for a case its type can't run."""
    RUNNERS[case["type"]].check_case(name, case, deploy_workloads=True)


def cluster_config(name: str, case: dict) -> ClusterConfig:
    """The cluster the case runs on, as its type asks for it."""
    return RUNNERS[case["type"]].cluster_config(name, case)


def image_files(case: dict) -> list[Path]:
    """The images.json files of the images the case's runs pull."""
    return RUNNERS[case["type"]].IMAGE_FILES


def missing_requirements(cases: dict[str, dict]) -> list[str]:
    missing = [tool for case in cases.values() for tool in RUNNERS[case["type"]].missing_requirements()]
    return list(dict.fromkeys(missing))


def run(name: str, case: dict, run_id: str, cluster: Cluster, **options: object) -> None:
    """options are those of its type's run, e.g. locust.run's image_repo, and
    test_case and repeat, or warmup, for one of a case's repeats."""
    if case["type"] == noop.TYPE:
        log(f"{name}: noop, nothing to run")
        return
    cluster.get_credentials()
    log(f"{name}: running as {run_id} on cluster {cluster.name}")
    # First, so it is watching before the run deploys anything to watch.
    monitoring.deploy(cluster)
    RUNNERS[case["type"]].run(cluster, name, case, run_id, **options)


def summarize(runs: list[tuple[str, str, dict]], run_id: str, bucket: str | None, report_prefix: str) -> None:
    """What a case's type does once all of it has run, its subcases too, if
    it does anything: runs are each record of each, its case's name and the
    case. bucket is the one they all reported to, if it is still there."""
    summarize = getattr(RUNNERS[runs[0][2]["type"]], "summarize", None)
    if summarize:
        summarize(runs, run_id, bucket, report_prefix)
