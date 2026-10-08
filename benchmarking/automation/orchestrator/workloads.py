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

"""Deploys mock-llm and the benchmark ActorTemplates of
workloads/manifests/<name>-template.yaml.tmpl onto the worker pool, and waits
for their golden snapshots; or deletes them."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from string import Template
from typing import Callable, Iterator

from . import AUTOMATION_DIR, images, substrate, worker_pool
from .cluster import Cluster
from .log import log
from .process import output, stream

MANIFESTS_DIR = AUTOMATION_DIR.parent / "workloads" / "manifests"
NAMESPACE = worker_pool.NAMESPACE
ATESPACE = "benchmark-workloads"
MOCK_LLM_IMAGE = "mock-llm"
# The templates whose image is neither ko:// nor a literal, but one of ours.
TEMPLATE_IMAGES = {"openclaw": "openclaw-workload"}
# Pinned rather than defaulted, so a stale SandboxConfig fails loudly. The
# templates take the class as its proto enum.
SANDBOX_CONFIGS = {
    "gvisor": ("gvisor-default", "SANDBOX_CLASS_GVISOR"),
    "microvm": ("microvm", "SANDBOX_CLASS_MICROVM"),
}
# The smallest microvm admits, so actors don't get kata's 2 GiB and its page
# cache in every snapshot.
DEFAULT_ACTOR_MEMORY = "256Mi"
DEFAULT_TIMEOUT_SECS = 300
POLL_SECS = 5
PROGRESS_EVERY_SECS = 30

KubectlAte = Callable[..., list[str]]


class WorkloadError(RuntimeError):
    pass


def missing_requirements() -> list[str]:
    missing = [tool for tool in ("kubectl", "go", "git", "gcloud") if not shutil.which(tool)]
    # kubectl-ate and ko are substrate's, built and run in the submodule.
    if not (substrate.SUBSTRATE_DIR / "go.mod").is_file():
        missing.append(f"the substrate submodule in {substrate.SUBSTRATE_DIR}")
    return missing


def templates() -> list[str]:
    return sorted(p.name.removesuffix("-template.yaml.tmpl") for p in MANIFESTS_DIR.glob("*-template.yaml.tmpl"))


def check_templates(names: list[str]) -> None:
    unknown = [name for name in names if name not in templates()]
    if unknown:
        raise ValueError(f"no workload template {', '.join(unknown)}; there are {', '.join(templates())}")


def kubectl(cluster: Cluster, *args: str, namespace: str = NAMESPACE) -> list[str]:
    return ["kubectl", *args, f"--kubeconfig={cluster.kubeconfig}", f"--namespace={namespace}"]


@contextmanager
def kubectl_ate(cluster: Cluster) -> Iterator[KubectlAte]:
    """Builds kubectl-ate once, as it runs on every poll, and yields what makes
    a command of it for the cluster."""
    with tempfile.TemporaryDirectory() as tmp:
        binary = f"{tmp}/kubectl-ate"
        stream(["go", "build", "-o", binary, "./cmd/kubectl-ate"], cwd=substrate.SUBSTRATE_DIR)
        yield lambda *args: [binary, *args, f"--kubeconfig={cluster.kubeconfig}"]


def pinned_image(cluster: Cluster, name: str) -> str:
    """The image images.build_and_push built from this commit, pinned."""
    return images.pin(images.image_ref(images.WORKLOADS_IMAGES, cluster.docker_repo, name))


def otlp_endpoint(cluster: Cluster) -> str:
    """The endpoint the control plane sends its telemetry to, so the actors
    follow it on any kind of cluster."""
    get = kubectl(
        cluster,
        "get",
        "configmap",
        "ate-otel-config",
        "-o=jsonpath={.data.OTEL_EXPORTER_OTLP_ENDPOINT}",
        namespace="ate-system",
    )
    try:
        endpoint = output(get).strip()
    except subprocess.CalledProcessError:
        endpoint = ""
    # No default: it would send the actors' telemetry to the wrong collector
    # without a word.
    if not endpoint:
        raise WorkloadError(
            "no OTEL_EXPORTER_OTLP_ENDPOINT in ate-system's ate-otel-config ConfigMap;"
            " install substrate first, or pass otlp"
        )
    return endpoint


def check_worker_pool(cluster: Cluster, sandbox_class: str, timeout: int) -> None:
    """The golden snapshots are taken on the pool's workers."""
    try:
        pool_class = output(
            kubectl(cluster, "get", "workerpool", worker_pool.NAME, "-o=jsonpath={.spec.sandboxClass}")
        ).strip()
    except subprocess.CalledProcessError:
        raise WorkloadError(f"no {worker_pool.NAME} WorkerPool in {NAMESPACE}") from None
    if pool_class != sandbox_class:
        raise WorkloadError(f"the {worker_pool.NAME} WorkerPool is {pool_class}, not {sandbox_class}")
    stream(kubectl(cluster, "rollout", "status", f"deployment/{worker_pool.NAME}", f"--timeout={timeout}s"))


