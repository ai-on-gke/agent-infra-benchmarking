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

"""A case's applied cluster."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .log import log
from .process import output, stream

# How long a cluster's persistent volumes get to go before it is destroyed.
VOLUMES_TIMEOUT_SECS = 300


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

    @property
    def substrate_docker_repo(self) -> str | None:
        # None for a cluster that runs no substrate, or one applied before the output existed.
        return self.outputs.get("substrate_docker_repo")

    @property
    def runner_ksa(self) -> str:
        # The variable's default for a state applied before the output existed.
        return self.outputs.get("benchmark_runner_ksa_name", "benchmark-runner")

    @property
    def sandbox_class(self) -> str:
        # The variable's default for a state applied before the output existed.
        return self.outputs.get("sandbox_class", "gvisor")

    def kubectl_flags(self, namespace: str | None = None) -> list[str]:
        flags = [f"--kubeconfig={self.kubeconfig}"]
        return flags + [f"--namespace={namespace}"] if namespace else flags

    def kubectl(self, *args: str, namespace: str | None = None) -> list[str]:
        """A kubectl command for the cluster, in namespace if given."""
        return ["kubectl", *args, *self.kubectl_flags(namespace)]

    def get_credentials(self) -> None:
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
            env={"KUBECONFIG": str(self.kubeconfig)},
        )

    def delete_volumes(self) -> None:
        """Deletes the cluster's persistent volume claims, and the workloads
        that mount or would recreate them, and waits for their volumes to go,
        as GKE leaves a claim's disk behind when the cluster is deleted under
        it. Best effort: a failure is logged, and the disks then stay."""
        try:
            self.get_credentials()
            claims = json.loads(output(self.kubectl("get", "pvc", "--all-namespaces", "--output=json")))["items"]
            for namespace in sorted({claim["metadata"]["namespace"] for claim in claims}):
                log(f"cluster: deleting the persistent volumes in {namespace}, so their disks go before the cluster")
                workloads = "statefulsets,deployments,jobs,pods"
                stream(self.kubectl("delete", workloads, "--all", "--wait=false", namespace=namespace))
                stream(self.kubectl("delete", "pvc", "--all", "--wait=false", namespace=namespace))
            if json.loads(output(self.kubectl("get", "pv", "--output=json")))["items"]:
                stream(self.kubectl("wait", "--for=delete", "pv", "--all", f"--timeout={VOLUMES_TIMEOUT_SECS}s"))
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
            log(f"cluster: the disks of {self.name}'s persistent volumes may outlive it: {exc}", "WARNING")
