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
from pathlib import Path

from . import AUTOMATION_DIR, terraform
from .log import log

ROOT = AUTOMATION_DIR.parent.parent
RUN_BENCHMARK = ROOT / "benchmarking" / "run_benchmark.sh"


def missing_requirements() -> list[str]:
    tools = ["kubectl", "jq", "envsubst", "go", "gcloud"]
    # lib.sh builds the images with docker unless on Cloud Build.
    if os.environ.get("USE_CLOUD_BUILD") != "1":
        tools.append("docker")
    return [tool for tool in tools if not shutil.which(tool)]


def get_credentials(outputs: dict, kubeconfig: Path) -> None:
    terraform.stream(
        [
            "gcloud",
            "container",
            "clusters",
            "get-credentials",
            outputs["cluster_name"],
            # As .terraform-env.sh has it: the cluster is regional, in the region.
            f"--location={outputs['region']}",
            f"--project={outputs['project_id']}",
        ],
        log,
        env={"KUBECONFIG": str(kubeconfig)},
    )


def run(name: str, case: dict, run_id: str, outputs: dict, kubeconfig: Path) -> None:
    """Runs the case on the cluster of a common config's outputs, reaching it
    with kubeconfig."""
    get_credentials(outputs, kubeconfig)
    # What .env.sh would export for the cluster.
    env = {
        "BENCHMARK_ENV_PROVIDED": "1",
        "PROJECT_ID": outputs["project_id"],
        "GCE_REGION": outputs["region"],
        "CLUSTER_LOCATION": outputs["region"],
        "CLUSTER_NAME": outputs["cluster_name"],
        "BUCKET_NAME": outputs["bucket_name"],
        # The cluster's own repository, which its nodes are granted to pull from.
        "DOCKER_REPO": outputs["artifact_registry_docker_repo"],
        "KO_DOCKER_REPO": outputs["artifact_registry_docker_repo"],
        "KO_DEFAULTPLATFORMS": "linux/amd64",
        "KUBECTL_CONTEXT": "",
        "KUBECONFIG": str(kubeconfig),
    }
    log(f"{name}: running as {run_id} on cluster {outputs['cluster_name']}")
    terraform.stream(
        [str(RUN_BENCHMARK), "--test-case-name", name, "--test-case", json.dumps(case), "--run-id", run_id],
        log,
        env=env,
        cwd=ROOT,
    )
