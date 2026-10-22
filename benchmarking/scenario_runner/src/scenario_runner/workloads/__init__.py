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

"""Deploys the benchmark ActorTemplates of
workloads/manifests/<name>-template.yaml.tmpl onto the worker pool, with what
they call (openclaw's mock-llm), and waits for their golden snapshots; or
deletes them."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from string import Template
from typing import Callable, Iterator

from .. import RUNNER_DIR, images
from ..control_planes import substrate, worker_pool
from ..infra.cluster import Cluster
from ..log import log
from ..process import output, stream
from . import openclaw

MANIFESTS_DIR = RUNNER_DIR.parent / "workloads" / "manifests"
NAMESPACE = worker_pool.NAMESPACE
ATESPACE = "benchmark-workloads"
# The templates whose image is neither ko:// nor a literal, but one of ours.
TEMPLATE_IMAGES = {"openclaw": "openclaw-workload"}
# The templates whose actors call mock-llm rather than a real LLM, which needs
# a node pool of its own (cluster_configs.MOCK_LLM_POOL).
MOCK_LLM_TEMPLATES = {"openclaw"}
# Pinned rather than defaulted, so a stale SandboxConfig fails loudly. The
# templates take the class as its proto enum.
SANDBOX_CONFIGS = {
    "gvisor": ("gvisor-default", "SANDBOX_CLASS_GVISOR"),
    "microvm": ("microvm", "SANDBOX_CLASS_MICROVM"),
}
# The smallest microvm admits, so actors don't get kata's 2 GiB and its page
# cache in every snapshot.
DEFAULT_ACTOR_MEMORY = "256Mi"
# A case's `actor-memory` unless it states one, by workload.
DEFAULT_ACTOR_MEMORY_BY_WORKLOAD = {"openclaw": "2Gi"}
# The least memory a workload's actors can do their work in, by workload and
# sandbox class. openclaw's Node.js agent is OOM-killed at 256Mi under gVisor,
# and at 1Gi in a microvm, whose guest gets the limit less the VMM's share.
MIN_ACTOR_MEMORY_MIB = {"openclaw": {"gvisor": 1024, "microvm": 2048}}
# Kubernetes quantity suffixes, in bytes.
QUANTITY_UNITS = {"": 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}
DEFAULT_TIMEOUT_SECS = 300
POLL_SECS = 5
PROGRESS_EVERY_SECS = 30

KubectlAte = Callable[..., list[str]]


class WorkloadError(RuntimeError):
    pass


def missing_requirements() -> list[str]:
    missing = [tool for tool in ("kubectl", "go", "git", "gcloud") if not shutil.which(tool)]
    # kubectl-ate is substrate's, built in the submodule.
    return missing + substrate.missing_submodule()


def templates() -> list[str]:
    return sorted(p.name.removesuffix("-template.yaml.tmpl") for p in MANIFESTS_DIR.glob("*-template.yaml.tmpl"))


def check_templates(names: list[str]) -> None:
    unknown = [name for name in names if name not in templates()]
    if unknown:
        raise ValueError(f"no workload template {', '.join(unknown)}; there are {', '.join(templates())}")


@contextmanager
def kubectl_ate(cluster: Cluster) -> Iterator[KubectlAte]:
    """Builds kubectl-ate once, as it runs on every poll, and yields what makes
    a command of it for the cluster."""
    with tempfile.TemporaryDirectory() as tmp:
        binary = f"{tmp}/kubectl-ate"
        stream(["go", "build", "-o", binary, "./cmd/kubectl-ate"], cwd=substrate.SUBSTRATE_DIR)
        yield lambda *args: [binary, *args, f"--kubeconfig={cluster.kubeconfig}"]


def default_actor_memory(workload: str | None) -> str:
    return DEFAULT_ACTOR_MEMORY_BY_WORKLOAD.get(workload or "", DEFAULT_ACTOR_MEMORY)


def min_actor_memory_mib(workload: str | None, sandbox_class: str) -> int | None:
    return MIN_ACTOR_MEMORY_MIB.get(workload or "", {}).get(sandbox_class)


def pinned_image(image_repo: str, name: str) -> str:
    """The image built from this tree into image_repo, pinned."""
    return images.pin(images.image_ref(images.WORKLOADS_IMAGES, image_repo, name))


def otlp_endpoint(cluster: Cluster) -> str:
    """The endpoint the control plane sends its telemetry to, so the actors
    follow it on any kind of cluster."""
    get = cluster.kubectl(
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
            cluster.kubectl(
                "get", "workerpool", worker_pool.NAME, "-o=jsonpath={.spec.sandboxClasses[*].name}", namespace=NAMESPACE
            )
        ).strip()
    except subprocess.CalledProcessError:
        raise WorkloadError(f"no {worker_pool.NAME} WorkerPool in {NAMESPACE}") from None
    if pool_class != sandbox_class:
        raise WorkloadError(f"the {worker_pool.NAME} WorkerPool is {pool_class}, not {sandbox_class}")
    rollout = ["rollout", "status", f"deployment/{worker_pool.NAME}", f"--timeout={timeout}s"]
    stream(cluster.kubectl(*rollout, namespace=NAMESPACE))


def render_template(
    cluster: Cluster, name: str, sandbox_class: str, actor_memory: str, otlp: str, image_repo: str
) -> str:
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
        values["IMAGE_WITH_DIGEST"] = pinned_image(image_repo, TEMPLATE_IMAGES[name])
    return Template((MANIFESTS_DIR / f"{name}-template.yaml.tmpl").read_text()).substitute(values)


def delete_template(ate: KubectlAte, name: str, atespace: str = ATESPACE) -> None:
    subprocess.run(ate("delete", "actor-template", name, "-a", atespace), capture_output=True)


def wait_golden_snapshot(
    ate: KubectlAte,
    cluster: Cluster,
    name: str,
    timeout: int,
    atespace: str = ATESPACE,
    pool: tuple[str, str] = (NAMESPACE, worker_pool.NAME),
) -> None:
    """Ready once the controller publishes the golden tag, which is when an
    actor can be restored from it; the snapshot existing is not enough. pool is
    the namespace and name of the WorkerPool the golden actor runs on."""
    started = time.monotonic()
    next_report = started + PROGRESS_EVERY_SECS
    status, error = None, ""
    while (elapsed := int(time.monotonic() - started)) < timeout:
        get = ate("get", "actor-template", name, "-a", atespace, "-o", "json")
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
                raise WorkloadError(f"actor template {atespace}/{name} failed: {golden['errorMessage']}")
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
    namespace, pool_name = pool
    raise WorkloadError(
        f"no golden snapshot of {atespace}/{name} within {timeout}s; check that gs://{cluster.bucket}/ exists"
        f" and the workers can write to it, then the workers: kubectl get pods -n {namespace}"
        f" --kubeconfig={cluster.kubeconfig}; kubectl logs -n {namespace} deployment/{pool_name} --tail=50"
        f" --kubeconfig={cluster.kubeconfig}"
    )


def deploy(
    cluster: Cluster,
    names: list[str],
    sandbox_class: str,
    image_repo: str,
    actor_memory: str = DEFAULT_ACTOR_MEMORY,
    otlp: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECS,
) -> None:
    """image_repo holds the workload images built from this tree. otlp is where an instrumented actor sends its telemetry; by
    default the control plane's endpoint."""
    if not names:
        raise ValueError("no workload template to deploy")
    check_templates(names)
    check_worker_pool(cluster, sandbox_class, timeout)
    otlp = otlp or otlp_endpoint(cluster)
    # A template that sets its own limit, as openclaw's does, ignores actor_memory.
    sized = [n for n in names if "${ACTOR_MEMORY}" in (MANIFESTS_DIR / f"{n}-template.yaml.tmpl").read_text()]
    log(
        f"workloads: deploying {', '.join(names)} (sandbox_class={sandbox_class},"
        f" actor_memory={actor_memory} for {', '.join(sized) or 'none of them'},"
        f" otlp_endpoint={otlp}, bucket=gs://{cluster.bucket}/{ATESPACE}/, substrate={substrate.revision()})"
    )
    if MOCK_LLM_TEMPLATES & set(names):
        openclaw.deploy_mock_llm(cluster, image_repo, timeout)

    with kubectl_ate(cluster) as ate:
        # The store wants a template's atespace to exist when it is created.
        if subprocess.run(ate("create", "atespace", ATESPACE), capture_output=True).returncode:
            output(ate("get", "atespace", ATESPACE))
        for name in names:
            # Templates are immutable, and the endpoint and class change between
            # runs. Deleting one also deletes its golden actor and snapshot.
            delete_template(ate, name)
            manifest = render_template(cluster, name, sandbox_class, actor_memory, otlp, image_repo)
            stream(ate("create", "actor-template", "-f", "-"), input=substrate.resolve(cluster, manifest))
        for i, name in enumerate(names, 1):
            log(f"workloads: [{i}/{len(names)}] waiting for the {ATESPACE}/{name} golden snapshot (timeout {timeout}s)")
            wait_golden_snapshot(ate, cluster, name, timeout)
    log(f"workloads: all {len(names)} golden snapshots ready")


