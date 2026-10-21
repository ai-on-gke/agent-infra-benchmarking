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

"""Deploys agent-substrate/env, from its submodule and the images builder.py
pushes of it, on a case's cluster for the sandbox-benchmark: the ate-env-api service and a WorkerPool,
as env's own `ate-env manifest` writes them, and the py312 ActorTemplate, as
`ate-env manifest template` writes it, running the benchmark's rootfs image.
env writes them for a stock cluster's gVisor; what is overridden for this
one is said where it is. Or deletes them."""

from __future__ import annotations

import functools
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from typing import Callable, Iterator

import yaml

from . import RUNNER_DIR, images, substrate, worker_pool, workloads
from .cluster import Cluster
from .log import log
from .process import output, stream

DEPENDENCY_REPOS = RUNNER_DIR.parent.parent / "dependency-repos"
ENV_DIR = DEPENDENCY_REPOS / "env"
ENV_PATH = ENV_DIR.relative_to(images.ROOT).as_posix()
# Pins the env commit its ate_env client and the API it calls are from.
PROVIDER = DEPENDENCY_REPOS / "ai-sandbox-benchmark" / "providers" / "substrate.py"
PACKAGES = ["./cmd/ate-env-api", "./cmd/ate-env-guest"]
# env's defaults. ate-env-api creates the environments' atespace as it starts.
NAMESPACE = "ate-env"
ENV_ATESPACE = "ate-env"
API = "ate-env-api"
API_PORT = 7777
API_ADDR = f"{API}.{NAMESPACE}.svc.cluster.local:{API_PORT}"
# In ENV_ATESPACE, as env looks a template up in the environment's atespace.
TEMPLATE = "py312"
# ate-env manifest's name for the template's pool.
POOL = f"{TEMPLATE}-workerpool"
# What the template's workerSelector picks, so its actors keep off other pools.
POOL_LABELS = {"workload": "sandbox-benchmark-ateom"}
ROOTFS_IMAGE = "sandbox-benchmark-rootfs"
# numpy, scipy and pandas do not fit workloads' 256Mi default.
DEFAULT_ACTOR_MEMORY = "1Gi"
DEFAULT_TIMEOUT_SECS = 300

AteEnv = Callable[..., list[str]]


class EnvError(RuntimeError):
    pass


def missing_requirements() -> list[str]:
    missing = [tool for tool in ("go", "git", "kubectl") if not shutil.which(tool)]
    if not (ENV_DIR / "go.mod").is_file():
        missing.append(f"the env submodule in {ENV_DIR}")
    # kubectl-ate is substrate's, built in its submodule.
    return list(dict.fromkeys(missing + workloads.missing_requirements()))


def check_client() -> None:
    """Refuses an env submodule at another commit than the one the provider's
    client is pinned to."""
    pinned = re.search(r'^ENV_COMMIT = "([0-9a-f]{40})"', PROVIDER.read_text(), re.MULTILINE)
    head = output(["git", "rev-parse", "HEAD"], cwd=ENV_DIR).strip()
    if not pinned:
        raise EnvError(f"no ENV_COMMIT in {PROVIDER}")
    if pinned[1] != head:
        raise EnvError(f"the env submodule is at {head}, but {PROVIDER.name}'s client is pinned to {pinned[1]}")


