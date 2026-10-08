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
hack/install-ate.sh --deploy-ate-system, configured from the cluster's
Terraform outputs."""

from __future__ import annotations

import shutil
import subprocess

from . import RUNNER_DIR
from .cluster import Cluster
from .log import log
from .process import output, stream

SUBSTRATE_DIR = RUNNER_DIR.parent.parent / "dependency-repos" / "substrate"


def revision() -> str:
    """The submodule's short commit, which every failure is explained against."""
    proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=SUBSTRATE_DIR, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else "unknown revision"


def ko(cluster: Cluster, *args: str, input: str) -> str:
    """Runs substrate's ko with input on its stdin and returns its stdout. Only
    in the submodule do its ko:// references resolve."""
    # ko has no flag for the registry. The platform is the nodes', whatever the
    # build host's.
    return output(
        [str(SUBSTRATE_DIR / "hack" / "run-tool.sh"), "ko", *args],
        env={"KO_DOCKER_REPO": cluster.docker_repo, "KO_DEFAULTPLATFORMS": "linux/amd64"},
        cwd=SUBSTRATE_DIR,
        input=input,
    )


def missing_submodule() -> list[str]:
    return [] if (SUBSTRATE_DIR / "go.mod").is_file() else [f"the substrate submodule in {SUBSTRATE_DIR}"]


def missing_requirements() -> list[str]:
    return [tool for tool in ("go", "gcloud") if not shutil.which(tool)] + missing_submodule()


def install(cluster: Cluster) -> None:
    env = {
        "PROJECT_ID": cluster.project_id,
        "CLUSTER_NAME": cluster.name,
        "CLUSTER_LOCATION": cluster.location,
        "BUCKET_NAME": cluster.bucket,
        "KO_DOCKER_REPO": cluster.docker_repo,
        # The nodes' platform, whatever the build host's.
        "KO_DEFAULTPLATFORMS": "linux/amd64",
        # Empty, so the installer runs get-credentials itself and uses the
        # context it writes into KUBECONFIG, rather than the developer's own.
        "KUBECTL_CONTEXT": "",
        "KUBECONFIG": str(cluster.kubeconfig),
        # A .ate-dev-env.sh in the submodule would name another cluster.
        "NO_DEV_ENV": "1",
    }
    log(f"substrate: installing on cluster {cluster.name}")
    stream([str(SUBSTRATE_DIR / "hack" / "install-ate.sh"), "--deploy-ate-system"], env=env, cwd=SUBSTRATE_DIR)