def deployed_template(cluster: Cluster, name: str, atespace: str = ATESPACE) -> dict:
    """The ActorTemplate as substrate holds it."""
    with kubectl_ate(cluster) as ate:
        try:
            return json.loads(output(ate("get", "actor-template", name, "-a", atespace, "-o", "json")))
        except subprocess.CalledProcessError:
            raise WorkloadError(f"no actor template {atespace}/{name}; deploy the workloads first") from None


def egress_hosts(template: dict) -> tuple[list[str], list[str]]:
    """The hosts of the URLs in the template's container env: what its actors
    are configured to call, such as the OTLP collector and openclaw's mock-llm.
    Substrate refuses every connection from an actor without an egress policy,
    and the worker gives each one a policy that allows these: the first list's
    over cleartext HTTP (gRPC included), the second's, from https:// URLs,
    over TLS, which a cleartext rule does not let through."""
    cleartext, tls = set(), set()
    for container in template.get("containers") or []:
        for env in container.get("env") or []:
            url = urllib.parse.urlsplit(env.get("value") or "")
            if url.scheme and url.hostname:
                (tls if url.scheme == "https" else cleartext).add(url.hostname)
    return sorted(cleartext), sorted(tls)


def memory_mib(quantity: str) -> float | None:
    """A Kubernetes memory quantity in MiB, or None if it cannot be read."""
    match = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", quantity or "")
    if not match or match[2] not in QUANTITY_UNITS:
        return None
    return float(match[1]) * QUANTITY_UNITS[match[2]] / 2**20


