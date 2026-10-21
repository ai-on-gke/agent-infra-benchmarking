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

"""Builds agent-sandbox's images and installs agent-sandbox (controller, CRDs,
extensions, and sandbox-router) on a case's cluster."""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import time

from . import RUNNER_DIR, builder, images
from .cluster import Cluster
from .log import log
from .process import retry, stream

AGENT_SANDBOX_DIR = RUNNER_DIR.parent.parent / "dependency-repos" / "agent-sandbox"
AGENT_SANDBOX_PATH = AGENT_SANDBOX_DIR.relative_to(images.ROOT).as_posix()
LABEL_VALUE = re.compile(r"[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?")

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


class AgentSandboxError(RuntimeError):
    pass


def revision() -> str:
    """The submodule's short commit."""
    proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=AGENT_SANDBOX_DIR, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else "unknown revision"


@functools.cache
def build_version() -> str:
    missing = missing_submodule()
    if missing:
        raise AgentSandboxError(f"the agent-sandbox build version requires {missing[0]}")
    version = os.environ.get("AGENT_SANDBOX_VERSION", "")
    if not version:
        cmd = ["git", "describe", "--tags", "--always", "--dirty"]
        proc = subprocess.run(cmd, cwd=AGENT_SANDBOX_DIR, capture_output=True, text=True)
        version = proc.stdout.strip() if proc.returncode == 0 else ""
    version = version or "dev"
    if not LABEL_VALUE.fullmatch(version):
        version = re.sub(r"[^A-Za-z0-9_.-]", "-", version)[:63]
    return version


def missing_submodule() -> list[str]:
    return [] if (AGENT_SANDBOX_DIR / "go.mod").is_file() else [f"the agent-sandbox submodule in {AGENT_SANDBOX_DIR}"]


def missing_requirements(cloud_build: bool = False) -> list[str]:
    tools = ["git", "gcloud"] + ([] if cloud_build else ["docker"])
    missing = [tool for tool in tools if not shutil.which(tool)]
    return list(dict.fromkeys(missing + missing_submodule()))


def build_images(repo: str, project_id: str, cloud_build: bool = False, login: bool = True) -> None:
    repo = images.group_repo(repo, "agent-sandbox")
    version = build_version()
    log(f"agent-sandbox: building the {version} images into {repo}")
    if cloud_build:
        builder.cloud_build_and_push(IMAGES, repo, version, images.DEFAULT_PLATFORM, project_id)
    else:
        builder.docker_build_and_push(IMAGES, repo, version, images.DEFAULT_PLATFORM, login)


def controller_image_ref(repo: str) -> str:
    return f"{images.group_repo(repo, 'agent-sandbox')}/{CONTROLLER_IMAGE.name}:{build_version()}"


def router_image_ref(repo: str) -> str:
    return f"{images.group_repo(repo, 'agent-sandbox')}/{ROUTER_IMAGE.name}:{build_version()}"


def install(cluster: Cluster) -> None:
    cluster.get_credentials()
    retry(f"agent-sandbox on cluster {cluster.name}", lambda: install_once(cluster))


