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

"""Deploys ai-on-gke/sandboxenv-rl, from its submodule and the images
builder.py pushes of it, on a case's cluster for the swebench case type: the
ate-env-api service and a WorkerPool, as sandboxenv-rl's own `ate-env
manifest` writes them, and the default-template ActorTemplate, as `ate-env
manifest template` writes it, which ate-env-api derives the template of each
task image from. sandboxenv-rl writes them for a stock cluster's gVisor; what
is overridden for this one is said where it is.

Unlike the agent-substrate/env that ate_env.py deploys, ate-env-api here
refuses callers without a bearer token, is let manage environments by an access
policy of its atespace, and dials the router over mTLS, whose allow-list has to
hold ate-env-api's SPIFFE ID, API_SPIFFE_ID: substrate is installed with it
(ingress_spiffe_ids of the case). So deploy makes the atespace and its policy
and the token's Secret before ate-env-api starts."""

from __future__ import annotations

import base64
import functools
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from typing import Callable, Iterator

import yaml

from ... import RUNNER_DIR, git, images, workloads
from ...control_planes import substrate, worker_pool
from ...infra.cluster import Cluster
from ...log import log
from ...process import output, retry, stream

DEPENDENCY_REPOS = RUNNER_DIR.parent.parent / "dependency-repos"
DIR = DEPENDENCY_REPOS / "sandboxenv-rl"
PATH = DIR.relative_to(images.ROOT).as_posix()
# The folder of the images repository its images go in, as builder.py has it.
GROUP = "sandboxenv-rl"
PACKAGES = ["./cmd/ate-env-api", "./cmd/ate-env-guest"]
# sandboxenv-rl's defaults. ate-env-api's atespace has to exist and have its policy.
NAMESPACE = "ate-env"
ATESPACE = "ate-env"
API = "ate-env-api"
API_PORT = 7777
API_ADDR = f"{API}.{NAMESPACE}.svc.cluster.local:{API_PORT}"
# What ate-env-api presents to the atenet router, as `ate-env manifest` says.
API_SPIFFE_ID = f"spiffe://cluster.local/ns/{NAMESPACE}/sa/{API}"
# The base template ate-env-api derives each image's from, and ate-env manifest's
# name for its pool.
TEMPLATE = "default-template"
POOL = f"{TEMPLATE}-workerpool"
# The Secret ate-env manifest has ate-env-api read its caller token from, key token.
AUTH_SECRET = "ate-env-api-token"
# ate-env manifest template's own defaults.
DEFAULT_ACTOR_MEMORY = "4Gi"
DEFAULT_ACTOR_CPU = "2"
DEFAULT_API_REPLICAS = 1
DEFAULT_TIMEOUT_SECS = 300

AteEnv = Callable[..., list[str]]


class SandboxenvError(RuntimeError):
    pass


def missing_requirements() -> list[str]:
    missing = [tool for tool in ("go", "git", "kubectl") if not shutil.which(tool)]
    if not (DIR / "go.mod").is_file():
        missing.append(f"the sandboxenv-rl submodule in {DIR}")
    # kubectl-ate is substrate's, built in its submodule.
    return list(dict.fromkeys(missing + workloads.missing_requirements()))


def pinned_commit() -> str:
    """The submodule commit the repo pins: in its index, which is HEAD's
    unless a new one is staged."""
    entry = output(["git", "ls-files", "--stage", "--", PATH], cwd=images.ROOT).split()
    if len(entry) < 2 or entry[0] != "160000":
        raise SandboxenvError(f"the repo pins no submodule at {PATH}")
    return entry[1]


