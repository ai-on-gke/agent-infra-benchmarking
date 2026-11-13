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
from scenario_runner.control_planes import agent_sandbox, worker_pool
from scenario_runner.workloads import openclaw


def template(*urls: str, memory_bytes: str = "268435456") -> dict:
    return {
        "containers": [{"env": [{"name": f"E{i}", "value": url} for i, url in enumerate(urls)]}],
        "resources": {"limits": {"cpuMillicores": "1000", "memoryBytes": memory_bytes}},
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

    def test_as_actor_templates_take_them(self):
        self.assertEqual(workloads.memory_bytes("256Mi"), 268435456)
        self.assertEqual(workloads.memory_bytes("2Gi"), 2147483648)
        self.assertEqual(workloads.cpu_millicores("1"), 1000)
        self.assertEqual(workloads.cpu_millicores("1500m"), 1500)
        self.assertEqual(workloads.cpu_millicores("0.25"), 250)

    def test_a_deployed_template_has_to_match_the_case(self):
        workloads.check_deployed(template(), "glutton", "256Mi")
        with self.assertRaisesRegex(workloads.WorkloadError, "actor-memory is 512Mi"):
            workloads.check_deployed(template(), "glutton", "512Mi")


class OpenclawTimeout(unittest.TestCase):
    def test_the_actor_ends_a_turn_before_the_worker_does(self):
        for workload_timeout, secs in [("120s", 100), ("240s", 220), ("4m", 220), ("1m30s", 70), ("1.5m", 70)]:
            with self.subTest(workload_timeout=workload_timeout):
                self.assertEqual(openclaw.exec_timeout_secs(workload_timeout), secs)

    def test_no_workload_timeout_keeps_the_actor_s_default(self):
        for workload_timeout in (None, "", "0"):
            self.assertEqual(openclaw.exec_timeout_secs(workload_timeout), openclaw.DEFAULT_EXEC_TIMEOUT_SECS)

    def test_bad_or_too_short_timeouts_are_refused(self):
        for workload_timeout in ("120", "2 m", "abc", "20s", "10s"):
            with self.subTest(workload_timeout=workload_timeout), self.assertRaises(ValueError):
                openclaw.exec_timeout_secs(workload_timeout)

    def test_both_templates_take_it(self):
        cluster = mock.Mock(project_id="p")
        with mock.patch.object(workloads, "pinned_image", return_value="i"):
            rendered = yaml.safe_load(workloads.render_template(cluster, "openclaw", "2Gi", "o", "r", "240s"))
        env = {e["name"]: e["value"] for e in rendered["containers"][0]["env"]}
        self.assertEqual(env["OPENCLAW_TIMEOUT_SECS"], "220")
        manifest = agent_sandbox.render(
            "openclaw-template.yaml.tmpl", NAME="openclaw", NAMESPACE="n", IMAGE="i", RUNTIME_CLASS="gvisor",
            EXEC_TIMEOUT_SECS=openclaw.exec_timeout_secs("240s"),
        )  # fmt: skip
        self.assertIn('value: "220"', manifest)

    def test_claude_code_takes_it_and_calls_mock_llm(self):
        cluster = mock.Mock(project_id="p")
        with mock.patch.object(workloads, "pinned_image", return_value="i"):
            rendered = yaml.safe_load(workloads.render_template(cluster, "claude-code", "1Gi", "o", "r", "240s"))
        env = {e["name"]: e["value"] for e in rendered["containers"][0]["env"]}
        self.assertEqual(env["CLAUDE_CODE_TIMEOUT_SECS"], "220")
        self.assertEqual(workloads.egress_hosts(rendered), (["mock-llm.benchmark-workloads.svc.cluster.local"], []))
        self.assertIn("claude-code", workloads.MOCK_LLM_TEMPLATES)


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

    def test_the_pool_names_its_class_s_sandbox_config(self):
        for sandbox_class, config in worker_pool.SANDBOX_CONFIGS.items():
            pool = self.deployed(1, sandbox_class)
            self.assertEqual(pool["spec"]["sandboxClasses"], [{"name": sandbox_class, "configRef": {"name": config}}])