def install_once(cluster: Cluster) -> None:
    repo = cluster.image_docker_repo
    if not repo:
        raise AgentSandboxError(f"cluster {cluster.name} names no repository of agent-sandbox images")
    controller_ref, router_ref = controller_image_ref(repo), router_image_ref(repo)
    try:
        controller_img = images.pin(controller_ref)
        router_img = images.pin(router_ref)
    except images.ImageError:
        log(f"agent-sandbox: images not found in {repo}; building and pushing...")
        build_images(repo, cluster.project_id)
        controller_img = images.pin(controller_ref)
        router_img = images.pin(router_ref)
    log(f"agent-sandbox: installing on cluster {cluster.name} (controller: {controller_img}, router: {router_img})")

    # 1. Apply CRDs
    crds_dir = AGENT_SANDBOX_DIR / "k8s" / "crds"
    for crd_file in sorted(crds_dir.glob("*.yaml")):
        stream(cluster.kubectl("apply", "-f", str(crd_file)))

    # 2. Namespace
    ns_manifest = f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {NAMESPACE}\n"
    stream(cluster.kubectl("apply", "-f", "-"), input=ns_manifest)

    # 3. RBAC
    rbac_file = AGENT_SANDBOX_DIR / "k8s" / "rbac.generated.yaml"
    stream(cluster.kubectl("apply", "-f", str(rbac_file)))
    ext_rbac_file = AGENT_SANDBOX_DIR / "k8s" / "extensions-rbac.generated.yaml"
    stream(cluster.kubectl("apply", "-f", str(ext_rbac_file)))
    ext_binding_file = AGENT_SANDBOX_DIR / "k8s" / "extensions.yaml"
    stream(cluster.kubectl("apply", "-f", str(ext_binding_file)))

    # 4. Controller Deployment
    controller_manifest = f"""apiVersion: v1
kind: ServiceAccount
metadata:
  name: agent-sandbox-controller
  namespace: {NAMESPACE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: agent-sandbox-controller
subjects:
- kind: ServiceAccount
  name: agent-sandbox-controller
  namespace: {NAMESPACE}
roleRef:
  kind: ClusterRole
  name: agent-sandbox-controller
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-sandbox-controller
  namespace: {NAMESPACE}
  labels:
    app: agent-sandbox-controller
spec:
  replicas: 1
  selector:
    matchLabels:
      app: agent-sandbox-controller
  template:
    metadata:
      labels:
        app: agent-sandbox-controller
    spec:
      serviceAccountName: agent-sandbox-controller
      containers:
      - name: agent-sandbox-controller
        image: {controller_img}
        args:
        - --leader-elect=true
        - --extensions
        - --api-connections=4
        - --separate-watch-connection=true
        - --sandbox-concurrent-workers=200
        - --sandbox-claim-concurrent-workers=150
        - --sandbox-warm-pool-concurrent-workers=2
        - --sandbox-warm-pool-replenish-delay=0
        - --sandbox-warm-pool-max-refill-rate=100
        - --cache-label-selectors=true
        - --disable-sandbox-events=true
        - --disable-claim-events=true
        - --disable-claim-observability-annotations=true
        ports:
        - name: metrics
          containerPort: 8080
          protocol: TCP
        - name: healthz
          containerPort: 8081
          protocol: TCP
"""
    stream(cluster.kubectl("apply", "-f", "-"), input=controller_manifest)

    # 5. Sandbox Router
    router_deploy_dir = AGENT_SANDBOX_DIR / "sandbox-router" / "deploy"
    stream(cluster.kubectl("apply", "-f", str(router_deploy_dir / "serviceaccount.yaml")))
    stream(cluster.kubectl("apply", "-f", str(router_deploy_dir / "rbac.yaml")))
    stream(cluster.kubectl("apply", "-f", str(router_deploy_dir / "service.yaml")))

    router_deployment_manifest = f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: sandbox-router
  namespace: {NAMESPACE}
  labels:
    app: sandbox-router
    app.kubernetes.io/name: sandbox-router
    app.kubernetes.io/component: sandbox-router
spec:
  replicas: 2
  selector:
    matchLabels:
      app.kubernetes.io/name: sandbox-router
      app.kubernetes.io/component: sandbox-router
  template:
    metadata:
      labels:
        app: sandbox-router
        app.kubernetes.io/name: sandbox-router
        app.kubernetes.io/component: sandbox-router
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "9090"
        prometheus.io/path: "/metrics"
    spec:
      serviceAccountName: sandbox-router
      containers:
      - name: sandbox-router
        image: {router_img}
        imagePullPolicy: IfNotPresent
        args:
        - --http-bind-address=:8080
        - --metrics-bind-address=:9090
        - --health-probe-bind-address=:8081
        - --cluster-domain=cluster.local
        - --proxy-timeout=180s
        - --response-header-timeout=180s
        - --upstream-max-retries=3
        - --cache-enabled=true
        ports:
        - name: proxy
          containerPort: 8080
          protocol: TCP
        - name: metrics
          containerPort: 9090
          protocol: TCP
        - name: healthz
          containerPort: 8081
          protocol: TCP
        livenessProbe:
          httpGet:
            path: /healthz
            port: healthz
          initialDelaySeconds: 5
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /healthz
            port: healthz
          initialDelaySeconds: 2
          periodSeconds: 5
