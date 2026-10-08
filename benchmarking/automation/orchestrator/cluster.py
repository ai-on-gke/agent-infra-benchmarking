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

"""A case's applied cluster, as the scripts run on it are configured for it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Cluster:
    """The cluster of a common config's outputs, its credentials going in
    kubeconfig."""

    outputs: dict
    kubeconfig: Path

    @property
    def name(self) -> str:
        return self.outputs["cluster_name"]

    @property
    def location(self) -> str:
        # As .terraform-env.sh has it: the cluster is regional, in the region.
        return self.outputs["region"]

    def env(self) -> dict[str, str]:
        """What .env.sh would export for the cluster."""
        return {
            "PROJECT_ID": self.outputs["project_id"],
            "CLUSTER_NAME": self.name,
            "CLUSTER_LOCATION": self.location,
            "BUCKET_NAME": self.outputs["bucket_name"],
            "KO_DOCKER_REPO": self.outputs["artifact_registry_docker_repo"],
            "KO_DEFAULTPLATFORMS": "linux/amd64",
            # Empty, so the scripts use kubeconfig's current context, the one
            # get-credentials writes, rather than the developer's own.
            "KUBECTL_CONTEXT": "",
            "KUBECONFIG": str(self.kubeconfig),
        }