def check_pinned() -> None:
    """Refuses a submodule checked out at another commit than the one the
    repo pins, as after a pull without git submodule update: the run would
    deploy and benchmark that other sandboxenv-rl. A new pin staged with git
    add counts, and uncommitted changes in the submodule are allowed: the build
    version marks them -dirty, and run.json the repo's state."""
    if not (DIR / "go.mod").is_file():
        raise SandboxenvError(f"the sandboxenv-rl submodule in {DIR} is not checked out; run git submodule update --init")
    pinned, checked_out = pinned_commit(), git.head(DIR)
    if pinned != checked_out:
        raise SandboxenvError(
            f"{PATH} is checked out at {checked_out[:12]}, but the repo pins {pinned[:12]}; run"
            f" git submodule update, or git add {PATH} to benchmark the commit it is at"
        )
    if output(["git", "status", "--porcelain"], cwd=DIR).strip():
        log(f"sandboxenv-rl: {PATH} has uncommitted changes; its build version is {build_version()}", "WARNING")


@functools.cache
def build_version() -> str:
    """What its images are tagged, read once per run: git describe in the
    submodule, so they are built again only once it moves."""
    return git.describe(DIR) or "dev"


def image_refs(repo: str) -> list[str]:
    """ate-env-api and ate-env-guest in repo, which builder.py pushes, each
    named after the last element of its package as ko --base-import-paths
    names it."""
    repo = images.group_repo(repo, GROUP)
    return [images.ref(repo, package.rsplit("/", 1)[-1], build_version()) for package in PACKAGES]


def pinned_image(repo: str, name: str) -> str:
    """One of image_refs, pinned."""
    return images.pin(images.ref(images.group_repo(repo, GROUP), name, build_version()))


@contextmanager
def ate_env() -> Iterator[AteEnv]:
    """Builds the CLI once and yields what makes a command of it."""
    with tempfile.TemporaryDirectory() as tmp:
        binary = f"{tmp}/ate-env"
        retry("building ate-env", lambda: stream(["go", "build", "-o", binary, "./cmd/ate-env"], cwd=DIR))
        yield lambda *args: [binary, *args]


def render_stack(cli: AteEnv, cluster: Cluster, sandbox_class: str, worker_count: int, api_image: str, api_replicas: int) -> str:
    """The namespace, WorkerPool and ate-env-api, with the pool fitted to the
    cluster and the case's sandbox class."""
    manifest = output(
        cli(
            "manifest",
            f"--namespace={NAMESPACE}",
            f"--template={TEMPLATE}",
            f"--api-image={api_image}",
            f"--api-replicas={api_replicas}",
            # substrate.resolve pins it to its image in the repository.
            f"--worker-image=ko://go.ate.dev/substrate/cmd/ateworker-{sandbox_class}",
            f"--replicas={worker_count}",
        )
    )
    docs = [doc for doc in yaml.safe_load_all(manifest) if doc]
    pool = next(doc for doc in docs if doc["kind"] == "WorkerPool")
    # sandboxenv-rl's pool is gvisor-default's alone.
    config_name = worker_pool.SANDBOX_CONFIGS[sandbox_class]
    pool["spec"]["sandboxClasses"] = [{"name": sandbox_class, "configRef": {"name": config_name}}]
    # sandboxenv-rl's workers go on any node; the sandboxed nodes are the
    # benchmarking workers, tainted for any class, as in worker-pool.yaml.tmpl.
    pool["spec"]["template"] = {
        "nodeSelector": {"benchmarking-worker": "true"},
        "tolerations": [{"key": "ate.dev/sandboxClass", "operator": "Exists", "effect": "NoSchedule"}],
    }
    return substrate.resolve(cluster, yaml.safe_dump_all(docs, sort_keys=False))


def render_template(cli: AteEnv, guest_image: str, actor_cpu: str, actor_memory: str) -> str:
    """default-template, whose actors run the guest, on the pool render_stack
    makes, which its default workerSelector names. Its snapshots go in the
    cluster's snapshot location: a location of the template's own needs a
    preview gate substrate is not installed with."""
    return output(
        cli(
            "manifest",
            "template",
            f"--template={TEMPLATE}",
            f"--atespace={ATESPACE}",
            f"--guest-image={guest_image}",
            f"--cpu={actor_cpu}",
            f"--memory={actor_memory}",
        )
    )