def deploy_mock_llm(cluster: Cluster, timeout: int) -> None:
    log("workloads: deploying mock-llm")
    image = pinned_image(cluster, MOCK_LLM_IMAGE)
    manifest = Template((MANIFESTS_DIR / "mock-llm.yaml.tmpl").read_text()).substitute(IMAGE_WITH_DIGEST=image)
    stream(kubectl(cluster, "apply", "-f", "-"), input=manifest)
    log(f"workloads: waiting for mock-llm to be ready (timeout: {timeout}s)")
    stream(kubectl(cluster, "rollout", "status", "deployment/mock-llm", f"--timeout={timeout}s"))


def render_template(cluster: Cluster, name: str, sandbox_class: str, actor_memory: str, otlp: str) -> str:
    config_name, class_enum = SANDBOX_CONFIGS[sandbox_class]
    values = {
        "BUCKET_NAME": cluster.bucket,
        "PROJECT_ID": cluster.project_id,
        "SANDBOX_CLASS": sandbox_class,
        "SANDBOX_CLASS_ENUM": class_enum,
        "SANDBOX_CONFIG_NAME": config_name,
        "OTLP_ENDPOINT": otlp,
        "ACTOR_MEMORY": actor_memory,
    }
    if name in TEMPLATE_IMAGES:
        values["IMAGE_WITH_DIGEST"] = pinned_image(cluster, TEMPLATE_IMAGES[name])
    return Template((MANIFESTS_DIR / f"{name}-template.yaml.tmpl").read_text()).substitute(values)


def delete_template(ate: KubectlAte, name: str) -> None:
    subprocess.run(ate("delete", "actor-template", name, "-a", ATESPACE), capture_output=True)


