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

"""Deploys or deletes mock-llm, from workloads/manifests/mock-llm.yaml.tmpl:
the LLM the openclaw actors call instead of a real one."""

from __future__ import annotations

import subprocess
from string import Template

from .. import RUNNER_DIR, images, worker_pool
from ..cluster import Cluster
from ..log import log
from ..process import stream

MOCK_LLM_MANIFEST = RUNNER_DIR.parent / "workloads" / "manifests" / "mock-llm.yaml.tmpl"
MOCK_LLM_IMAGE = "mock-llm"
# Has to match the manifest's, which the openclaw template's MOCK_LLM_BASE_URL
# names.
NAMESPACE = worker_pool.NAMESPACE


def deploy_mock_llm(cluster: Cluster, timeout: int) -> None:
    """Expects images.build_and_push to have built mock-llm from this commit."""
    log("workloads: deploying mock-llm")
    image = images.pin(images.image_ref(images.WORKLOADS_IMAGES, cluster.docker_repo, MOCK_LLM_IMAGE))
    manifest = Template(MOCK_LLM_MANIFEST.read_text()).substitute(IMAGE_WITH_DIGEST=image)
    stream(cluster.kubectl("apply", "-f", "-", namespace=NAMESPACE), input=manifest)
    log(f"workloads: waiting for mock-llm to be ready (timeout: {timeout}s)")
    stream(cluster.kubectl("rollout", "status", "deployment/mock-llm", f"--timeout={timeout}s", namespace=NAMESPACE))


def delete_mock_llm(cluster: Cluster) -> None:
    try:
        stream(cluster.kubectl("delete", "deployment,service", "mock-llm", "--ignore-not-found", namespace=NAMESPACE))
    except subprocess.CalledProcessError:
        log("workloads: mock-llm not deleted")
