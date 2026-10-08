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

"""Installs substrate on a case's cluster with the submodule's
hack/install-ate.sh --deploy-ate-system, as README.md's setup does, configured
from the cluster's Terraform outputs rather than from .env.sh."""

from __future__ import annotations

import shutil
from pathlib import Path

from . import AUTOMATION_DIR, terraform
from .log import log

SUBSTRATE_DIR = AUTOMATION_DIR.parent.parent / "dependency-repos" / "substrate"


def missing_requirements() -> list[str]:
    missing = [tool for tool in ("go", "gcloud") if not shutil.which(tool)]
    if not (SUBSTRATE_DIR / "go.mod").is_file():
        missing.append(f"the substrate submodule in {SUBSTRATE_DIR}")
    return missing


def install(outputs: dict, kubeconfig: Path) -> None:
    """Installs substrate on the cluster of a common config's outputs, its
    credentials going in kubeconfig."""
    env = {
        # The developer's .ate-dev-env.sh names their own cluster.
        "NO_DEV_ENV": "1",
        "PROJECT_ID": outputs["project_id"],
        "CLUSTER_NAME": outputs["cluster_name"],
        # As .terraform-env.sh has it: the cluster is regional, in the region.
        "CLUSTER_LOCATION": outputs["region"],
        "BUCKET_NAME": outputs["bucket_name"],
        "KO_DOCKER_REPO": outputs["artifact_registry_docker_repo"],
        "KO_DEFAULTPLATFORMS": "linux/amd64",
        # Empty, so the installer runs get-credentials, which writes the
        # cluster's context into kubeconfig rather than the developer's own.
        "KUBECTL_CONTEXT": "",
        "KUBECONFIG": str(kubeconfig),
    }
    log(f"substrate: installing on cluster {outputs['cluster_name']}")
    terraform.stream(
        [str(SUBSTRATE_DIR / "hack" / "install-ate.sh"), "--deploy-ate-system"], log, env=env, cwd=SUBSTRATE_DIR
    )