def wait_golden_snapshot(ate: KubectlAte, cluster: Cluster, name: str, timeout: int) -> None:
    """Ready once the controller publishes the golden tag, which is when an
    actor can be restored from it; the snapshot existing is not enough."""
    started = time.monotonic()
    next_report = started + PROGRESS_EVERY_SECS
    status, error = None, ""
    while (elapsed := int(time.monotonic() - started)) < timeout:
        get = ate("get", "actor-template", name, "-a", ATESPACE, "-o", "json")
        proc = subprocess.run(get, capture_output=True, text=True)
        try:
            template = json.loads(proc.stdout) if proc.returncode == 0 else None
        except ValueError:
            template, proc.stderr = None, f"unreadable output: {proc.stdout.strip()}"
        if template is not None:
            error = ""
            status = template.get("status") or {}
            golden = status.get("goldenSnapshotStatus") or {}
            tag = golden.get("goldenTag") or {}
            if tag.get("name"):
                log(f"  {name}: golden snapshot ready after {elapsed}s, tag {tag.get('atespace', '')}/{tag['name']}")
                return
            if golden.get("errorMessage"):
                raise WorkloadError(f"actor template {ATESPACE}/{name} failed: {golden['errorMessage']}")
        else:
            # Kept, or a get failing every time looks like a slow snapshot.
            error = proc.stderr.strip()
        if time.monotonic() >= next_report:
            if error:
                log(f"  {name}: {elapsed}s/{timeout}s, get failing: {error}")
            else:
                due = (status or {}).get("goldenSnapshotStatus", {}).get("takeGoldenSnapshotAt")
                log(f"  {name}: {elapsed}s/{timeout}s, no golden snapshot yet{f' (due at {due})' if due else ''}")
            next_report += PROGRESS_EVERY_SECS
        time.sleep(POLL_SECS)

    if error:
        log(f"  every get failed, the last with: {error}", "ERROR")
    elif status is not None:
        log(f"  neither a snapshot nor an error; last status: {json.dumps(status)}", "ERROR")
    # The golden actor's snapshot is published as a tag under the bucket.
    raise WorkloadError(
        f"no golden snapshot of {ATESPACE}/{name} within {timeout}s; check that gs://{cluster.bucket}/ exists"
        f" and the workers can write to it, then the workers: kubectl get pods -n {NAMESPACE};"
        f" kubectl logs -n {NAMESPACE} deployment/{worker_pool.NAME} --tail=50"
    )


def deploy(
    cluster: Cluster,
    names: list[str],
    sandbox_class: str,
    actor_memory: str = DEFAULT_ACTOR_MEMORY,
    otlp: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECS,
) -> None:
    """otlp is where an instrumented actor sends its telemetry; by default the
    control plane's endpoint."""
    if not names:
        raise ValueError("no workload template to deploy")
    check_templates(names)
    check_worker_pool(cluster, sandbox_class, timeout)
    otlp = otlp or otlp_endpoint(cluster)
    log(
        f"workloads: deploying {', '.join(names)} (sandbox_class={sandbox_class}, actor_memory={actor_memory},"
        f" otlp_endpoint={otlp}, bucket=gs://{cluster.bucket}/{ATESPACE}/, substrate={substrate.revision()})"
    )
    deploy_mock_llm(cluster, timeout)

    with kubectl_ate(cluster) as ate:
        # The store wants a template's atespace to exist when it is created.
        if subprocess.run(ate("create", "atespace", ATESPACE), capture_output=True).returncode:
            output(ate("get", "atespace", ATESPACE))
        for name in names:
            # Templates are immutable, and the endpoint and class change between
            # runs. Deleting one also deletes its golden actor and snapshot.
            delete_template(ate, name)
            manifest = render_template(cluster, name, sandbox_class, actor_memory, otlp)
            # ko builds the ko:// images and puts their pushed digests in.
            resolved = substrate.ko(cluster, "resolve", "-f", "-", input=manifest)
            stream(ate("create", "actor-template", "-f", "-"), input=resolved)
        for i, name in enumerate(names, 1):
            log(f"workloads: [{i}/{len(names)}] waiting for the {ATESPACE}/{name} golden snapshot (timeout {timeout}s)")
            wait_golden_snapshot(ate, cluster, name, timeout)
    log(f"workloads: all {len(names)} golden snapshots ready")


def delete(cluster: Cluster, names: list[str] | None = None) -> None:
    """Deletes the named templates, by default all, then their atespace unless
    other templates are left in it, and mock-llm."""
    check_templates(names or [])
    log("workloads: deleting")
    with kubectl_ate(cluster) as ate:
        for name in names or templates():
            delete_template(ate, name)
        if subprocess.run(ate("delete", "atespace", ATESPACE), capture_output=True).returncode:
            log(f"workloads: atespace {ATESPACE} not deleted; it may not exist or not be empty")
    try:
        stream(kubectl(cluster, "delete", "deployment,service", "mock-llm", "--ignore-not-found"))
    except subprocess.CalledProcessError:
        log("workloads: mock-llm not deleted")
