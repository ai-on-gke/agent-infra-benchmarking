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

"""Runs a case on its cluster with benchmarking/run_benchmark.sh, configured
from the cluster's Terraform outputs rather than from .env.sh."""

from __future__ import annotations

import json
import os
import shutil

from . import AUTOMATION_DIR
from .cluster import Cluster
from .log import log
from .process import stream

ROOT = AUTOMATION_DIR.parent.parent
RUN_BENCHMARK = ROOT / "benchmarking" / "run_benchmark.sh"


def missing_requirements() -> list[str]:
    tools = ["kubectl", "jq", "envsubst", "go", "gcloud"]
    # lib.sh builds the images with docker unless on Cloud Build.
    if os.environ.get("USE_CLOUD_BUILD") != "1":
        tools.append("docker")
    return [tool for tool in tools if not shutil.which(tool)]


def get_credentials(cluster: Cluster) -> None:
    stream(
        [
            "gcloud",
            "container",
            "clusters",
            "get-credentials",
            cluster.name,
            f"--location={cluster.location}",
            f"--project={cluster.outputs['project_id']}",
        ],
        env={"KUBECONFIG": str(cluster.kubeconfig)},
    )


def run(name: str, case: dict, run_id: str, cluster: Cluster) -> None:
    get_credentials(cluster)
    env = {
        **cluster.env(),
        "BENCHMARK_ENV_PROVIDED": "1",
        "GCE_REGION": cluster.outputs["region"],
        # The cluster's own repository, which its nodes are granted to pull from.
        "DOCKER_REPO": cluster.outputs["artifact_registry_docker_repo"],
    }
    log(f"{name}: running as {run_id} on cluster {cluster.name}")
    stream(
        [str(RUN_BENCHMARK), "--test-case-name", name, "--test-case", json.dumps(case), "--run-id", run_id],
        env=env,
        cwd=ROOT,
    )
