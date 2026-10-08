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

"""Recreates or deletes the benchmark WorkerPool the workloads run on, from
workloads/manifests/worker-pool.yaml.tmpl, on a dev infra's cluster:

  tools/python -m orchestrator.worker_pool deploy NAME --worker-count 30
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from string import Template

from . import AUTOMATION_DIR, dev_infra
from .cluster import Cluster
from .log import log
from .process import failure, stream
from .substrate import SUBSTRATE_DIR

MANIFEST = AUTOMATION_DIR.parent / "workloads" / "manifests" / "worker-pool.yaml.tmpl"
# Have to match the WorkerPool in the manifest.
NAME = "benchmark-ateom"
NAMESPACE = "benchmark-workloads"
SANDBOX_CLASSES = ("gvisor", "microvm")
DEFAULT_TIMEOUT_SECS = 300


def missing_requirements() -> list[str]:
    missing = [tool for tool in ("kubectl", "go", "git") if not shutil.which(tool)]
    # ko resolves the pool's ko:// workerImage, and only works inside the submodule.
    if not (SUBSTRATE_DIR / "go.mod").is_file():
        missing.append(f"the substrate submodule in {SUBSTRATE_DIR}")
    return missing


def kubectl_flags(cluster: Cluster) -> list[str]:
    return [f"--kubeconfig={cluster.kubeconfig}", f"--namespace={NAMESPACE}"]


def kubectl(cluster: Cluster, *args: str) -> None:
    stream(["kubectl", *args, *kubectl_flags(cluster)])


def delete(cluster: Cluster, timeout: int = DEFAULT_TIMEOUT_SECS) -> None:
    # By name rather than through the manifest, which also carries the namespace.
    log(f"worker pool: deleting any previous {NAME}")
    kubectl(cluster, "delete", "workerpool", NAME, "--ignore-not-found", "--wait=true")
    # The Deployment is garbage-collected after the pool; a new one of the same
    # name would race it.
    try:
        kubectl(cluster, "wait", "--for=delete", f"deployment/{NAME}", f"--timeout={timeout}s")
    except subprocess.CalledProcessError:
        pass


def deploy(cluster: Cluster, worker_count: int, sandbox_class: str, timeout: int = DEFAULT_TIMEOUT_SECS) -> None:
    """Always a new pool, so no run starts on workers the last one warmed."""
    delete(cluster, timeout)

    log(f"worker pool: deploying {NAME} (replicas={worker_count}, sandbox_class={sandbox_class})")
    manifest = Template(MANIFEST.read_text()).substitute(WORKER_COUNT=worker_count, SANDBOX_CLASS=sandbox_class)
    # ko has no flag for the registry. The platform is the nodes', whatever the
    # build host's.
    stream(
        [str(SUBSTRATE_DIR / "hack" / "run-tool.sh"), "ko", "apply", "-f", "-", "--", *kubectl_flags(cluster)],
        env={"KO_DOCKER_REPO": cluster.docker_repo, "KO_DEFAULTPLATFORMS": "linux/amd64"},
        cwd=SUBSTRATE_DIR,
        input=manifest,
    )

    log(f"worker pool: waiting for the workers to be ready (timeout: {timeout}s)")
    kubectl(cluster, "wait", "--for=create", f"deployment/{NAME}", f"--timeout={timeout}s")
    try:
        kubectl(cluster, "rollout", "status", f"deployment/{NAME}", f"--timeout={timeout}s")
    except subprocess.CalledProcessError:
        log(f"worker pool: {NAME} was not ready within {timeout}s. Pending pods and scheduling events:", "ERROR")
        for args in (
            ["get", "pods", "--field-selector=status.phase=Pending"],
            ["get", "events", "--field-selector=reason=FailedScheduling", "--sort-by=.lastTimestamp"],
        ):
            try:
                kubectl(cluster, *args)
            except subprocess.CalledProcessError:
                pass
        raise


def main() -> None:
    p = argparse.ArgumentParser(
        prog="python3 -m orchestrator.worker_pool",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = p.add_subparsers(dest="command", required=True)
    deploy_command = commands.add_parser("deploy", help="recreate the WorkerPool and wait for its workers to be ready")
    deploy_command.add_argument("--worker-count", type=int, default=1, help="replicas; default: %(default)s")
    deploy_command.add_argument(
        "--sandbox-class",
        choices=SANDBOX_CLASSES,
        default="gvisor",
        help="microvm requires dependency-repos/substrate/hack/install-microvm-deps.sh --install to have run."
        " Default: %(default)s",
    )
    delete_command = commands.add_parser("delete", help="delete the WorkerPool")
    for command in (deploy_command, delete_command):
        command.add_argument("name", type=dev_infra.check_name, metavar="NAME", help="the dev infra")
        command.add_argument(
            "--wait-timeout", type=int, default=DEFAULT_TIMEOUT_SECS, metavar="SECONDS", help="default: %(default)s"
        )
    args = p.parse_args()
    if args.command == "deploy" and args.worker_count < 0:
        p.error("--worker-count must not be negative")

    missing = missing_requirements()
    if missing:
        sys.exit(f"worker pool: requires {', '.join(missing)}")
    if not dev_infra.state_file(args.name, "cluster").exists():
        sys.exit(f"worker pool: dev infra {args.name}'s cluster is not applied")
    cluster = dev_infra.cluster(args.name)
    try:
        cluster.get_credentials()
        if args.command == "deploy":
            deploy(cluster, args.worker_count, args.sandbox_class, args.wait_timeout)
        else:
            delete(cluster, args.wait_timeout)
    except subprocess.CalledProcessError as exc:
        sys.exit(f"worker pool: {failure(exc)}")


if __name__ == "__main__":
    main()
