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
from datetime import datetime, timezone
from pathlib import Path

from ..log import log
from ..process import output, stream
from .terraform import STOCKOUT_MARKERS

# How long a cluster's persistent volumes get to go before it is destroyed.
VOLUMES_TIMEOUT_SECS = 300
CREATE_OPERATIONS = {"CREATE_CLUSTER", "CREATE_NODE_POOL"}


def utc_now() -> str:
    """Now, as RFC 3339, which GKE's operation times compare with."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gke_stockout(project_id: str, location: str, cluster: str, since: str) -> bool:
    """Whether a create of the cluster or of one of its node pools that GKE
    started at since (RFC 3339) or later failed on a stockout. GKE says so only
    on the operation, after waiting about 35 minutes for the nodes; the google
    provider passes on just that the pool is in the error state."""
    cmd = ["gcloud", "container", "operations", "list", f"--location={location}", f"--project={project_id}",
           f'--filter=targetLink~"/clusters/{cluster}(/|$)"', "--format=json"]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        log(f"cannot read cluster {cluster}'s GKE operations to tell a stockout: {proc.stderr.strip()}", "WARNING")
        return False
    return any(
        op.get("operationType") in CREATE_OPERATIONS
        and op.get("startTime", "") >= since
        and any(marker in json.dumps([op.get("error"), op.get("statusMessage")]) for marker in STOCKOUT_MARKERS)
        for op in json.loads(proc.stdout or "[]")
    )


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
        # Its zone, or for a regional one or a state applied before the output
        # existed, the region.
        return self.outputs.get("location") or self.outputs["region"]

    @property
    def project_id(self) -> str:
        return self.outputs["project_id"]

    @property
    def bucket(self) -> str:
        return self.outputs["bucket_name"]

    @property
    def image_docker_repo(self) -> str | None:
        """The runner infra's repository, which substrate's images and the
        benchmark's are pulled from; None for a cluster granted none. The old
        output's name for a state applied before it was renamed."""
        return self.outputs.get("image_docker_repo") or self.outputs.get("substrate_docker_repo")

    @property
    def runner_ksa(self) -> str:
        # The variable's default for a state applied before the output existed.
        return self.outputs.get("benchmark_runner_ksa_name", "benchmark-runner")

    @property
    def sandboxed_kvm(self) -> bool:
        """Whether the sandboxed pool has /dev/kvm, which microvm needs. A
        state applied before the output existed had it for microvm only."""
        return self.outputs.get("sandboxed_kvm", self.outputs.get("sandbox_class") == "microvm")

    def kubectl_flags(self, namespace: str | None = None) -> list[str]:
        flags = [f"--kubeconfig={self.kubeconfig}"]
        return flags + [f"--namespace={namespace}"] if namespace else flags

    def kubectl(self, *args: str, namespace: str | None = None) -> list[str]:
        """A kubectl command for the cluster, in namespace if given."""
        return ["kubectl", *args, *self.kubectl_flags(namespace)]

    def apply_namespace(self, namespace: str) -> None:
        manifest = f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {namespace}\n"
        stream(self.kubectl("apply", "-f", "-"), input=manifest)

    @property
    def context(self) -> str:
        """The name get_credentials gives the cluster's context."""
        return f"gke_{self.project_id}_{self.location}_{self.name}"

    def get_credentials(self) -> None:
        """Writes the cluster's context into kubeconfig, as its current one."""
        self.kubeconfig.parent.mkdir(parents=True, exist_ok=True)
        stream(
            [
                "gcloud",
                "container",
                "clusters",
                "get-credentials",
                self.name,
                f"--location={self.location}",
                f"--project={self.outputs['project_id']}",
                *(["--internal-ip"] if self.outputs.get("private_endpoint") else []),
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
