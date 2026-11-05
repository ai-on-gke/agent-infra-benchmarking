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

"""agent-sandbox's check that its sandboxes' pods run in gVisor."""

import unittest

from scenario_runner.control_planes.agent_sandbox import isolation_problems, warm_pools

SANDBOX = [{"kind": "Sandbox", "name": "s"}]


def pod(name: str, runtime_class: str | None = "gvisor", node: str = "sandboxed", owners=SANDBOX, **meta) -> dict:
    spec = {"nodeName": node, **({"runtimeClassName": runtime_class} if runtime_class else {})}
    return {"metadata": {"name": name, "ownerReferences": owners, **meta}, "spec": spec}


def node(name: str, runtime: str | None = None) -> dict:
    return {"metadata": {"name": name, "labels": {"sandbox.gke.io/runtime": runtime} if runtime else {}}}


NODES = [node("sandboxed", "gvisor"), node("default")]


class Isolation(unittest.TestCase):
    def test_gvisor_pods_on_gke_sandbox_nodes_pass(self):
        self.assertEqual(isolation_problems([pod("a"), pod("b")], NODES, "gvisor"), [])

    def test_a_pod_without_the_runtime_class_fails(self):
        problems = isolation_problems([pod("a"), pod("b", runtime_class=None)], NODES, "gvisor")
        self.assertEqual(problems, ["pod b has runtimeClassName None, not 'gvisor'"])

    def test_a_pod_off_gke_sandbox_nodes_fails(self):
        problems = isolation_problems([pod("a", node="default")], NODES, "gvisor")
        self.assertEqual(problems, ["pod a is on node default, which is not sandbox.gke.io/runtime=gvisor"])

    def test_only_sandboxes_pods_count(self):
        # mock-llm shares the namespace, and a recreated pool's pods may still be going.
        others = [pod("mock-llm", runtime_class=None, owners=[{"kind": "ReplicaSet"}]),
                  pod("old", runtime_class=None, deletionTimestamp="2026-10-26T00:00:00Z")]  # fmt: skip
        self.assertEqual(isolation_problems([pod("a"), *others], NODES, "gvisor"), [])

    def test_no_sandbox_pods_fails(self):
        self.assertEqual(isolation_problems([], NODES, "gvisor"), ["no sandbox pods to check"])


if __name__ == "__main__":
    unittest.main()


class WarmPools(unittest.TestCase):
    def test_the_sandboxes_are_split_evenly(self):
        self.assertEqual(
            warm_pools("glutton", 300),
            {f"glutton-warm-pool-{i}": 75 for i in range(4)},
        )

    def test_a_remainder_goes_to_the_first_pools(self):
        self.assertEqual(list(warm_pools("glutton", 10).values()), [3, 3, 2, 2])

    def test_fewer_sandboxes_than_shards_get_a_pool_each(self):
        self.assertEqual(warm_pools("openclaw", 2), {"openclaw-warm-pool-0": 1, "openclaw-warm-pool-1": 1})
