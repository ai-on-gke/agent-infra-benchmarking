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

"""Installs agent-sandbox (controller, CRDs, extensions, and sandbox-router) on
a case's cluster, from the images builder.py built into its repository, and
deploys a workload's SandboxTemplate and SandboxWarmPool, from
agent-sandbox/manifests/."""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import time
from string import Template

from .. import LABEL_RE, RUNNER_DIR, git, images
from ..infra.cluster import Cluster
from ..log import log
from ..process import output, retry, stream
from ..workloads import openclaw

AGENT_SANDBOX_DIR = RUNNER_DIR.parent.parent / "dependency-repos" / "agent-sandbox"
AGENT_SANDBOX_PATH = AGENT_SANDBOX_DIR.relative_to(images.ROOT).as_posix()
MANIFESTS_DIR = RUNNER_DIR.parent / "agent-sandbox" / "manifests"

CONTROLLER_IMAGE = images.Image(
    name="agent-sandbox-controller",
    dockerfile=f"{AGENT_SANDBOX_PATH}/Dockerfile",
    context=AGENT_SANDBOX_PATH,
)

ROUTER_IMAGE = images.Image(
    name="sandbox-router-go",
    dockerfile=f"{AGENT_SANDBOX_PATH}/sandbox-router/Dockerfile",
    context=AGENT_SANDBOX_PATH,
)

IMAGES = [CONTROLLER_IMAGE, ROUTER_IMAGE]
NAMESPACE = "agent-sandbox-system"
# Where the templates and warm pools go: beside mock-llm, which the openclaw
# template's MOCK_LLM_BASE_URL names.
WORKLOADS_NAMESPACE = openclaw.NAMESPACE
# The image of workloads.json each workload's template runs.
WORKLOAD_IMAGES = {
    "glutton": "glutton",
    "openclaw": "openclaw-workload",
    "rl-rollout": "rl-rollout",
}
ROLLOUT_TIMEOUT_SECS = 180
# Each sandbox class's RuntimeClass, GKE Sandbox's, whose nodes carry
# GKE_SANDBOX_LABEL with it (infra.cluster.gke-sandbox).
RUNTIME_CLASSES = {"gvisor": "gvisor"}
GKE_SANDBOX_LABEL = "sandbox.gke.io/runtime"


class AgentSandboxError(RuntimeError):
    pass


@functools.cache
def build_version() -> str:
    missing = missing_submodule()
    if missing:
        raise AgentSandboxError(f"the agent-sandbox build version requires {missing[0]}")
    version = os.environ.get("AGENT_SANDBOX_VERSION") or git.describe(AGENT_SANDBOX_DIR) or "dev"
    if not LABEL_RE.fullmatch(version):
        version = re.sub(r"[^A-Za-z0-9_.-]", "-", version)[:63]
    return version


def missing_submodule() -> list[str]:
    return [] if (AGENT_SANDBOX_DIR / "go.mod").is_file() else [f"the agent-sandbox submodule in {AGENT_SANDBOX_DIR}"]


def image_refs(repo: str) -> list[str]:
    """What install takes from repo, tagged build_version()."""
    return [controller_image_ref(repo), router_image_ref(repo)]


def controller_image_ref(repo: str) -> str:
    return f"{images.group_repo(repo, 'agent-sandbox')}/{CONTROLLER_IMAGE.name}:{build_version()}"


def router_image_ref(repo: str) -> str:
    return f"{images.group_repo(repo, 'agent-sandbox')}/{ROUTER_IMAGE.name}:{build_version()}"


def render(manifest: str, **values: object) -> str:
    return Template((MANIFESTS_DIR / manifest).read_text()).substitute(values)


def apply(cluster: Cluster, manifest: str) -> None:
    stream(cluster.kubectl("apply", "-f", "-"), input=manifest)


def warm_pool_name(workload: str) -> str:
    return f"{workload}-warm-pool"


def install(cluster: Cluster) -> None:
    cluster.get_credentials()
    retry(f"agent-sandbox on cluster {cluster.name}", lambda: install_once(cluster))


