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

"""scenarios.yaml as the scenario-runner reads it: the shipped cases and the
checks a case has to pass."""

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from scenario_runner import locust
from scenario_runner.cases import SCENARIOS_FILE, load_cases, record_names, warmup_names

GLUTTON = {
    "type": "locust",
    "infra": {"cluster": {"name": "common", "tfvars": "default.tfvars"}},
    "master": {"run-time": "1m", "users": 10},
    "worker": {"workload-name": "glutton", "workload-template": "glutton"},
}


def load(cases: dict) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump({"cases": cases}, f)
    try:
        return load_cases(Path(f.name))
    finally:
        Path(f.name).unlink()


class ShippedCases(unittest.TestCase):
    def test_every_shipped_case_loads(self):
        self.assertIn("glutton", load_cases(SCENARIOS_FILE))

    def test_each_microvm_case_pairs_with_its_gvisor_case(self):
        # gVisor and microvm compare only if the cases differ in the class alone.
        cases = load_cases(SCENARIOS_FILE)
        for name, case in cases.items():
            if not name.endswith("-microvm"):
                continue
            pair = copy.deepcopy(cases[name.removesuffix("-microvm")])
            self.assertEqual(pair.pop("sandbox-class"), "gvisor")
            mine = copy.deepcopy(case)
            self.assertEqual(mine.pop("sandbox-class"), "microvm")
            self.assertEqual(mine, pair, f"{name} differs from its gVisor case in more than the class")

    def test_class_is_not_set_twice(self):
        # The runner sets the cluster's sandbox_class from the case's sandbox-class.
        for name, case in load_cases(SCENARIOS_FILE).items():
            self.assertNotIn("sandbox_class", case["infra"]["cluster"].get("tfvars_inline", ""), name)


class CaseChecks(unittest.TestCase):
    def test_defaults(self):
        case = locust.check_case("glutton", GLUTTON, deploy_workloads=True)
        self.assertEqual(case["sandbox-class"], "gvisor")
        self.assertEqual(case["actor-memory"], "256Mi")
        self.assertEqual(case["locust-workers"], 1)
        self.assertEqual(case["max-failure-ratio"], locust.DEFAULT_MAX_FAILURE_RATIO)
        self.assertEqual(case["master"]["spawn-rate"], 10)

    def test_openclaw_gets_its_memory(self):
        case = {**GLUTTON, "worker": {"workload-name": "openclaw", "workload-template": "openclaw"}}
        self.assertEqual(locust.check_case("openclaw", case, True)["actor-memory"], "2Gi")

    def test_openclaw_on_microvm_needs_2gi(self):
        worker = {"workload-name": "openclaw", "workload-template": "openclaw"}
        case = {**GLUTTON, "worker": worker, "sandbox-class": "microvm", "actor-memory": "1Gi"}
        with self.assertRaisesRegex(ValueError, "less than openclaw needs on microvm"):
            locust.check_case("openclaw-microvm", case, True)
        locust.check_case("openclaw", {**case, "sandbox-class": "gvisor"}, True)

    def test_bad_values_are_refused(self):
        for key, value in [
            ("actor-memory", "lots"),
            ("locust-workers", 0),
            ("max-failure-ratio", 2),
            ("worker-count", -1),
            ("sandbox-class", "kata"),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                locust.check_case("c", {**GLUTTON, key: value}, True)

    def test_several_worker_pods_are_expected_by_the_master(self):
        case = locust.check_case("c", {**GLUTTON, "locust-workers": 3}, True)
        args = locust.master_args(case)
        self.assertEqual(args[args.index("--expect-workers") + 1], "3")

    def test_flags(self):
        self.assertEqual(locust.flags({"a": True, "b": 2, "c": "x", "d": False}), ["--a", "--b=2", "--c=x", "--d=false"])

    def test_repeats_are_recorded_by_number(self):
        self.assertEqual(record_names("c", {"repeats": {"count": 3}}), ["c-1", "c-2", "c-3"])
        self.assertEqual(record_names("c", {}), ["c"])

    def test_one_warmup_on_a_shared_cluster_by_default(self):
        self.assertEqual(warmup_names("c", {}), ["c-warmup-1"])
        self.assertEqual(warmup_names("c", {"repeats": {"warmups": 0}}), [])
        self.assertEqual(warmup_names("c", {"repeats": {"cluster": "per-repeat"}}), [])

    def test_load_fills_in_how_a_case_is_sampled(self):
        self.assertEqual(load({"c": GLUTTON})["c"]["repeats"], {"count": 1, "warmups": 1, "cluster": "shared"})
        case = load({"c": {**GLUTTON, "repeats": {"count": 3, "cluster": "per-repeat"}}})["c"]
        self.assertEqual(case["repeats"], {"count": 3, "warmups": 0, "cluster": "per-repeat"})

    def test_load_refuses_bad_cases(self):
        for case in (
            {**GLUTTON, "repeats": 3},
            {**GLUTTON, "repeats": {"count": 0}},
            {**GLUTTON, "repeats": {"times": 3}},
            {**GLUTTON, "repeats": {"cluster": "fresh"}},
            {**GLUTTON, "repeats": {"warmups": -1}},
            {**GLUTTON, "repeats": {"warmups": 1, "cluster": "per-repeat"}},
            {**GLUTTON, "infra": {"cluster": {"name": "common", "unknown": 1}}},
            {**GLUTTON, "infra": {"cluster": {"name": "nowhere"}}},
            {**GLUTTON, "type": "jmeter"},
        ):
            with self.subTest(case=case), self.assertRaises(ValueError):
                load({"c": case})
