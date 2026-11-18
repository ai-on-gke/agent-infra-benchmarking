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

"""Deploys monitoring/node-probe.py as a DaemonSet on the sandboxed nodes,
whose kernel-level samples go to the cluster's logs, and waits for it."""

from __future__ import annotations

import json

from .. import RUNNER_DIR, images
from ..infra.cluster import Cluster
from ..log import log
from ..process import stream
from . import jobs

SCRIPT = RUNNER_DIR.parent / "monitoring" / "node-probe.py"
NAME = "node-probe"
NAMESPACE = jobs.NAMESPACE
# The sandboxed pool's label in infra/clusters/common/main.tf.
NODE_SELECTOR = {"benchmarking-worker": "true"}
# Any image of this tree's with python3 and a shell does; the script is mounted.
IMAGE = "locust-master"
TIMEOUT_SECS = 300


def manifest(image: str) -> str:
    labels = {"app": NAME}
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": NAME, "namespace": NAMESPACE},
        "data": {"node-probe.py": SCRIPT.read_text()},
    }
    daemon_set = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {"name": NAME, "namespace": NAMESPACE},
        "spec": {
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "nodeSelector": NODE_SELECTOR,
                    "tolerations": [{"operator": "Exists"}],
                    "hostPID": True,
                    "containers": [
                        {
                            "name": NAME,
                            "image": image,
                            "command": ["python3", "-u", "/probe/node-probe.py"],
                            "securityContext": {"privileged": True},
                            "resources": {"requests": {"cpu": "200m", "memory": "128Mi"}},
                            "volumeMounts": [{"name": "script", "mountPath": "/probe"}],
                        }
                    ],
                    "volumes": [{"name": "script", "configMap": {"name": NAME}}],
                },
            },
        },
    }
    return "\n---\n".join(json.dumps(obj) for obj in (config_map, daemon_set))


def deploy(cluster: Cluster, image_repo: str) -> None:
    image = images.pin(images.image_ref(images.LOCUST_IMAGES, image_repo, IMAGE))
    log(f"node probe: deploying {NAME} on cluster {cluster.name}'s sandboxed nodes")
    cluster.apply_namespace(NAMESPACE)
    stream(cluster.kubectl("apply", "-f", "-"), input=manifest(image))
    rollout = ["rollout", "status", f"daemonset/{NAME}", f"--timeout={TIMEOUT_SECS}s"]
    stream(cluster.kubectl(*rollout, namespace=NAMESPACE))
    log(f"node probe: {NAME} is up; its samples are the logs of container {NAME} in namespace {NAMESPACE}")