def install_once(cluster: Cluster) -> None:
    repo = cluster.image_docker_repo
    if not repo:
        raise AgentSandboxError(f"cluster {cluster.name} names no repository of agent-sandbox images")
    controller_img = images.pin(controller_image_ref(repo))
    router_img = images.pin(router_image_ref(repo))
    log(f"agent-sandbox: installing on cluster {cluster.name} (controller: {controller_img}, router: {router_img})")
    k8s = AGENT_SANDBOX_DIR / "k8s"
    for crd in sorted((k8s / "crds").glob("*.yaml")):
        stream(cluster.kubectl("apply", "-f", str(crd)))
    cluster.apply_namespace(NAMESPACE)
    for rbac in ("rbac.generated.yaml", "extensions-rbac.generated.yaml", "extensions.yaml"):
        stream(cluster.kubectl("apply", "-f", str(k8s / rbac)))
    apply(cluster, render("controller.yaml.tmpl", NAMESPACE=NAMESPACE, IMAGE=controller_img))
    router_deploy = AGENT_SANDBOX_DIR / "sandbox-router" / "deploy"
    for manifest in ("serviceaccount.yaml", "rbac.yaml", "service.yaml"):
        stream(cluster.kubectl("apply", "-f", str(router_deploy / manifest)))
    apply(cluster, render("router.yaml.tmpl", NAMESPACE=NAMESPACE, IMAGE=router_img))
    for deployment in ("agent-sandbox-controller", "sandbox-router"):
        rollout = ["rollout", "status", f"deployment/{deployment}", f"--timeout={ROLLOUT_TIMEOUT_SECS}s"]
        stream(cluster.kubectl(*rollout, namespace=NAMESPACE))
    log(f"agent-sandbox: controller and router are ready on cluster {cluster.name}")


def cleanup(cluster: Cluster, namespace: str, timeout: int = 60) -> dict:
    """Deletes any leftover SandboxClaim and Sandbox resources in namespace."""
    record = {"namespace": namespace, "claims_deleted": 0, "sandboxes_deleted": 0}
    for kind, deleted in (("sandboxclaims", "claims_deleted"), ("sandboxes", "sandboxes_deleted")):
        cmd = cluster.kubectl("get", kind, "-o", "jsonpath={.items[*].metadata.name}", namespace=namespace)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        names = proc.stdout.split() if proc.returncode == 0 else []
        if names:
            log(f"agent-sandbox: cleaning up {len(names)} {kind} in {namespace}")
            delete = ["delete", kind, *names, "--ignore-not-found", f"--timeout={timeout}s"]
            subprocess.run(cluster.kubectl(*delete, namespace=namespace), capture_output=True, text=True)
            record[deleted] = len(names)
    return record


def deploy_workload_and_pool(
    cluster: Cluster,
    workload_name: str,
    worker_count: int,
    image_repo: str,
    sandbox_class: str = "gvisor",
    timeout: int = 300,
) -> None:
    if workload_name not in WORKLOAD_IMAGES:
        raise AgentSandboxError(f"no agent-sandbox template of {workload_name}; there are {', '.join(WORKLOAD_IMAGES)}")
    if sandbox_class not in RUNTIME_CLASSES:
        raise AgentSandboxError(f"agent-sandbox runs only {', '.join(RUNTIME_CLASSES)}, not {sandbox_class}")
    cluster.apply_namespace(WORKLOADS_NAMESPACE)
    if workload_name == "openclaw":
        openclaw.deploy_mock_llm(cluster, image_repo, timeout)
    template = render(
        f"{workload_name}-template.yaml.tmpl",
        NAME=workload_name,
        NAMESPACE=WORKLOADS_NAMESPACE,
        IMAGE=images.pin(images.image_ref(images.WORKLOADS_IMAGES, image_repo, WORKLOAD_IMAGES[workload_name])),
        RUNTIME_CLASS=RUNTIME_CLASSES[sandbox_class],
    )
    log(f"agent-sandbox: deploying SandboxTemplate {workload_name}")
    apply(cluster, template)
    apply_warm_pool(cluster, workload_name, worker_count, timeout)
    check_isolated(cluster, RUNTIME_CLASSES[sandbox_class])


