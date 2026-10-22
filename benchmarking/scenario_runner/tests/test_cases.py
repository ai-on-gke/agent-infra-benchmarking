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

from unittest import mock

from scenario_runner import benchmark, locust, sandbox_benchmark
from scenario_runner.cases import (
    SCENARIOS_FILE,
    grouped,
    load_bare_metal,
    load_cases,
    record_names,
    warmup_names,
    with_bare_metal,
)
from scenario_runner.cluster_configs import CommonCluster, DummyCluster, OpenclawCluster
from scenario_runner.reports import VARIANT

GLUTTON = {
    "locust": {
        "master": {"run-time": "1m", "users": 10},
        "worker": {"workload-name": "glutton", "workload-template": "glutton"},
    },
}


# As load_cases has it.
LOADED = {**GLUTTON, "type": "locust"}


def glutton(locust: dict = None, **case) -> dict:
    """GLUTTON with locust's keys on top of its, and the case's on top of it."""
    return {**GLUTTON, "locust": {**GLUTTON["locust"], **(locust or {})}, **case}


# An openclaw case on the cluster it needs.
OPENCLAW = {
    "worker": {"workload-name": "openclaw", "workload-template": "openclaw"},
    "infra": {"cluster": {"configuration": "openclaw"}},
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
        # gVisor and microvm compare only if the cases differ in the class, and
        # the nested virtualization microvm boots its guests on, alone.
        cases = load_cases(SCENARIOS_FILE)
        for name, case in cases.items():
            if not name.endswith("-microvm"):
                continue
            pair = copy.deepcopy(cases[name.removesuffix("-microvm")])
            self.assertEqual(pair.pop("sandbox-class"), "gvisor")
            mine = copy.deepcopy(case)
            self.assertEqual(mine.pop("sandbox-class"), "microvm")
            self.assertIs(mine["locust"]["infra"]["cluster"].pop("nested-virtualization"), True)
            self.assertEqual(mine, pair, f"{name} differs from its gVisor case in more than the class")

    def test_each_case_asks_for_its_cluster(self):
        cases = load_cases(SCENARIOS_FILE)
        clusters = {name: benchmark.cluster_config(name, case) for name, case in cases.items()}
        self.assertEqual(clusters["glutton"], CommonCluster(sandboxed_nodes=1))
        self.assertEqual(clusters["glutton-microvm"], CommonCluster(sandboxed_nodes=1, nested_virtualization=True))
        self.assertEqual(clusters["openclaw-baseline"], OpenclawCluster(sandboxed_nodes=10))
        self.assertEqual(clusters["sandbox-benchmark-e2b"].sandboxed_machine_type, "n2-standard-16")
        self.assertEqual(clusters["dummy"], DummyCluster(sandboxed_nodes=10))


class CaseChecks(unittest.TestCase):
    def test_defaults(self):
        case = locust.check_case("glutton", GLUTTON, deploy_workloads=True)
        self.assertEqual(case["sandbox-class"], "gvisor")
        self.assertEqual(case["control-plane"], "agent-substrate")
        options = case["locust"]
        self.assertEqual(options["actor-memory"], "256Mi")
        self.assertEqual(options["locust-workers"], 1)
        self.assertEqual(options["max-failure-ratio"], locust.DEFAULT_MAX_FAILURE_RATIO)
        self.assertEqual(options["master"]["spawn-rate"], 10)

    def test_openclaw_gets_its_memory(self):
        case = glutton(OPENCLAW)
        self.assertEqual(locust.check_case("openclaw", case, True)["locust"]["actor-memory"], "2Gi")

    def test_openclaw_on_microvm_needs_2gi(self):
        case = glutton({**OPENCLAW, "actor-memory": "1Gi"}, **{"sandbox-class": "microvm"})
        with self.assertRaisesRegex(ValueError, "less than openclaw needs on microvm"):
            locust.check_case("openclaw-microvm", case, True)
        locust.check_case("openclaw", {**case, "sandbox-class": "gvisor"}, True)

    def test_bad_values_are_refused(self):
        for key, value in [
            ("actor-memory", "lots"),
            ("locust-workers", 0),
            ("max-failure-ratio", 2),
            ("worker-count", -1),
            ("users", 10),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                locust.check_case("c", glutton({key: value}), True)
        for key, value in [("sandbox-class", "kata"), ("control-plane", "none")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                locust.check_case("c", glutton(**{key: value}), True)

    def test_several_worker_pods_are_expected_by_the_master(self):
        case = locust.check_case("c", glutton({"locust-workers": 3}), True)
        args = locust.master_args(case["locust"])
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

    def test_load_fills_in_the_type_from_its_mapping(self):
        self.assertEqual(load({"c": GLUTTON})["c"]["type"], "locust")
        noop = load({"n": {"noop": None}})["n"]
        self.assertEqual((noop["type"], noop["noop"]), ("noop", {}))

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
            {**GLUTTON, "infra": {"cluster": {}}},
            glutton({"infra": {"cluster": {"unknown": 1}}}),
            glutton({"infra": {"cluster": {"name": "common"}}}),
            glutton({"infra": {"cluster": {"configuration": "dummy"}}}),
            glutton({"infra": {"cluster": {"sandboxed-nodes": 0}}}),
            glutton({"infra": {"pool": {}}}),
            {**GLUTTON, "type": "locust"},
            {"repeats": {}},
            {**GLUTTON, "noop": {}},
            {"noop": {"x": 1}},
            {"noop": {"infra": {"cluster": {"configuration": "nowhere"}}}},
            {"noop": {"infra": {"cluster": {"configuration": "dummy", "default-machine-type": "n2-standard-16"}}}},
            {**GLUTTON, "jmeter": {}},
            {**GLUTTON, "worker-count": 10},
            {**GLUTTON, "locust": None},
            {**GLUTTON, "control-plane": "k8s"},
        ):
            with self.subTest(case=case), self.assertRaises(ValueError):
                load({"c": case})
        with self.assertRaises(ValueError):
            load({"c": {**GLUTTON, "bare-metal": "yes"}})


class Clusters(unittest.TestCase):
    def test_locust_asks_for_its_configuration(self):
        cluster = {"sandboxed-nodes": 3, "nested-virtualization": True}
        case = load({"c": glutton({"infra": {"cluster": cluster}}, **{"sandbox-class": "microvm"})})["c"]
        self.assertEqual(benchmark.cluster_config("c", case), CommonCluster(3, nested_virtualization=True))
        openclaw = load({"c": glutton({"infra": {"cluster": {"configuration": "openclaw"}}})})["c"]
        self.assertEqual(benchmark.cluster_config("c", openclaw), OpenclawCluster())

    def test_a_workload_that_calls_mock_llm_needs_openclaw(self):
        with self.assertRaisesRegex(ValueError, "set `locust.infra.cluster.configuration: openclaw`"):
            load({"c": glutton({"worker": OPENCLAW["worker"]})})
        load({"c": glutton(OPENCLAW)})

    def test_a_noop_picks_its_configuration_common_by_default(self):
        self.assertEqual(benchmark.cluster_config("n", load({"n": {"noop": None}})["n"]), CommonCluster())
        dummy = load({"n": {"noop": {"infra": {"cluster": {"configuration": "dummy", "sandboxed-nodes": 2}}}}})["n"]
        self.assertEqual(benchmark.cluster_config("n", dummy), DummyCluster(2))

    def test_what_is_wrong_is_named_where_it_is(self):
        with self.assertRaisesRegex(ValueError, r"case c\S*'s `locust.infra.cluster`: has unknown size; it takes"):
            load({"c": glutton({"infra": {"cluster": {"size": 1}}})})


class Variants(unittest.TestCase):
    def test_shipped_sandbox_benchmark_is_a_case_per_provider(self):
        cases = load_cases(SCENARIOS_FILE)
        members = grouped(cases)["sandbox-benchmark"]
        self.assertEqual(members[0], "sandbox-benchmark-substrate")
        self.assertNotIn("sandbox-benchmark", cases)
        for name in members:
            options = cases[name]["sandbox-benchmark"]
            self.assertEqual(name, f"sandbox-benchmark-{options['provider']['name']}")
            self.assertEqual(options["tests"], ["test_suspend_resume"])
            self.assertEqual(cases[name][VARIANT], {"of": "sandbox-benchmark", "name": options["provider"]["name"]})
        self.assertEqual(cases["sandbox-benchmark-substrate"]["sandbox-benchmark"]["provider"]["env-worker-count"], 3)
        self.assertEqual(cases["sandbox-benchmark-e2b"]["sandbox-benchmark"]["provider"], {"name": "e2b"})

    def test_a_variant_changes_any_key_and_merges_mappings(self):
        variants = {"micro": {"sandbox-class": "microvm", "locust": {"master": {"users": 3}}}, "plain": None}
        cases = load({"c": {**GLUTTON, "variants": variants}, "d": GLUTTON})
        self.assertEqual(list(cases), ["c-micro", "c-plain", "d"])
        self.assertEqual(cases["c-micro"]["sandbox-class"], "microvm")
        self.assertEqual(cases["c-micro"]["locust"]["master"], {"run-time": "1m", "users": 3})
        self.assertEqual(cases["c-plain"]["locust"]["master"], GLUTTON["locust"]["master"])
        self.assertEqual(grouped(cases), {"c": ["c-micro", "c-plain"], "d": ["d"]})

    def test_bad_variants_are_refused(self):
        for case in (
            {**GLUTTON, "variants": {}},
            {**GLUTTON, "variants": ["a"]},
            {**GLUTTON, "variants": {"a": 1}},
            {**GLUTTON, "variants": {"a b": {}}},
            {**GLUTTON, "variants": {"a": {"type": "sandbox-benchmark"}}},
            {**GLUTTON, "variants": {"a": {"sandbox-benchmark": {}}}},
            {**GLUTTON, "variants": {"a": {"variants": {"b": {}}}}},
            {**GLUTTON, "variants": {"a": {VARIANT: {}}}},
            {**GLUTTON, VARIANT: {"of": "x", "name": "y"}},
        ):
            with self.subTest(case=case), self.assertRaises(ValueError):
                load({"c": case})
        with self.assertRaisesRegex(ValueError, "which is another case"):
            load({"c": {**GLUTTON, "variants": {"a": {}}}, "c-a": GLUTTON})

    def test_a_variant_on_bare_metal_is_one_of_the_case_on_it(self):
        cases = load({"c": {**GLUTTON, "variants": {"a": {"bare-metal": True}, "b": {}}}})
        cases = with_bare_metal(cases, set(cases), False, None, PLACEMENTS)
        self.assertEqual(grouped(cases), {"c-metal": ["c-a-metal"], "c": ["c-b"]})


class SandboxBenchmarkCases(unittest.TestCase):
    OPTIONS = {"tests": ["test_x"], "runs": 1}

    def check(self, provider: object = None, **case) -> dict:
        options = {**self.OPTIONS, **({"provider": provider} if provider is not None else {})}
        with mock.patch.object(sandbox_benchmark, "test_names", return_value=None):
            return sandbox_benchmark.check_case("c", {"sandbox-benchmark": options, **case}, True)

    def test_substrate_by_default_with_its_options_filled_in(self):
        def provider(given=None) -> dict:
            return self.check(given)["sandbox-benchmark"]["provider"]

        self.assertEqual(provider(), {"name": "substrate", **sandbox_benchmark.PROVIDER_OPTIONS["substrate"]})
        self.assertEqual(provider({"name": "substrate", "actor-cpu": "4"})["actor-cpu"], "4")
        self.assertEqual(provider({"name": "e2b"}), {"name": "e2b"})

    def test_bad_providers_are_refused(self):
        for provider in (
            "e2b",
            {},
            {"name": "aws"},
            {"name": "e2b", "actor-cpu": "2"},
            {"name": "substrate", "x": 1},
            {"name": "substrate", "env-worker-count": 0},
            {"name": "substrate", "actor-cpu": 2},
        ):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                self.check(provider)

    def test_substrate_needs_its_control_plane(self):
        with self.assertRaisesRegex(ValueError, "needs `control-plane: agent-substrate`"):
            self.check(**{"control-plane": "none"})
        self.check({"name": "e2b"}, **{"control-plane": "none"})

    def test_unknown_keys_are_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown comparator"):
            self.check(**{"sandbox-benchmark": {**self.OPTIONS, "comparator": {}}})


class SandboxBenchmarkReports(unittest.TestCase):
    CASE = {"type": "sandbox-benchmark", "sandbox-benchmark": {"provider": {"name": "e2b"}}}

    def test_a_variant_reports_beside_the_others_of_its_group(self):
        variant = {**self.CASE, VARIANT: {"of": "sb-metal", "name": "fast"}}
        self.assertEqual(sandbox_benchmark.report_folder("sb-fast-metal-2", "sb-fast-metal", variant), "sb-metal-2/fast")
        self.assertEqual(sandbox_benchmark.report_folder("sb-2", "sb", self.CASE), "sb-2/e2b")

    def test_summarize_combines_each_record_s_variants_in_order(self):
        a = {**self.CASE, VARIANT: {"of": "sb", "name": "a"}, "sandbox-benchmark": {"cost-sheet": True}}
        b = {**self.CASE, VARIANT: {"of": "sb", "name": "b"}}
        runs = [("sb-a-1", "sb-a", a), ("sb-a-2", "sb-a", a), ("sb-b-1", "sb-b", b), ("sb-b-2", "sb-b", b)]
        with mock.patch.object(sandbox_benchmark, "combine_reports") as combine:
            sandbox_benchmark.summarize(runs, "run-1", "bucket", "reports")
        self.assertEqual(
            combine.call_args_list,
            [
                mock.call("bucket", "reports", "run-1", "sb-1", ["a", "b"], True),
                mock.call("bucket", "reports", "run-1", "sb-2", ["a", "b"], True),
            ],
        )


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

    def test_a_case_on_bare_metal_is_renamed(self):
        cases = with_bare_metal({"c": {**LOADED, "bare-metal": True}}, {"c"}, False, None, PLACEMENTS)
        self.assertEqual(cases, {"c-metal": {**LOADED, "bare-metal": True}})
        repeats = {"repeats": {"count": 2, "warmups": 1}}
        self.assertEqual(record_names("c-metal", repeats), ["c-metal-1", "c-metal-2"])
        self.assertEqual(warmup_names("c-metal", repeats), ["c-metal-warmup-1"])

    def test_the_case_wins_over_the_default_and_the_override_over_both(self):
        cases = {"on": {**LOADED, "bare-metal": True}, "off": {**LOADED, "bare-metal": False}, "unsaid": LOADED}
        for default, override, expected in (
            (True, None, ["on-metal", "off", "unsaid-metal"]),
            (False, None, ["on-metal", "off", "unsaid"]),
            (False, True, ["on-metal", "off-metal", "unsaid-metal"]),
            (True, False, ["on", "off", "unsaid"]),
        ):
            with self.subTest(default=default, override=override):
                self.assertEqual(list(with_bare_metal(cases, set(cases), default, override, PLACEMENTS)), expected)

    def test_a_noop_goes_on_bare_metal_only_if_it_says_so(self):
        noop = {"type": "noop", "noop": {}}
        self.assertEqual(list(with_bare_metal({"n": noop}, {"n"}, True, True, PLACEMENTS)), ["n"])
        explicit = {"n": {**noop, "bare-metal": True}}
        self.assertEqual(list(with_bare_metal(explicit, {"n"}, False, None, PLACEMENTS)), ["n-metal"])

    def test_a_case_on_bare_metal_is_not_recorded_as_another(self):
        with self.assertRaisesRegex(ValueError, "which is another case"):
            with_bare_metal({"c": LOADED}, {"c", "c-metal"}, True, None, PLACEMENTS)

    def test_bare_metal_needs_a_placement(self):
        with self.assertRaisesRegex(ValueError, "has none in its region"):
            with_bare_metal({"c": LOADED}, {"c"}, True, None, [])
