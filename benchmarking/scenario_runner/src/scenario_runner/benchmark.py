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

"""Runs a case on its cluster with the module its `type` names."""

from __future__ import annotations

import argparse

from . import ate_env, images, locust, monitoring, reports, sandbox_benchmark, workloads
from .cluster import Cluster
from .log import log

RUNNERS = {"locust": locust, "sandbox-benchmark": sandbox_benchmark}
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


def check_run_id(run_id: str) -> str:
    # The runs' pods are labeled with it.
    if not reports.LABEL_RE.fullmatch(run_id):
        raise argparse.ArgumentTypeError(
            f"{run_id!r}: use at most 63 letters, digits, '-', '_' and '.', starting and ending with a letter or digit"
        )
    return run_id


def missing_requirements(cases: dict[str, dict]) -> list[str]:
    missing = [tool for case in cases.values() for tool in RUNNERS[case["type"]].missing_requirements()]
    return list(dict.fromkeys(missing))


def run(name: str, case: dict, run_id: str, cluster: Cluster, **options: object) -> None:
    """options are those of its type's run, e.g. locust.run's image_repo, and
    test_case and repeat, or warmup, for one of a case's repeats."""
    cluster.get_credentials()
    log(f"{name}: running as {run_id} on cluster {cluster.name}")
    # First, so it is watching before the run deploys anything to watch.
    monitoring.deploy(cluster)
    RUNNERS[case["type"]].run(cluster, name, case, run_id, **options)