def recreate_warm_pool(
    cluster: Cluster, workload_name: str, worker_count: int, sandbox_class: str = "gvisor", timeout: int = 300
) -> None:
    delete = ["delete", "sandboxwarmpool", warm_pool_name(workload_name), "--ignore-not-found"]
    stream(cluster.kubectl(*delete, namespace=WORKLOADS_NAMESPACE))
    apply_warm_pool(cluster, workload_name, worker_count, timeout)
    check_isolated(cluster, RUNTIME_CLASSES[sandbox_class])


def check_isolated(cluster: Cluster, runtime_class: str) -> None:
    """Raises AgentSandboxError unless every sandbox's pod asks for
    runtime_class and is on a node GKE Sandbox runs it on."""
    pods = json.loads(output(cluster.kubectl("get", "pods", "-o", "json", namespace=WORKLOADS_NAMESPACE)))["items"]
    nodes = json.loads(output(cluster.kubectl("get", "nodes", "-o", "json")))["items"]
    problems = isolation_problems(pods, nodes, runtime_class)
    if problems:
        raise AgentSandboxError("sandboxes not isolated: " + "; ".join(problems))
    count = sum(map(is_sandbox_pod, pods))
    log(f"agent-sandbox: all {count} sandbox pods run with RuntimeClass {runtime_class} on GKE Sandbox nodes")


def is_sandbox_pod(pod: dict) -> bool:
    """A sandbox's pod, not one a pool recreated is still deleting."""
    meta = pod["metadata"]
    owned = any(ref.get("kind") == "Sandbox" for ref in meta.get("ownerReferences", []))
    return owned and "deletionTimestamp" not in meta


def isolation_problems(pods: list[dict], nodes: list[dict], runtime_class: str) -> list[str]:
    """What in pods and nodes, as kubectl gets them, leaves a sandbox's pod
    out of runtime_class, or that there is no sandbox pod to tell."""
    sandboxed = {
        node["metadata"]["name"]
        for node in nodes
        if node["metadata"].get("labels", {}).get(GKE_SANDBOX_LABEL) == runtime_class
    }
    problems = []
    for pod in filter(is_sandbox_pod, pods):
        name = pod["metadata"]["name"]
        have = pod["spec"].get("runtimeClassName")
        if have != runtime_class:
            problems.append(f"pod {name} has runtimeClassName {have!r}, not {runtime_class!r}")
        elif pod["spec"].get("nodeName") not in sandboxed:
            node = pod["spec"].get("nodeName")
            problems.append(f"pod {name} is on node {node}, which is not {GKE_SANDBOX_LABEL}={runtime_class}")
    if not any(map(is_sandbox_pod, pods)):
        problems.append("no sandbox pods to check")
    return problems


def apply_warm_pool(cluster: Cluster, workload_name: str, worker_count: int, timeout: int) -> None:
    name = warm_pool_name(workload_name)
    log(f"agent-sandbox: deploying SandboxWarmPool {name} with {worker_count} replicas")
    values = {"NAME": name, "NAMESPACE": WORKLOADS_NAMESPACE, "REPLICAS": worker_count, "TEMPLATE": workload_name}
    apply(cluster, render("warm-pool.yaml.tmpl", **values))
    wait_warm_pool_ready(cluster, name, worker_count, timeout)


def wait_warm_pool_ready(cluster: Cluster, name: str, worker_count: int, timeout: int = 300) -> None:
    log(f"agent-sandbox: waiting for {name} to pre-warm {worker_count} sandboxes (timeout: {timeout}s)")
    deadline = time.monotonic() + timeout
    get = cluster.kubectl(
        "get", "sandboxwarmpool", name, "-o", "jsonpath={.status.readyReplicas}", namespace=WORKLOADS_NAMESPACE
    )
    while time.monotonic() < deadline:
        proc = subprocess.run(get, capture_output=True, text=True)
        ready = proc.stdout.strip()
        if proc.returncode == 0 and ready.isdigit():
            if int(ready) >= worker_count:
                log(f"agent-sandbox: {name} ready with {ready}/{worker_count} sandboxes")
                return
            log(f"agent-sandbox: {name} has {ready}/{worker_count} ready sandboxes...")
        time.sleep(5)
    raise AgentSandboxError(f"timed out waiting for {name} to reach {worker_count} ready sandboxes")