"""
    stream(cluster.kubectl("apply", "-f", "-"), input=router_deployment_manifest)

    # 6. Wait for deployments
    timeout = 180
    stream(cluster.kubectl("rollout", "status", "deployment/agent-sandbox-controller", f"--timeout={timeout}s", namespace=NAMESPACE))
    stream(cluster.kubectl("rollout", "status", "deployment/sandbox-router", f"--timeout={timeout}s", namespace=NAMESPACE))
    log(f"agent-sandbox: controller and router are ready on cluster {cluster.name}")


def cleanup(cluster: Cluster, namespace: str, timeout: int = 60) -> dict:
    """Deletes any leftover SandboxClaim and Sandbox resources in namespace."""
    record = {"namespace": namespace, "claims_deleted": 0, "sandboxes_deleted": 0, "left": []}
    for kind in ("sandboxclaims", "sandboxes"):
        cmd = cluster.kubectl("get", kind, "-n", namespace, "-o", "jsonpath={.items[*].metadata.name}")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            names = proc.stdout.strip().split()
            log(f"agent-sandbox: cleaning up {len(names)} {kind} in {namespace}")
            del_cmd = cluster.kubectl("delete", kind, *names, "-n", namespace, "--ignore-not-found", f"--timeout={timeout}s")
            subprocess.run(del_cmd, capture_output=True, text=True)
            if kind == "sandboxclaims":
                record["claims_deleted"] = len(names)
            else:
                record["sandboxes_deleted"] = len(names)
    return record


def deploy_workload_and_pool(
    cluster: Cluster,
    workload_name: str,
    worker_count: int,
    image_repo: str,
    sandbox_class: str = "gvisor",
    timeout: int = 300,
) -> None:
    ns = "benchmark-workloads"
    stream(cluster.kubectl("apply", "-f", "-"), input=f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {ns}\n")

    if workload_name == "openclaw":
        from .workloads import openclaw
        openclaw.deploy_mock_llm(cluster, image_repo, timeout)

    if workload_name == "openclaw":
        image = images.pin(images.image_ref(images.WORKLOADS_IMAGES, image_repo, "openclaw-workload"))
        containers_yaml = f"""      containers:
      - name: openclaw
        image: {image}
        command: ["python3", "-u", "/app/openclaw_actor.py"]
        env:
        - name: OPENCLAW_USE_MOCK_LLM
          value: "true"
        - name: MOCK_LLM_BASE_URL
          value: "http://mock-llm.benchmark-workloads.svc.cluster.local/v1"
        - name: OPENCLAW_TIMEOUT_SECS
          value: "100"
        ports:
        - containerPort: 80
          name: http
        readinessProbe:
          httpGet:
            path: /readyz
            port: 80
          initialDelaySeconds: 2
          periodSeconds: 5
        resources:
          limits:
            memory: 2Gi"""
    else:
        image = images.pin(images.image_ref(images.WORKLOADS_IMAGES, image_repo, "glutton"))
        containers_yaml = f"""      containers:
      - name: glutton
        image: {image}
        command:
        - "/ko-app/glutton"
        - "--grpc-listen-addr=:80"
        - "--metrics-listen-addr=:9090"
        - "--data-dir=/tmp/glutton"
        - "--mode=http"
        ports:
        - containerPort: 80
          name: http
        readinessProbe:
          httpGet:
            path: /readyz
            port: 80
          initialDelaySeconds: 1
          periodSeconds: 2
        resources:
          limits:
            memory: 256Mi"""

    runtime_class_field = ""
    if sandbox_class == "microvm":
        runtime_class_field = "      runtimeClassName: kata\n"
    template_yaml = f"""apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxTemplate
