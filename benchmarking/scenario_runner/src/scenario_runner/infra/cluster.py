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
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from ..log import log
from ..process import output, stream
from .terraform import STOCKOUT_MARKERS

# How long a cluster's persistent volumes get to go before it is destroyed.
VOLUMES_TIMEOUT_SECS = 300
CREATE_OPERATIONS = {"CREATE_CLUSTER", "CREATE_NODE_POOL"}
# The common config's pool a bare-metal placement moves.
SANDBOXED_POOL = "sandboxed"
STOCKOUT_GRACE_SECS = 300
STOCKOUT_POLL_SECS = 30


def utc_now() -> str:
    """Now, as RFC 3339, which GKE's operation times compare with."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_zone(project_id: str, region: str) -> str:
    """The first zone of the region that is up, which a case's cluster goes in
    unless it or --zone says another. Raises ValueError if there is none, or
    the zones cannot be read."""
    cmd = ["gcloud", "compute", "zones", "list", f"--project={project_id}", "--format=json(name,region,status)"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        raise ValueError(f"cannot list the zones of {region} to pick one; pass --zone: {proc.stderr.strip()}")
    zones = sorted(
        zone["name"]
        for zone in json.loads(proc.stdout or "[]")
        if zone.get("region", "").rsplit("/", 1)[-1] == region and zone.get("status") == "UP"
    )
    if not zones:
        raise ValueError(f"project {project_id} has no zone up in region {region}")
    return zones[0]


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


def group_where(url: str, project_id: str) -> list[str]:
    """The arguments that name a zonal instance group, by its URL, to gcloud."""
    zone = url.split("/zones/", 1)[1].split("/", 1)[0]
    return [url.rsplit("/", 1)[-1], f"--zone={zone}", f"--project={project_id}"]


def gcloud_json(cmd: list[str]) -> object | None:
    """cmd's --format=json output, or None if it failed, with a warning unless
    what it asked for is not there (yet)."""
    proc = subprocess.run([*cmd, "--format=json"], capture_output=True, text=True)
    if proc.returncode:
        if "code=404" not in proc.stderr:
            log(f"{' '.join(cmd[:5])} failed: {proc.stderr.strip()}", "WARNING")
        return None
    return json.loads(proc.stdout or "null")


class PoolStockout:
    """Tells whether a failed apply of a cluster with a bare-metal pool was a
    stockout, as gke_stockout does. With give_up, while watching it also
    gives up on the pool once its instance group has failed to create
    instances on stockouts for STOCKOUT_GRACE_SECS, by resizing the group to
    0, which ends GKE's create of the pool at once rather than after about 35
    minutes. GKE then takes the pool as created, so the apply succeeds and the
    caller asks gave_up."""

    def __init__(self, project_id: str, zone: str, cluster: str, pool: str = SANDBOXED_POOL, give_up: bool = True):
        self.project_id, self.zone, self.cluster, self.pool = project_id, zone, cluster, pool
        self.give_up = give_up
        self.since = utc_now()
        self.gave_up = False

    def __call__(self) -> bool:
        return self.gave_up or gke_stockout(self.project_id, self.zone, self.cluster, self.since)

    @contextmanager
    def watching(self) -> Iterator[None]:
        if not self.give_up:
            yield
            return
        done = threading.Event()
        thread = threading.Thread(target=self.watch, args=(done,), daemon=True)
        thread.start()
        try:
            yield
        finally:
            done.set()
            thread.join()

    def watch(self, done: threading.Event) -> None:
        while not done.wait(STOCKOUT_POLL_SECS):
            try:
                groups = self.stocked_out_groups()
            except Exception as exc:
                log(f"cannot watch pool {self.pool} for stockouts: {exc}", "WARNING")
                continue
            if groups and self.resize_to_zero(groups):
                return

    def stocked_out_groups(self) -> list[str]:
        """The pool's instance groups, by URL, if every one still creating has
        failed on stockouts since at least STOCKOUT_GRACE_SECS ago."""
        pool = gcloud_json(["gcloud", "container", "node-pools", "describe", self.pool, f"--cluster={self.cluster}",
                            f"--location={self.zone}", f"--project={self.project_id}"])  # fmt: skip
        urls = (pool or {}).get("instanceGroupUrls", [])
        since = datetime.fromisoformat(self.since)
        grace_from = datetime.now(timezone.utc) - timedelta(seconds=STOCKOUT_GRACE_SECS)
        creating = []
        group = ["gcloud", "compute", "instance-groups", "managed"]
        for url in urls:
            where = group_where(url, self.project_id)
            described = gcloud_json([*group, "describe", *where])
            if not described or not described.get("currentActions", {}).get("creating"):
                continue
            errors = gcloud_json([*group, "list-errors", *where]) or []
            first = min(
                (
                    datetime.fromisoformat(err["timestamp"])
                    for err in errors
                    if any(marker in json.dumps(err.get("error")) for marker in STOCKOUT_MARKERS)
                    and datetime.fromisoformat(err["timestamp"]) >= since
                ),
                default=None,
            )
            if first is None or first > grace_from:
                return []
            creating.append(url)
        return creating

    def resize_to_zero(self, urls: list[str]) -> bool:
        log(f"pool {self.pool} of {self.cluster} has failed on stockouts in {self.zone} for"
            f" {STOCKOUT_GRACE_SECS}s; resizing its instance groups to 0 to give up on it", "WARNING")  # fmt: skip
        for url in urls:
            resize = ["gcloud", "compute", "instance-groups", "managed", "resize", *group_where(url, self.project_id)]
            if gcloud_json([*resize, "--size=0"]) is None:
                return False
        self.gave_up = True
        return True


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
    def snapshot_transfer_gsa(self) -> str | None:
        """The account substrate moves snapshots as, without its domain; None
        for a state applied before the output existed."""
        return self.outputs.get("snapshot_transfer_gsa_name")

    @property
    def sandboxed_kvm(self) -> bool:
        """Whether the sandboxed pool has /dev/kvm, which microvm needs. A
        state applied before the output existed had it for microvm only."""
        return self.outputs.get("sandboxed_kvm", self.outputs.get("sandbox_class") == "microvm")

    @property
    def sandboxed_gke_sandbox(self) -> bool:
        """Whether the sandboxed pool runs GKE Sandbox, which agent-sandbox's
        gvisor pods need; none before the output existed."""
        return self.outputs.get("sandboxed_gke_sandbox", False)

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
