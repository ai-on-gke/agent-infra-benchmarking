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

from . import AUTOMATION_DIR
from .cluster import Cluster
from .log import log
from .process import stream

SUBSTRATE_DIR = AUTOMATION_DIR.parent.parent / "dependency-repos" / "substrate"


def missing_requirements() -> list[str]:
    missing = [tool for tool in ("go", "gcloud") if not shutil.which(tool)]
    if not (SUBSTRATE_DIR / "go.mod").is_file():
        missing.append(f"the substrate submodule in {SUBSTRATE_DIR}")
    return missing


def install(cluster: Cluster) -> None:
    # With KUBECTL_CONTEXT empty, the installer runs get-credentials itself.
    env = {
        **cluster.env(),
        # A .ate-dev-env.sh in the submodule would name another cluster.
        "NO_DEV_ENV": "1",
    }
    log(f"substrate: installing on cluster {cluster.name}")
    stream([str(SUBSTRATE_DIR / "hack" / "install-ate.sh"), "--deploy-ate-system"], env=env, cwd=SUBSTRATE_DIR)
