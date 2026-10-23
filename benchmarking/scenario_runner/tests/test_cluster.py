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

"""Where a case's cluster goes, and how the runner reaches it."""

import json
import unittest
from pathlib import Path
from unittest import mock

from scenario_runner.infra import cluster, cluster_configs

NETWORK = "projects/p/global/networks/runs"
SUBNETWORK = "projects/p/regions/us-central1/subnetworks/runs"


def applied_variables(**env: str | None) -> dict:
    unset = {"NODE_SERVICE_ACCOUNT": None, "CLUSTER_NETWORK": None, "CLUSTER_SUBNETWORK": None}
    with mock.patch.multiple(cluster_configs, **{**unset, **env}):
        return cluster_configs.CommonCluster().variables(cluster_configs.execution({}))


class Network(unittest.TestCase):
    def test_a_cloud_run_job_puts_its_clusters_in_its_network_with_an_internal_endpoint(self):
        variables = applied_variables(CLUSTER_NETWORK=NETWORK, CLUSTER_SUBNETWORK=SUBNETWORK)
        self.assertEqual(variables["existing_network"], {"network": NETWORK, "subnetwork": SUBNETWORK})
        self.assertIs(variables["enable_private_endpoint"], True)

    def test_a_cloud_run_job_s_nodes_run_as_its_account(self):
        variables = applied_variables(NODE_SERVICE_ACCOUNT="nodes@p.iam.gserviceaccount.com")
        self.assertEqual(variables["node_service_account"], "nodes@p.iam.gserviceaccount.com")
        self.assertNotIn("node_service_account", applied_variables())

    def test_elsewhere_a_cluster_gets_a_network_of_its_own(self):
        variables = applied_variables(CLUSTER_NETWORK=None, CLUSTER_SUBNETWORK=None)
        self.assertNotIn("existing_network", variables)
        self.assertNotIn("enable_private_endpoint", variables)


class Credentials(unittest.TestCase):
    def command(self, outputs: dict) -> list[str]:
        outputs = {"cluster_name": "c", "location": "us-central1-a", "project_id": "p", **outputs}
        with mock.patch.object(cluster, "stream") as stream:
            cluster.Cluster(outputs, Path("kubeconfig")).get_credentials()
        return stream.call_args.args[0]

    def test_an_internal_endpoint_is_reached_by_its_internal_ip(self):
        self.assertIn("--internal-ip", self.command({"private_endpoint": True}))
        self.assertNotIn("--internal-ip", self.command({"private_endpoint": False}))
        # A state applied before the output existed.
        self.assertNotIn("--internal-ip", self.command({}))

    def test_the_context_is_the_one_gcloud_names(self):
        c = cluster.Cluster({"cluster_name": "c", "location": "us-central1-a", "project_id": "p"}, Path("k"))
        self.assertEqual(c.context, "gke_p_us-central1-a_c")


class Volumes(unittest.TestCase):
    def delete(self, claims: list[dict], volumes: list[dict], fail: bool = False) -> tuple[list[list[str]], mock.Mock]:
        c = cluster.Cluster({"cluster_name": "c", "location": "z", "project_id": "p"}, Path("k"))
        streamed = []

        def got(cmd, *args, **kwargs):
            if fail:
                raise cluster.subprocess.CalledProcessError(1, cmd)
            return json.dumps({"items": claims if "pvc" in cmd else volumes})

        with mock.patch.object(cluster, "output", side_effect=got), \
             mock.patch.object(cluster, "stream", side_effect=lambda cmd, *a, **k: streamed.append(cmd)), \
             mock.patch.object(cluster, "log") as log:  # fmt: skip
            c.delete_volumes()
        return streamed, log

    def test_claims_go_with_what_mounts_them_and_their_volumes_are_waited_for(self):
        claims = [{"metadata": {"namespace": "ate-system"}}, {"metadata": {"namespace": "benchmarking"}}]
        streamed, _ = self.delete(claims, [{"metadata": {"name": "pv"}}])
        deletes = [cmd for cmd in streamed if "delete" in cmd]
        namespaces = [cmd[-1] for cmd in deletes]
        self.assertEqual(namespaces, ["--namespace=ate-system"] * 2 + ["--namespace=benchmarking"] * 2)
        self.assertIn("statefulsets,deployments,jobs,pods", deletes[0])
        self.assertIn("pvc", deletes[1])
        self.assertIn("--for=delete", streamed[-1])

    def test_no_claims_no_wait(self):
        streamed, _ = self.delete([], [])
        self.assertFalse([cmd for cmd in streamed if "kubectl" in cmd])

    def test_a_failure_only_warns(self):
        _, log = self.delete([], [], fail=True)
        self.assertEqual(log.call_args.args[1], "WARNING")
