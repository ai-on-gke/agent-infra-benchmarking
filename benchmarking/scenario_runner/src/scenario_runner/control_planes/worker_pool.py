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
workloads/manifests/worker-pool.yaml.tmpl."""

from __future__ import annotations

import shutil
import subprocess
from string import Template

from .. import RUNNER_DIR
from ..infra.cluster import Cluster
from ..log import log
from ..process import output, stream
from . import substrate

MANIFEST = RUNNER_DIR.parent / "workloads" / "manifests" / "worker-pool.yaml.tmpl"
# Have to match the WorkerPool in the manifest.
NAME = "benchmark-ateom"
NAMESPACE = "benchmark-workloads"
DEFAULT_TIMEOUT_SECS = 300


def missing_requirements() -> list[str]:
    # gcloud pins the pool's workerImage to the digest of substrate's image in the repository.
    return [tool for tool in ("kubectl", "gcloud") if not shutil.which(tool)]


def delete(cluster: Cluster, timeout: int = DEFAULT_TIMEOUT_SECS, name: str = NAME, namespace: str = NAMESPACE) -> None:
    # By name rather than through the manifest, which also carries the namespace.
    log(f"worker pool: deleting any previous {name}")
    stream(cluster.kubectl("delete", "workerpool", name, "--ignore-not-found", "--wait=true", namespace=namespace))
    # The Deployment is garbage-collected after the pool; a new one of the same
    # name would race it.
    wait = ["wait", "--for=delete", f"deployment/{name}", f"--timeout={timeout}s"]
    try:
        stream(cluster.kubectl(*wait, namespace=namespace))
    except subprocess.CalledProcessError:
        pass


def deploy(cluster: Cluster, worker_count: int, sandbox_class: str, timeout: int = DEFAULT_TIMEOUT_SECS) -> None:
    """Always a new pool, so no run starts on workers the last one warmed."""
    delete(cluster, timeout)

    log(f"worker pool: deploying {NAME} (replicas={worker_count}, sandbox_class={sandbox_class})")
    manifest = Template(MANIFEST.read_text()).substitute(WORKER_COUNT=worker_count, SANDBOX_CLASS=sandbox_class)
    resolved = substrate.resolve(cluster, manifest)
    log(output(cluster.kubectl("apply", "-f", "-", namespace=NAMESPACE), input=resolved).strip())

    wait_ready(cluster, timeout)


def wait_ready(cluster: Cluster, timeout: int = DEFAULT_TIMEOUT_SECS, name: str = NAME, namespace: str = NAMESPACE) -> None:
    """Waits for the Deployment the controller makes of the pool to roll out."""
    log(f"worker pool: waiting for the {name} workers to be ready (timeout: {timeout}s)")
    stream(cluster.kubectl("wait", "--for=create", f"deployment/{name}", f"--timeout={timeout}s", namespace=namespace))
    try:
        stream(cluster.kubectl("rollout", "status", f"deployment/{name}", f"--timeout={timeout}s", namespace=namespace))
    except subprocess.CalledProcessError:
        log(f"worker pool: {name} was not ready within {timeout}s. Pending pods and scheduling events:", "ERROR")
        for args in (
            ["get", "pods", "--field-selector=status.phase=Pending"],
            ["get", "events", "--field-selector=reason=FailedScheduling", "--sort-by=.lastTimestamp"],
        ):
            try:
                stream(cluster.kubectl(*args, namespace=namespace))
            except subprocess.CalledProcessError:
                pass
        raise