metadata:
  name: {workload_name}
  namespace: {ns}
spec:
  networkPolicyManagement: Unmanaged
  podTemplate:
    spec:
      dnsPolicy: ClusterFirst
{runtime_class_field}      nodeSelector:
        benchmarking-worker: "true"
      tolerations:
      - key: ate.dev/sandboxClass
        operator: Exists
        effect: NoSchedule
{containers_yaml}
"""
    log(f"agent-sandbox: deploying SandboxTemplate {workload_name}")
    stream(cluster.kubectl("apply", "-f", "-"), input=template_yaml)

    warm_pool_name = f"{workload_name}-warm-pool"
    warm_pool_yaml = f"""apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxWarmPool
metadata:
  name: {warm_pool_name}
  namespace: {ns}
spec:
  replicas: {worker_count}
  sandboxTemplateRef:
    name: {workload_name}
"""
    log(f"agent-sandbox: deploying SandboxWarmPool {warm_pool_name} with {worker_count} replicas")
    stream(cluster.kubectl("apply", "-f", "-"), input=warm_pool_yaml)
    wait_warm_pool_ready(cluster, warm_pool_name, worker_count, timeout)


def wait_warm_pool_ready(cluster: Cluster, warm_pool_name: str, worker_count: int, timeout: int = 300) -> None:
    ns = "benchmark-workloads"
    log(f"agent-sandbox: waiting for {warm_pool_name} to pre-warm {worker_count} sandboxes (timeout: {timeout}s)")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cmd = cluster.kubectl("get", "sandboxwarmpool", warm_pool_name, "-n", ns, "-o", "jsonpath={.status.readyReplicas}")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                ready = int(proc.stdout.strip())
                if ready >= worker_count:
                    log(f"agent-sandbox: {warm_pool_name} ready with {ready}/{worker_count} sandboxes")
                    return
                log(f"agent-sandbox: {warm_pool_name} has {ready}/{worker_count} ready sandboxes...")
            except ValueError:
                pass
        time.sleep(5)
    raise AgentSandboxError(f"timed out waiting for {warm_pool_name} to reach {worker_count} ready sandboxes")


def recreate_warm_pool(cluster: Cluster, workload_name: str, worker_count: int, timeout: int = 300) -> None:
    ns = "benchmark-workloads"
    warm_pool_name = f"{workload_name}-warm-pool"
    stream(cluster.kubectl("delete", "sandboxwarmpool", warm_pool_name, "-n", ns, "--ignore-not-found"))
    warm_pool_yaml = f"""apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxWarmPool
metadata:
  name: {warm_pool_name}
  namespace: {ns}
spec:
  replicas: {worker_count}
  sandboxTemplateRef:
    name: {workload_name}
"""
    log(f"agent-sandbox: recreating SandboxWarmPool {warm_pool_name} with {worker_count} replicas")
    stream(cluster.kubectl("apply", "-f", "-"), input=warm_pool_yaml)
    wait_warm_pool_ready(cluster, warm_pool_name, worker_count, timeout)


def delete_workload_and_pool(cluster: Cluster, workload_name: str) -> None:
    ns = "benchmark-workloads"
    warm_pool_name = f"{workload_name}-warm-pool"
    stream(cluster.kubectl("delete", "sandboxwarmpool", warm_pool_name, "-n", ns, "--ignore-not-found"))
    stream(cluster.kubectl("delete", "sandboxtemplate", workload_name, "-n", ns, "--ignore-not-found"))
    if workload_name == "openclaw":
        from .workloads import openclaw
        openclaw.delete_mock_llm(cluster)
