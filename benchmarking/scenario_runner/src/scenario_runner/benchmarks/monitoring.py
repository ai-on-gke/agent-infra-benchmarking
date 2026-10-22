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

"""Deploys Prometheus from monitoring/prometheus.yaml onto the cluster's
monitoring node pool, and waits for it."""

from __future__ import annotations

from .. import RUNNER_DIR
from ..infra.cluster import Cluster
from ..log import log
from ..process import output, stream

MANIFEST = RUNNER_DIR.parent / "monitoring" / "prometheus.yaml"
# Have to match the Deployment in the manifest, and its nodeSelector the
# monitoring pool's label in infra/clusters/common/main.tf.
NAME = "prometheus"
NAMESPACE = "benchmarking"
NODE_LABEL = "benchmarking-monitoring=true"
TIMEOUT_SECS = 300


class MonitoringError(RuntimeError):
    pass


def deploy(cluster: Cluster) -> None:
    """Applies the manifest, which leaves a Prometheus already running as it
    is, so a dev infra's keeps what it recorded."""
    # Without the pool its pod would only sit Pending until the rollout times out.
    if not output(cluster.kubectl("get", "nodes", f"--selector={NODE_LABEL}", "-o=name")).strip():
        raise MonitoringError(
            f"cluster {cluster.name} has no node labeled {NODE_LABEL} for Prometheus; apply its config again,"
            " with --dev-infra-update for a dev infra"
        )
    log(f"monitoring: deploying {NAME} on cluster {cluster.name}")
    # Without --namespace: the manifest's RBAC reaches into ate-system.
    stream(cluster.kubectl("apply", "-f", str(MANIFEST)))
    rollout = ["rollout", "status", f"deployment/{NAME}", f"--timeout={TIMEOUT_SECS}s"]
    stream(cluster.kubectl(*rollout, namespace=NAMESPACE))
    log(f"monitoring: {NAME} is up: kubectl port-forward -n {NAMESPACE} svc/{NAME} 9090:9090 --kubeconfig={cluster.kubeconfig}")