def cpu_cores(quantity: str) -> float:
    """A Kubernetes cpu quantity, such as "2" or "1500m", in cores."""
    return int(quantity[:-1]) / 1000 if quantity.endswith("m") else float(quantity)


def check_deployed(template: dict, name: str, sandbox_class: str, workload: str, actor_memory: str) -> None:
    """Refuses a deployed template the case cannot run on: one of another
    sandbox class, whose actors never schedule on the case's pool, or with
    less memory than the workload needs. Matters when the templates were not
    deployed by this run (--no-deploy-workloads)."""
    want = SANDBOX_CONFIGS[sandbox_class][1]
    have = (template.get("sandboxConfig") or {}).get("sandboxClass")
    if have != want:
        raise WorkloadError(f"actor template {ATESPACE}/{name} is {have or 'of no sandbox class'}, not {want}; deploy it again")
    limits = (template.get("resources") or {}).get("limits") or []
    quantity = next((limit.get("quantity") for limit in limits if limit.get("name") == "memory"), None)
    if quantity and memory_mib(quantity) != memory_mib(actor_memory):
        raise WorkloadError(
            f"actor template {ATESPACE}/{name} limits actors to {quantity}, but the case's actor-memory is"
            f" {actor_memory}; deploy it again"
        )


def delete(cluster: Cluster, names: list[str] | None = None) -> None:
    """Deletes the named templates, by default all, then their atespace unless
    other templates are left in it, and mock-llm with openclaw's."""
    check_templates(names or [])
    log("workloads: deleting")
    with kubectl_ate(cluster) as ate:
        for name in names or templates():
            delete_template(ate, name)
        if subprocess.run(ate("delete", "atespace", ATESPACE), capture_output=True).returncode:
            log(f"workloads: atespace {ATESPACE} not deleted; it may not exist or not be empty")
    if "openclaw" in (names or templates()):
        openclaw.delete_mock_llm(cluster)
