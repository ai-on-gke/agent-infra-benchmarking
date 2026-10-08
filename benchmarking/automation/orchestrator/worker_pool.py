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

from . import AUTOMATION_DIR, substrate
from .cluster import Cluster
from .log import log
from .process import stream

MANIFEST = AUTOMATION_DIR.parent / "workloads" / "manifests" / "worker-pool.yaml.tmpl"
# Have to match the WorkerPool in the manifest.
NAME = "benchmark-ateom"
NAMESPACE = "benchmark-workloads"
DEFAULT_TIMEOUT_SECS = 300


def missing_requirements() -> list[str]:
    missing = [tool for tool in ("kubectl", "go", "git") if not shutil.which(tool)]
    # ko resolves the pool's ko:// workerImage, and only works inside the submodule.
    if not (substrate.SUBSTRATE_DIR / "go.mod").is_file():
        missing.append(f"the substrate submodule in {substrate.SUBSTRATE_DIR}")
    return missing


def delete(cluster: Cluster, timeout: int = DEFAULT_TIMEOUT_SECS) -> None:
    # By name rather than through the manifest, which also carries the namespace.
    log(f"worker pool: deleting any previous {NAME}")
    stream(cluster.kubectl("delete", "workerpool", NAME, "--ignore-not-found", "--wait=true", namespace=NAMESPACE))
    # The Deployment is garbage-collected after the pool; a new one of the same
    # name would race it.
    wait = ["wait", "--for=delete", f"deployment/{NAME}", f"--timeout={timeout}s"]
    try:
        stream(cluster.kubectl(*wait, namespace=NAMESPACE))
    except subprocess.CalledProcessError:
        pass


def deploy(cluster: Cluster, worker_count: int, sandbox_class: str, timeout: int = DEFAULT_TIMEOUT_SECS) -> None:
    """Always a new pool, so no run starts on workers the last one warmed."""
    delete(cluster, timeout)

    log(f"worker pool: deploying {NAME} (replicas={worker_count}, sandbox_class={sandbox_class})")
    manifest = Template(MANIFEST.read_text()).substitute(WORKER_COUNT=worker_count, SANDBOX_CLASS=sandbox_class)
    log(substrate.ko(cluster, "apply", "-f", "-", "--", *cluster.kubectl_flags(NAMESPACE), input=manifest).strip())

    log(f"worker pool: waiting for the workers to be ready (timeout: {timeout}s)")
    stream(cluster.kubectl("wait", "--for=create", f"deployment/{NAME}", f"--timeout={timeout}s", namespace=NAMESPACE))
    try:
        stream(cluster.kubectl("rollout", "status", f"deployment/{NAME}", f"--timeout={timeout}s", namespace=NAMESPACE))
    except subprocess.CalledProcessError:
        log(f"worker pool: {NAME} was not ready within {timeout}s. Pending pods and scheduling events:", "ERROR")
        for args in (
            ["get", "pods", "--field-selector=status.phase=Pending"],
            ["get", "events", "--field-selector=reason=FailedScheduling", "--sort-by=.lastTimestamp"],
        ):
            try:
                stream(cluster.kubectl(*args, namespace=NAMESPACE))
            except subprocess.CalledProcessError:
                pass
        raise
