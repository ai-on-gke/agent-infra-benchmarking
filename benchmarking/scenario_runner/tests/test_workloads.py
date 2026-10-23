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

import unittest
from unittest import mock

import yaml

from scenario_runner import workloads
from scenario_runner.control_planes import worker_pool


def template(*urls: str, memory: str = "256Mi", enum: str = "SANDBOX_CLASS_GVISOR") -> dict:
    return {
        "containers": [{"env": [{"name": f"E{i}", "value": url} for i, url in enumerate(urls)]}],
        "resources": {"limits": [{"name": "memory", "quantity": memory}]},
        "sandboxConfig": {"sandboxClass": enum},
    }


class EgressHosts(unittest.TestCase):
    def test_cleartext_and_tls_hosts_are_told_apart(self):
        hosts = workloads.egress_hosts(
            template("http://otel.ns.svc:4317", "https://api.example.com/v1", "http://mock-llm.ns.svc/v1", "not a url")
        )
        self.assertEqual(hosts, (["mock-llm.ns.svc", "otel.ns.svc"], ["api.example.com"]))

    def test_no_urls_no_hosts(self):
        self.assertEqual(workloads.egress_hosts({}), ([], []))


class Memory(unittest.TestCase):
    def test_quantities(self):
        self.assertEqual(workloads.memory_mib("2Gi"), 2048)
        self.assertEqual(workloads.memory_mib("256Mi"), 256)
        self.assertIsNone(workloads.memory_mib("2 GB"))

    def test_minimums_by_class(self):
        self.assertEqual(workloads.min_actor_memory_mib("openclaw", "microvm"), 2048)
        self.assertEqual(workloads.min_actor_memory_mib("openclaw", "gvisor"), 1024)
        self.assertIsNone(workloads.min_actor_memory_mib("glutton", "microvm"))

    def test_a_deployed_template_has_to_match_the_case(self):
        workloads.check_deployed(template(), "glutton", "gvisor", "glutton", "256Mi")
        with self.assertRaisesRegex(workloads.WorkloadError, "not SANDBOX_CLASS_MICROVM"):
            workloads.check_deployed(template(), "glutton", "microvm", "glutton", "256Mi")
        with self.assertRaisesRegex(workloads.WorkloadError, "actor-memory is 512Mi"):
            workloads.check_deployed(template(), "glutton", "gvisor", "glutton", "512Mi")


class WorkerPool(unittest.TestCase):
    def deployed(self, *args) -> dict:
        """The WorkerPool deploy applies, its images left unresolved."""
        with (
            mock.patch.object(worker_pool, "delete"),
            mock.patch.object(worker_pool, "wait_ready"),
            mock.patch.object(worker_pool.substrate, "resolve", side_effect=lambda cluster, manifest: manifest),
            mock.patch.object(worker_pool, "output", return_value="") as applied,
        ):
            worker_pool.deploy(mock.Mock(), *args)
        return list(yaml.safe_load_all(applied.call_args.kwargs["input"]))[1]

    def test_each_worker_gets_the_limits(self):
        pool = self.deployed(2, "gvisor", {"memory": "1536Mi", "cpu": "4"})
        self.assertEqual(pool["spec"]["replicas"], 2)
        self.assertEqual(pool["spec"]["template"]["resources"], {"limits": {"memory": "1536Mi", "cpu": "4"}})

    def test_no_limits_no_resources(self):
        self.assertEqual(self.deployed(10, "gvisor")["spec"]["template"]["resources"], {})
