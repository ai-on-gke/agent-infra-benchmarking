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
from scenario_runner.cases import (
    SCENARIOS_FILE,
    load_bare_metal,
    load_cases,
    record_names,
    warmup_names,
    with_bare_metal,
)

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


def load_metal(settings) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump({"bare-metal": settings, "cases": {"c": GLUTTON}}, f)
    try:
        return load_bare_metal(Path(f.name))
    finally:
        Path(f.name).unlink()


PLACEMENTS = [
    {"zone": "us-central1-a", "machine-type": "c3-standard-192-metal"},
    {"zone": "us-central1-c", "machine-type": "c3-highcpu-192-metal"},
]


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

    def test_each_agent_sandbox_case_pairs_with_its_substrate_case(self):
        cases = load_cases(SCENARIOS_FILE)
        paired = [name for name in cases if name.endswith("-agent-sandbox")]
        self.assertTrue(paired)
        for name in paired:
            pair = copy.deepcopy(cases[name.removesuffix("-agent-sandbox")])
            self.assertEqual(pair.pop("provider"), "agent-substrate")
            self.assertFalse(pair.pop("skip-substrate-install", False))
            mine = copy.deepcopy(cases[name])
            self.assertEqual(mine.pop("provider"), "agent-sandbox")
            self.assertTrue(mine.pop("skip-substrate-install"))
            self.assertEqual(mine, pair, f"{name} differs from its substrate case in more than the provider")

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

    def test_load_fills_in_the_provider(self):
        self.assertEqual(load({"c": GLUTTON})["c"]["provider"], "agent-substrate")
        case = load({"c": {**GLUTTON, "provider": "agent-sandbox"}})["c"]
        self.assertEqual(case["provider"], "agent-sandbox")
        self.assertTrue(case["skip-substrate-install"])

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
            {**GLUTTON, "provider": "e2b"},
            {**GLUTTON, "provider": "agent-sandbox", "sandbox-class": "microvm"},
            {"type": "noop", "infra": GLUTTON["infra"], "provider": "agent-sandbox"},
        ):
            with self.subTest(case=case), self.assertRaises(ValueError):
                load({"c": case})
        with self.assertRaises(ValueError):
            load({"c": {**GLUTTON, "bare-metal": "yes"}})


class BareMetal(unittest.TestCase):
    def test_shipped_settings_load(self):
        settings = load_bare_metal(SCENARIOS_FILE)
        self.assertIsInstance(settings["default"], bool)
        self.assertTrue(settings["placements"])
        self.assertTrue(all(p["machine-type"].endswith("-metal") for p in settings["placements"]))

    def test_settings_default_to_none(self):
        self.assertEqual(load_metal({}), {"default": False, "placements": []})

    def test_bad_settings_are_refused(self):
        for settings in (
            {"default": "yes"},
            {"placements": [{"zone": "us-central1-a"}]},
            {"placements": [{"zone": "us-central1-a", "machine-type": "c2-standard-16"}]},
            {"unknown": 1},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                load_metal(settings)

    def test_a_case_on_bare_metal_is_renamed_and_placed_first(self):
        cases = with_bare_metal({"c": {**GLUTTON, "bare-metal": True}}, {"c"}, False, None, PLACEMENTS)
        self.assertEqual(list(cases), ["c-metal"])
        variables = cases["c-metal"]["infra"]["cluster"]["variables"]
        self.assertEqual(variables, {"zone": "us-central1-a", "sandboxed_machine_type": "c3-standard-192-metal"})
        repeats = {"repeats": {"count": 2, "warmups": 1}}
        self.assertEqual(record_names("c-metal", repeats), ["c-metal-1", "c-metal-2"])
        self.assertEqual(warmup_names("c-metal", repeats), ["c-metal-warmup-1"])

    def test_the_case_wins_over_the_default_and_the_override_over_both(self):
        cases = {"on": {**GLUTTON, "bare-metal": True}, "off": {**GLUTTON, "bare-metal": False}, "unsaid": GLUTTON}
        for default, override, expected in (
            (True, None, ["on-metal", "off", "unsaid-metal"]),
            (False, None, ["on-metal", "off", "unsaid"]),
            (False, True, ["on-metal", "off-metal", "unsaid-metal"]),
            (True, False, ["on", "off", "unsaid"]),
        ):
            with self.subTest(default=default, override=override):
                self.assertEqual(list(with_bare_metal(cases, set(cases), default, override, PLACEMENTS)), expected)

    def test_a_noop_goes_on_bare_metal_only_if_it_says_so(self):
        noop = {"type": "noop", "infra": {"cluster": {"name": "common"}}}
        self.assertEqual(list(with_bare_metal({"n": noop}, {"n"}, True, True, PLACEMENTS)), ["n"])
        explicit = {"n": {**noop, "bare-metal": True}}
        self.assertEqual(list(with_bare_metal(explicit, {"n"}, False, None, PLACEMENTS)), ["n-metal"])

    def test_a_case_on_bare_metal_is_not_recorded_as_another(self):
        with self.assertRaisesRegex(ValueError, "which is another case"):
            with_bare_metal({"c": GLUTTON}, {"c", "c-metal"}, True, None, PLACEMENTS)

    def test_bare_metal_needs_a_placement(self):
        with self.assertRaisesRegex(ValueError, "has none in its region"):
            with_bare_metal({"c": GLUTTON}, {"c"}, True, None, [])
