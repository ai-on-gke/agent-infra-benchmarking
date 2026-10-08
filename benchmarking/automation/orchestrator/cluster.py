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

"""A case's applied cluster, and the settings the benchmarking scripts and
substrate's installer read for it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .log import log
from .process import stream


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
        # The cluster is regional, in the region.
        return self.outputs["region"]

    @property
    def project_id(self) -> str:
        return self.outputs["project_id"]

    @property
    def bucket(self) -> str:
        return self.outputs["bucket_name"]

    @property
    def docker_repo(self) -> str:
        # The cluster's own repository, which its nodes are granted to pull from.
        return self.outputs["artifact_registry_docker_repo"]

    def env(self) -> dict[str, str]:
        return {
            "PROJECT_ID": self.project_id,
            "CLUSTER_NAME": self.name,
            "CLUSTER_LOCATION": self.location,
            "BUCKET_NAME": self.bucket,
            "DOCKER_REPO": self.docker_repo,
            "KO_DOCKER_REPO": self.docker_repo,
            # The nodes' platform, whatever the build host's.
            "KO_DEFAULTPLATFORMS": "linux/amd64",
            # Empty, so the scripts use kubeconfig's current context, the one
            # get-credentials writes, rather than the developer's own.
            "KUBECTL_CONTEXT": "",
            "KUBECONFIG": str(self.kubeconfig),
        }

    def get_credentials(self, log_line: Callable[[str], None] = log) -> None:
        """Writes the cluster's context into kubeconfig."""
        stream(
            [
                "gcloud",
                "container",
                "clusters",
                "get-credentials",
                self.name,
                f"--location={self.location}",
                f"--project={self.outputs['project_id']}",
            ],
            log_line,
            env={"KUBECONFIG": str(self.kubeconfig)},
        )
