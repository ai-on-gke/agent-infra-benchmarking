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
SandboxConfig, every image by digest, substrate's control plane included, the
commits, and the tools the run had. The master
writes it into run.json beside the reports, with how the run went."""

from __future__ import annotations

import functools
import json
import platform
import re
import subprocess
from collections import defaultdict
from typing import Callable

from .. import images
from ..control_planes import agent_sandbox, substrate, worker_pool
from ..infra.cluster import Cluster
from ..infra.cluster_configs import ClusterConfig, to_record
from ..log import log
from ..process import output
from ..scenarios.install import AGENT_SANDBOX, SUBSTRATE, control_plane


def _read(what: str, read: Callable[[], object], errors: dict[str, str]) -> object:
    """read(), or None with the reason in errors: a part that cannot be read
    leaves a gap in run.json rather than failing the run."""
    try:
        return read()
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, substrate.SubstrateError, agent_sandbox.AgentSandboxError) as exc:
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


def deployments(cluster: Cluster, namespace: str) -> dict:
    """The WorkerPool's workers and what the workload calls, such as mock-llm,
    which run as Deployments in the pool's namespace."""
    items = _kubectl_json(cluster, "deployments", namespace=namespace)["items"]
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


# Substrate's control plane: ateapi, atelet, atenet, the controller and the
# dataplane, which hack/install-ate.sh deploys into it.
CONTROL_PLANE_NAMESPACE = "ate-system"


def control_plane_namespace(case: dict) -> str:
    """Where the case's control plane runs."""
    return agent_sandbox.NAMESPACE if control_plane(case) == AGENT_SANDBOX else CONTROL_PLANE_NAMESPACE


def workload_of(pod: dict) -> str:
    """The Deployment, DaemonSet, StatefulSet or Job a pod belongs to, or the
    pod itself."""
    owners = pod["metadata"].get("ownerReferences") or []
    if not owners:
        return f"Pod/{pod['metadata']['name']}"
    owner = owners[0]
    if owner["kind"] == "ReplicaSet":
        suffix = (pod["metadata"].get("labels") or {}).get("pod-template-hash")
        name = owner["name"].removesuffix(f"-{suffix}") if suffix else owner["name"]
        return f"Deployment/{name}"
    return f"{owner['kind']}/{owner['name']}"


def pod_images(cluster: Cluster, namespace: str) -> dict:
    """Every container image the namespace's pods run, by their workload and
    container, as the manifest named it, with the digests the nodes resolved
    it to: what ran, whatever tag the manifest named."""
    images_by_workload: dict[str, dict[str, dict]] = defaultdict(dict)
    for pod in _kubectl_json(cluster, "pods", namespace=namespace)["items"]:
        spec = pod.get("spec") or {}
        # The status's image is a bare image ID for an image named by digest.
        named = {c["name"]: c.get("image") for c in spec.get("initContainers", []) + spec.get("containers", [])}
        statuses = (pod.get("status") or {}).get("initContainerStatuses", []) + (pod.get("status") or {}).get(
            "containerStatuses", []
        )
        for status in statuses:
            image = named.get(status["name"]) or status["image"]
            entry = images_by_workload[workload_of(pod)].setdefault(status["name"], {"image": image, "digests": []})
            digest = (status.get("imageID") or "").rpartition("@")[2]
            if digest and digest not in entry["digests"]:
                entry["digests"].append(digest)
    return dict(sorted(images_by_workload.items()))