def auth_token(cluster: Cluster) -> str | None:
    """The token ate-env-api requires, read from its Secret; None without one."""
    get = cluster.kubectl("get", "secret", AUTH_SECRET, "-o=jsonpath={.data.token}", namespace=NAMESPACE)
    proc = subprocess.run(get, capture_output=True, text=True)
    if proc.returncode or not proc.stdout.strip():
        return None
    return base64.b64decode(proc.stdout.strip()).decode()


def ensure_auth_secret(cli: AteEnv, cluster: Cluster) -> None:
    """Makes the Secret with ate-env-api's token unless it exists, as each
    `ate-env manifest auth-secret` prints a new token and a dev infra's later
    runs and the tokens its callers hold should agree."""
    if auth_token(cluster):
        log(f"sandboxenv-rl: reusing the token in {NAMESPACE}/{AUTH_SECRET}")
        return
    secret = output(cli("manifest", "auth-secret", f"--namespace={NAMESPACE}"))
    stream(cluster.kubectl("create", "-f", "-"), input=secret)


def ensure_access_policy(ate: Callable[..., list[str]], policy: str) -> None:
    """Gives ate-env-api the editor role on its atespace, unless the atespace
    has a policy already: it has at most one, which an earlier run made, and
    that is the same. kubectl ate get exits 0 for an atespace without a policy
    too, printing nothing to stdout."""
    if output(ate("get", "access-policy", "-a", ATESPACE)).strip():
        log(f"sandboxenv-rl: {ATESPACE} has its access policy already")
        return
    stream(ate("create", "access-policy", "-a", ATESPACE, "-f", "-"), input=policy)


def deploy(
    cluster: Cluster,
    image_repo: str,
    sandbox_class: str,
    worker_count: int,
    actor_memory: str = DEFAULT_ACTOR_MEMORY,
    actor_cpu: str = DEFAULT_ACTOR_CPU,
    api_replicas: int = DEFAULT_API_REPLICAS,
    timeout: int = DEFAULT_TIMEOUT_SECS,
) -> None:
    """Deploys ate-env-api, the pool and default-template from their images in
    image_repo, image_refs, and waits for the template's golden snapshot.
    Always a new pool, so no run starts on workers the last one warmed."""
    check_pinned()
    api_image, guest_image = pinned_image(image_repo, "ate-env-api"), pinned_image(image_repo, "ate-env-guest")
    with ate_env() as cli:
        stack = render_stack(cli, cluster, sandbox_class, worker_count, api_image, api_replicas)
        template = render_template(cli, guest_image, actor_cpu, actor_memory)
        policy = output(cli("manifest", "access-policy", f"--namespace={NAMESPACE}"))
        # The Secret needs the namespace, which the stack applies too.
        cluster.apply_namespace(NAMESPACE)
        ensure_auth_secret(cli, cluster)

    with workloads.kubectl_ate(cluster) as ate:
        # Before ate-env-api: creating an atespace takes an owner, which it is not.
        workloads.ensure_atespace(ate, ATESPACE)
        ensure_access_policy(ate, policy)

        worker_pool.delete(cluster, timeout, POOL, NAMESPACE)
        log(f"sandboxenv-rl: deploying {API} and {POOL} (replicas={worker_count}, sandbox_class={sandbox_class}) in {NAMESPACE}")
        stream(cluster.kubectl("apply", "-f", "-"), input=stack)
        stream(cluster.kubectl("rollout", "status", f"deployment/{API}", f"--timeout={timeout}s", namespace=NAMESPACE))
        worker_pool.wait_ready(cluster, timeout, POOL, NAMESPACE)

        log(
            f"sandboxenv-rl: registering {ATESPACE}/{TEMPLATE} (actor_memory={actor_memory}, actor_cpu={actor_cpu})"
        )
        # Templates are immutable. Deleting one also deletes its golden actor and snapshot.
        workloads.delete_template(ate, TEMPLATE, ATESPACE)
        stream(ate("create", "actor-template", "-f", "-"), input=template)
        workloads.wait_golden_snapshot(ate, cluster, TEMPLATE, timeout, ATESPACE, (NAMESPACE, POOL))
    log(f"sandboxenv-rl: {ATESPACE}/{TEMPLATE} ready")
