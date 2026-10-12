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

"""What a run was made of that its reports cannot say themselves: the case,
the cluster and its nodes, the WorkerPool, the ActorTemplate and its
SandboxConfig, the images by digest and the commits. The master writes it into
run.json beside the reports, with how the run went."""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from typing import Callable

from . import SCENARIOS_DIR, images, substrate, worker_pool
from .cluster import Cluster
from .log import log
from .process import output


def _read(what: str, read: Callable[[], object], errors: dict[str, str]) -> object:
    """read(), or None with the reason in errors: a part that cannot be read
    leaves a gap in run.json rather than failing the run."""
    try:
        return read()
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, substrate.SubstrateError) as exc:
        errors[what] = str(exc)
        log(f"run info: no {what}: {exc}", "WARNING")
        return None


def _kubectl_json(cluster: Cluster, *args: str, namespace: str | None = None) -> dict:
    return json.loads(output(cluster.kubectl("get", *args, "-o=json", namespace=namespace)))


def cluster_config(cluster: Cluster) -> dict:
    """The cluster's versions and node pools as GKE describes them."""
    describe = [
        "gcloud", "container", "clusters", "describe", cluster.name,
        f"--location={cluster.location}", f"--project={cluster.project_id}", "--format=json",
    ]  # fmt: skip
    desc = json.loads(output(describe))
    pools = {}
    for pool in desc.get("nodePools") or []:
        config, management = pool.get("config") or {}, pool.get("management") or {}
        autoscaling = pool.get("autoscaling") or {}
        pools[pool["name"]] = {
            "version": pool.get("version"),
            "machine_type": config.get("machineType"),
            "disk_type": config.get("diskType"),
            "disk_size_gb": config.get("diskSizeGb"),
            "local_ssd_count": config.get("localSsdCount", 0),
            "image_type": config.get("imageType"),
            "spot": config.get("spot", False),
            "locations": pool.get("locations"),
            "autoscaling": (
                {"min": autoscaling.get("minNodeCount"), "max": autoscaling.get("maxNodeCount")}
                if autoscaling.get("enabled")
                else None
            ),
            "auto_upgrade": management.get("autoUpgrade", False),
            "auto_repair": management.get("autoRepair", False),
        }
    return {
        "name": desc.get("name"),
        "location": desc.get("location"),
        "control_plane_version": desc.get("currentMasterVersion"),
        "release_channel": (desc.get("releaseChannel") or {}).get("channel", "UNSPECIFIED"),
        "node_pools": pools,
    }


def nodes(cluster: Cluster) -> dict:
    """The nodes as they came up, per pool: what a run actually ran on."""
    by_pool: dict[str, list[dict]] = defaultdict(list)
    for node in _kubectl_json(cluster, "nodes")["items"]:
        labels, status = node["metadata"].get("labels", {}), node.get("status", {})
        info, capacity = status.get("nodeInfo", {}), status.get("capacity", {})
        memory = capacity.get("memory", "")
        by_pool[labels.get("cloud.google.com/gke-nodepool", "unknown")].append(
            {
                "ready": any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])),
                "machine_type": labels.get("node.kubernetes.io/instance-type"),
                "boot_disk": labels.get("cloud.google.com/gke-boot-disk"),
                "zone": labels.get("topology.kubernetes.io/zone"),
                "cpu": capacity.get("cpu"),
                # In GiB: the exact capacity differs by a few KiB from node to node.
                "memory_gib": round(int(memory[:-2]) / 2**20, 1) if memory.endswith("Ki") else memory,
                "kubelet_version": info.get("kubeletVersion"),
                "os_image": info.get("osImage"),
                "kernel_version": info.get("kernelVersion"),
                "container_runtime": info.get("containerRuntimeVersion"),
            }
        )

    def distinct(items: list[dict], key: str) -> list:
        return sorted({item[key] for item in items if item[key] is not None}, key=str)

    # Field per node -> the list of its distinct values per pool.
    fields = {
        "machine_type": "machine_types",
        "boot_disk": "boot_disks",
        "zone": "zones",
        "cpu": "cpu",
        "memory_gib": "memory_gib",
        "kubelet_version": "kubelet_versions",
        "os_image": "os_images",
        "kernel_version": "kernel_versions",
        "container_runtime": "container_runtimes",
    }
    return {
        pool: {
            "count": len(items),
            "ready": sum(item["ready"] for item in items),
            **{name: distinct(items, field) for field, name in fields.items()},
        }
        for pool, items in sorted(by_pool.items())
    }


def deployments(cluster: Cluster) -> dict:
    """The WorkerPool's workers and what the workload calls, such as mock-llm,
    which run as Deployments in the pool's namespace."""
    items = _kubectl_json(cluster, "deployments", namespace=worker_pool.NAMESPACE)["items"]
    return {
        d["metadata"]["name"]: {
            "replicas": d["spec"].get("replicas"),
            "containers": [
                {"name": c["name"], "image": c["image"], "resources": c.get("resources")}
                for c in d["spec"]["template"]["spec"]["containers"]
            ],
        }
        for d in items
    }


def infra(case: dict) -> dict:
    """The case's cluster spec, with the text of the tfvars file it names, so
    a published run says what hardware it asked for."""
    spec = dict(case["infra"]["cluster"])
    tfvars = spec.get("tfvars")
    if tfvars:
        # As cases.CLUSTERS_DIR, which importing would be circular.
        path = SCENARIOS_DIR / "infra" / "clusters" / spec["name"] / tfvars
        spec["tfvars_content"] = path.read_text() if path.is_file() else None
    return spec


def collect(
    cluster: Cluster, case: dict, template_name: str | None, template: dict | None, image_refs: dict, bucket: str
) -> dict:
    errors: dict[str, str] = {}
    sandbox_configs = {}
    config_name = ((template or {}).get("sandboxConfig") or {}).get("configName")
    if config_name:
        config = _read("sandbox config", lambda: _kubectl_json(cluster, "sandboxconfig", config_name), errors)
        sandbox_configs = {config_name: (config or {}).get("spec")}
    pool = _read(
        "worker pool",
        lambda: _kubectl_json(cluster, "workerpool", worker_pool.NAME, namespace=worker_pool.NAMESPACE),
        errors,
    )
    dirty = _read("repo state", lambda: bool(output(["git", "status", "--porcelain"], cwd=images.ROOT).strip()), errors)
    info = {
        "case": case,
        "infra": _read("infra", lambda: infra(case), errors),
        "cluster": _read("cluster config", lambda: cluster_config(cluster), errors),
        "nodes": _read("nodes", lambda: nodes(cluster), errors),
        "worker_pool": {"spec": pool.get("spec"), "status": pool.get("status")} if pool else None,
        "actor_templates": {template_name: template} if template_name and template else {},
        "sandbox_configs": sandbox_configs,
        "deployments": _read("deployments", lambda: deployments(cluster), errors),
        "images": image_refs,
        "locust_image": image_refs.get("locust-master"),
        "commit": images.tag(),
        "commit_dirty": dirty,
        "substrate_commit": _read("substrate commit", lambda: output(["git", "rev-parse", "HEAD"], cwd=substrate.SUBSTRATE_DIR).strip(), errors),
        "substrate_version": _read("substrate version", substrate.build_version, errors),
        "bucket": bucket,
    }
    if errors:
        info["run_info_errors"] = errors
    return info