def _version(cmd: list[str], pattern: str = r"\S+") -> str | None:
    """The tool's version, or None for a tool this machine does not have."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return None
    match = re.search(pattern, proc.stdout)
    return match[0] if match else None


@functools.cache
def tools() -> dict:
    """The versions of the tools on this machine that built and deployed the
    run: Terraform applied its infra, ko and Docker built its images, kubectl
    and gcloud deployed it."""
    ko = re.search(r"github\.com/google/ko (v\S+)", (substrate.SUBSTRATE_DIR / "hack/tools/ko/go.mod").read_text())
    return {
        "terraform": _version(["terraform", "version", "-json"], r'(?<="terraform_version": ")[^"]+'),
        "kubectl": _version(["kubectl", "version", "--client", "-o", "json"], r'(?<="gitVersion": ")[^"]+'),
        "gcloud": _version(["gcloud", "version", "--format=value(\"Google Cloud SDK\")"]),
        "go": _version(["go", "env", "GOVERSION"]),
        "docker": _version(["docker", "version", "--format", "{{.Client.Version}}"]),
        "ko": ko[1] if ko else None,
        "python": platform.python_version(),
    }


def collect(
    cluster: Cluster,
    case: dict,
    infra: ClusterConfig,
    template_name: str | None,
    template: dict | None,
    image_refs: dict,
    bucket: str,
    pool: tuple[str, str] = (worker_pool.NAMESPACE, worker_pool.NAME),
) -> dict:
    """infra is the cluster the case's type asked for, so a published run
    says what hardware it asked for; pool is the namespace and name of the
    WorkerPool the run's actors run on."""
    pool_namespace, pool_name = pool
    errors: dict[str, str] = {}
    sandbox_configs = {}
    config_name = ((template or {}).get("sandboxConfig") or {}).get("configName")
    if config_name:
        config = _read("sandbox config", lambda: _kubectl_json(cluster, "sandboxconfig", config_name), errors)
        sandbox_configs = {config_name: (config or {}).get("spec")}
    if control_plane(case) == AGENT_SANDBOX:
        pool = _read(
            "warm pool",
            lambda: _kubectl_json(cluster, "sandboxwarmpool", agent_sandbox.warm_pool_name(template_name), namespace=pool_namespace),
            errors,
        )
    else:
        pool = _read(
            "worker pool",
            lambda: _kubectl_json(cluster, "workerpool", pool_name, namespace=pool_namespace),
            errors,
        )
    dirty = _read("repo state", lambda: bool(output(["git", "status", "--porcelain"], cwd=images.ROOT).strip()), errors)
    info = {
        "case": case,
        "infra": to_record(infra),
        "cluster": _read("cluster config", lambda: cluster_config(cluster), errors),
        "nodes": _read("nodes", lambda: nodes(cluster), errors),
        "worker_pool": {"spec": pool.get("spec"), "status": pool.get("status")} if pool else None,
        "actor_templates": {template_name: template} if template_name and template else {},
        "sandbox_configs": sandbox_configs,
        "deployments": _read("deployments", lambda: deployments(cluster, pool_namespace), errors),
        "control_plane_images": _read(
            "control plane images", lambda: pod_images(cluster, control_plane_namespace(case)), errors
        ),
        "worker_pool_images": _read("worker pool images", lambda: pod_images(cluster, pool_namespace), errors),
        "images": image_refs,
        "locust_image": image_refs.get("locust-master"),
        "commit": images.commit(),
        "image_tag": images.tag(),
        "commit_dirty": dirty,
        "tools": _read("tool versions", tools, errors),
        "control_plane": control_plane(case),
        "substrate_commit": _read("substrate commit", lambda: output(["git", "rev-parse", "HEAD"], cwd=substrate.SUBSTRATE_DIR).strip(), errors) if control_plane(case) == SUBSTRATE else None,
        "substrate_version": _read("substrate version", substrate.build_version, errors) if control_plane(case) == SUBSTRATE else None,
        "agent_sandbox_commit": _read("agent-sandbox commit", lambda: output(["git", "rev-parse", "HEAD"], cwd=agent_sandbox.AGENT_SANDBOX_DIR).strip(), errors) if control_plane(case) == AGENT_SANDBOX else None,
        "agent_sandbox_version": _read("agent-sandbox version", agent_sandbox.build_version, errors) if control_plane(case) == AGENT_SANDBOX else None,
        "bucket": bucket,
    }
    # A locust run's run.json keeps the key it has always had.
    if "locust-master" in image_refs:
        info["locust_image"] = image_refs["locust-master"]
    if errors:
        info["run_info_errors"] = errors
    return info