@functools.cache
def build_version() -> str:
    """What env's images are tagged, read once per run: git describe in the
    submodule, as substrate's build version is, so they are built again only
    once it moves."""
    describe = ["git", "describe", "--tags", "--always", "--dirty"]
    proc = subprocess.run(describe, cwd=ENV_DIR, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "dev"


def image_refs(repo: str) -> list[str]:
    """ate-env-api and ate-env-guest in repo, which builder.py pushes, each
    named after the last element of its package as ko --base-import-paths
    names it."""
    repo = images.group_repo(repo, "env")
    return [images.ref(repo, package.rsplit("/", 1)[-1], build_version()) for package in PACKAGES]


def pinned_image(repo: str, name: str) -> str:
    """One of image_refs, pinned."""
    return images.pin(images.ref(images.group_repo(repo, "env"), name, build_version()))


@contextmanager
def ate_env() -> Iterator[AteEnv]:
    """Builds env's CLI once and yields what makes a command of it."""
    with tempfile.TemporaryDirectory() as tmp:
        binary = f"{tmp}/ate-env"
        stream(["go", "build", "-o", binary, "./cmd/ate-env"], cwd=ENV_DIR)
        yield lambda *args: [binary, *args]


def render_stack(cli: AteEnv, cluster: Cluster, sandbox_class: str, worker_count: int, api_image: str) -> str:
    """env's namespace, WorkerPool and ate-env-api, with the pool fitted to the
    cluster and the case's sandbox class."""
    manifest = output(
        cli(
            "manifest",
            f"--namespace={NAMESPACE}",
            f"--template={TEMPLATE}",
            f"--api-image={api_image}",
            # substrate.resolve pins it to its image in the repository.
            f"--worker-image=ko://github.com/agent-substrate/substrate/cmd/ateom-{sandbox_class}",
            f"--replicas={worker_count}",
        )
    )
    docs = list(yaml.safe_load_all(manifest))
    pool = next(doc for doc in docs if doc["kind"] == "WorkerPool")
    pool["metadata"].setdefault("labels", {}).update(POOL_LABELS)
    # env's pool is gvisor-default's alone.
    config_name, _ = workloads.SANDBOX_CONFIGS[sandbox_class]
    pool["spec"]["sandboxClasses"] = [{"name": sandbox_class, "configRef": {"name": config_name}}]
    # env's workers go on any node; the sandboxed nodes are the benchmarking
    # workers, tainted with their class, as in worker-pool.yaml.tmpl.
    pool["spec"]["template"] = {
        "nodeSelector": {"benchmarking-worker": "true"},
        "tolerations": [
            {"key": "ate.dev/sandboxClass", "operator": "Equal", "value": sandbox_class, "effect": "NoSchedule"}
        ],
    }
    return substrate.resolve(cluster, yaml.safe_dump_all(docs, sort_keys=False))


def render_template(
    cli: AteEnv, cluster: Cluster, sandbox_class: str, guest_image: str, rootfs_image: str, actor_memory: str
) -> str:
    """env's ActorTemplate running rootfs_image, with the guest mounted into
    it, on the case's sandbox class and the pool render_stack labels."""
    manifest = output(
        cli(
            "manifest",
            "template",
            f"--template={TEMPLATE}",
            f"--atespace={ENV_ATESPACE}",
            f"--guest-image={guest_image}",
            f"--rootfs-image={rootfs_image}",
            f"--snapshots-bucket=gs://{cluster.bucket}/{ENV_ATESPACE}/{TEMPLATE}/",
        )
    )
    template = yaml.safe_load(manifest)
    config_name, class_enum = workloads.SANDBOX_CONFIGS[sandbox_class]
    template["sandboxConfig"] = {"sandboxClass": class_enum, "configName": config_name}
    template["workerSelector"] = {"matchLabels": POOL_LABELS}
    # Unset, microvm gives every actor 2 GiB.
    template["resources"] = {"limits": [{"name": "memory", "quantity": actor_memory}]}
    return yaml.safe_dump(template, sort_keys=False)


def deploy(
    cluster: Cluster,
    image_repo: str,
    sandbox_class: str,
    worker_count: int,
    actor_memory: str = DEFAULT_ACTOR_MEMORY,
    timeout: int = DEFAULT_TIMEOUT_SECS,
) -> None:
    """Deploys env and the template from their images in image_repo, image_refs
    and the sandbox-benchmark's rootfs of this tree, and waits
    for the template's golden snapshot. Always a new pool, so no run starts
    on workers the last one warmed."""
    check_client()
    api_image, guest_image = pinned_image(image_repo, "ate-env-api"), pinned_image(image_repo, "ate-env-guest")
    rootfs_image = images.pin(images.image_ref(images.SANDBOX_BENCHMARK_IMAGES, image_repo, ROOTFS_IMAGE))
    with ate_env() as cli:
        stack = render_stack(cli, cluster, sandbox_class, worker_count, api_image)
        template = render_template(cli, cluster, sandbox_class, guest_image, rootfs_image, actor_memory)

    worker_pool.delete(cluster, timeout, POOL, NAMESPACE)
    log(f"env: deploying {API} and {POOL} (replicas={worker_count}, sandbox_class={sandbox_class}) in {NAMESPACE}")
    stream(cluster.kubectl("apply", "-f", "-"), input=stack)
    stream(cluster.kubectl("rollout", "status", f"deployment/{API}", f"--timeout={timeout}s", namespace=NAMESPACE))
    worker_pool.wait_ready(cluster, timeout, POOL, NAMESPACE)

    log(
        f"env: registering {ENV_ATESPACE}/{TEMPLATE} (rootfs={rootfs_image}, actor_memory={actor_memory},"
        f" bucket=gs://{cluster.bucket}/{ENV_ATESPACE}/{TEMPLATE}/)"
    )
    with workloads.kubectl_ate(cluster) as ate:
        # The store wants a template's atespace to exist when it is created.
        if subprocess.run(ate("create", "atespace", ENV_ATESPACE), capture_output=True).returncode:
            output(ate("get", "atespace", ENV_ATESPACE))
        # Templates are immutable. Deleting one also deletes its golden actor and snapshot.
        workloads.delete_template(ate, TEMPLATE, ENV_ATESPACE)
        stream(ate("create", "actor-template", "-f", "-"), input=template)
        workloads.wait_golden_snapshot(ate, cluster, TEMPLATE, timeout, ENV_ATESPACE, (NAMESPACE, POOL))
    log(f"env: {ENV_ATESPACE}/{TEMPLATE} ready")


def delete(cluster: Cluster, timeout: int = DEFAULT_TIMEOUT_SECS) -> None:
    """Deletes the template, the pool, and env's namespace. The atespace is
    left to ate-env-api, which creates it."""
    log("env: deleting")
    with workloads.kubectl_ate(cluster) as ate:
        workloads.delete_template(ate, TEMPLATE, ENV_ATESPACE)
    worker_pool.delete(cluster, timeout, POOL, NAMESPACE)
    stream(cluster.kubectl("delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=true"))
