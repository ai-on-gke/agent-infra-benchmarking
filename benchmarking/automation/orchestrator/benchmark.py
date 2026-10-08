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

"""Runs a case on its cluster with the benchmarking/<type>/run_benchmark.sh its
`type` names, configured from the cluster's Terraform outputs."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import AUTOMATION_DIR
from .cluster import Cluster
from .log import log
from .process import stream

ROOT = AUTOMATION_DIR.parent.parent
BENCHMARKING_DIR = ROOT / "benchmarking"


def script(case_type: str) -> Path:
    return BENCHMARKING_DIR / case_type / "run_benchmark.sh"


def types() -> list[str]:
    """The types a case can run as, one per benchmarking/<type>/run_benchmark.sh."""
    return sorted(p.parent.name for p in BENCHMARKING_DIR.glob("*/run_benchmark.sh") if os.access(p, os.X_OK))


def missing_requirements() -> list[str]:
    tools = ["kubectl", "jq", "envsubst", "go", "gcloud"]
    # lib.sh builds the images with docker unless on Cloud Build.
    if os.environ.get("USE_CLOUD_BUILD") != "1":
        tools.append("docker")
    return [tool for tool in tools if not shutil.which(tool)]


def run(name: str, case: dict, run_id: str, cluster: Cluster) -> None:
    cluster.get_credentials()
    log(f"{name}: running as {run_id} on cluster {cluster.name}")
    stream(
        [str(script(case["type"])), "--test-case-name", name, "--test-case", json.dumps(case), "--run-id", run_id],
        env=cluster.env(),
        cwd=ROOT,
    )
