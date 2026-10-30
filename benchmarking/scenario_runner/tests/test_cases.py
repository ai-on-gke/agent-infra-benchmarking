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

import argparse
import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from unittest import mock

from scenario_runner import benchmarks
from scenario_runner.benchmarks import locust, sandbox_benchmark
from scenario_runner.infra.cluster_configs import CommonCluster, DummyCluster, OpenclawCluster
from scenario_runner.scenarios.bare_metal import Settings, with_bare_metal
from scenario_runner.scenarios.file import SCENARIOS_FILE, load_scenarios
from scenario_runner.scenarios.repeats import record_names, warmup_names
from scenario_runner.scenarios.variants import VARIANT, grouped

SHIPPED = load_scenarios(SCENARIOS_FILE)

GLUTTON = {
    "locust": {
        "install": {"substrate": {"sandbox-class": "gvisor"}},
        "master": {"run-time": "1m", "users": 10},
        "worker": {"workload-name": "glutton", "workload-template": "glutton"},
    },
}


# As load_scenarios has it.
LOADED = {**GLUTTON, "type": "locust"}


def glutton(locust: dict = None, **case) -> dict:
    """GLUTTON with locust's keys on top of its, and the case's on top of it."""
    return {**GLUTTON, "locust": {**GLUTTON["locust"], **(locust or {})}, **case}


def on(sandbox_class: str) -> dict:
    """locust's keys for a case that installs substrate with sandbox_class."""
    return {"install": {"substrate": {"sandbox-class": sandbox_class}}}


def check(name: str, case: dict) -> dict:
    """locust.check_case's case, typed as load_scenarios has it."""
    return locust.check_case(name, {**case, "type": "locust"}, True)


# An openclaw case on the cluster it needs.
OPENCLAW = {
    "worker": {"workload-name": "openclaw", "workload-template": "openclaw"},
    "infra": {"cluster": {"configuration": "openclaw"}},
}


def load_file(scenarios: dict):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(scenarios, f, sort_keys=False)
    try:
        return load_scenarios(Path(f.name))
    finally:
        Path(f.name).unlink()


def load(cases: dict) -> dict:
    return load_file({"cases": cases}).cases


def load_metal(settings) -> Settings:
    return load_file({"bare-metal": settings, "cases": {"c": GLUTTON}}).bare_metal


PLACEMENTS = [
    {"zone": "us-central1-a", "machine-type": "c3-standard-192-metal"},
    {"zone": "us-central1-c", "machine-type": "c3-highcpu-192-metal"},
]


class ShippedCases(unittest.TestCase):
    def test_every_shipped_case_loads(self):
        self.assertIn("glutton-substrate-gvisor", SHIPPED.cases)

    def test_each_microvm_case_pairs_with_its_gvisor_case(self):
        # gVisor and microvm compare only if the cases differ in the class, and
        # the nested virtualization microvm boots its guests on, alone.
        cases = SHIPPED.cases
        for name, case in cases.items():
            if not name.endswith("-substrate-microvm"):
                continue
            pair = copy.deepcopy(cases[name.removesuffix("-microvm") + "-gvisor"])
            self.assertEqual(pair["locust"].pop("install"), on("gvisor")["install"])
            mine = copy.deepcopy(case)
            self.assertEqual(mine["locust"].pop("install"), on("microvm")["install"])
            self.assertIs(mine["locust"]["infra"]["cluster"].pop("nested-virtualization"), True)
            self.assertEqual(mine, pair, f"{name} differs from its gVisor case in more than the class")

    def test_each_agent_sandbox_case_pairs_with_its_substrate_case(self):
        cases = SHIPPED.cases
        paired = [name for name in cases if name.endswith("-agent-sandbox-gvisor")]
        self.assertTrue(paired)
        for name in paired:
            pair = copy.deepcopy(cases[name.removesuffix("-agent-sandbox-gvisor") + "-substrate-gvisor"])
            self.assertEqual(pair["locust"].pop("install"), on("gvisor")["install"])
            mine = copy.deepcopy(cases[name])
            self.assertEqual(mine["locust"].pop("install"), {"agent-sandbox": {"sandbox-class": "gvisor"}})
            # Its pods' gVisor is GKE Sandbox's.
            self.assertIs(mine["locust"]["infra"]["cluster"].pop("gke-sandbox"), True)
            self.assertEqual(mine, pair, f"{name} differs from its substrate case in more than the control plane")

    def test_shared_workers_is_glutton_single_on_fewer_bounded_workers(self):
        # So it compares, and runs on glutton-single's dev infra.
        cases = copy.deepcopy(SHIPPED.cases)
        mine, pair = cases["glutton-shared-workers"], cases["glutton-single"]
        self.assertEqual(mine["locust"].pop("workers"), {"count": 2, "limits": {"memory": "1536Mi"}})
        pair["locust"].pop("workers")
        self.assertEqual(mine, pair)

    def test_each_case_asks_for_its_cluster(self):
        cases = SHIPPED.cases
        clusters = {name: benchmarks.cluster_config(name, case) for name, case in cases.items()}
        self.assertEqual(clusters["glutton-substrate-gvisor"], CommonCluster(sandboxed_nodes=30))
        self.assertEqual(
            clusters["glutton-substrate-microvm"], CommonCluster(sandboxed_nodes=30, nested_virtualization=True)
        )
        self.assertEqual(clusters["openclaw-density-baseline"], OpenclawCluster(sandboxed_nodes=10))
        self.assertEqual(
            clusters["sandbox-benchmark-substrate"],
            CommonCluster(sandboxed_nodes=1, sandboxed_machine_type="n2-standard-16"),
        )
        self.assertEqual(clusters["dummy"], DummyCluster(sandboxed_nodes=10))


class CaseChecks(unittest.TestCase):
    def test_defaults(self):
        case = locust.check_case("glutton", LOADED, deploy_workloads=True)
        options = case["locust"]
        self.assertEqual(options["actor-memory"], "256Mi")
        self.assertEqual(options["locust-workers"], 1)
        self.assertEqual(options["max-failure-ratio"], locust.DEFAULT_MAX_FAILURE_RATIO)
        self.assertEqual(options["master"]["spawn-rate"], 10)

    def test_openclaw_gets_its_memory(self):
        case = glutton(OPENCLAW)
        self.assertEqual(check("openclaw", case)["locust"]["actor-memory"], "2Gi")

    def test_openclaw_on_microvm_needs_2gi(self):
        case = glutton({**OPENCLAW, "actor-memory": "1Gi", **on("microvm")})
        with self.assertRaisesRegex(ValueError, "less than openclaw needs on microvm"):
            check("openclaw-microvm", case)
        check("openclaw", glutton({**OPENCLAW, "actor-memory": "1Gi"}))

    def test_bad_values_are_refused(self):
        for key, value in [
            ("actor-memory", "lots"),
            ("locust-workers", 0),
            ("max-failure-ratio", 2),
            ("worker-count", 10),
            ("workers", 10),
            ("workers", {"count": -1}),
            ("workers", {"size": 1}),
            ("workers", {"limits": {}}),
            ("workers", {"limits": {"gpu": "1"}}),
            ("workers", {"limits": {"cpu": 2}}),
            ("workers", {"limits": {"memory": "lots"}}),
            ("users", 10),
            ("user-class", "glutton"),
            ("user-class", "_master"),
            ("user-class", None),
        ]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                check("c", glutton({key: value}))
        with self.assertRaisesRegex(ValueError, "must name substrate or agent-sandbox"):
            check("c", glutton({"install": {}}))

    def test_workers_default_to_one_without_limits(self):
        self.assertEqual(check("c", GLUTTON)["locust"]["workers"], {"count": 1})
        workers = {"count": 2, "limits": {"cpu": "4", "memory": "1Gi"}}
        self.assertEqual(check("c", glutton({"workers": workers}))["locust"]["workers"], workers)

    def test_worker_limits_must_fit_an_actor(self):
        with self.assertRaisesRegex(ValueError, "leaves no room for one actor of 512Mi"):
            check("c", glutton({"actor-memory": "512Mi", "workers": {"limits": {"memory": "256Mi"}}}))
        check("c", glutton({"actor-memory": "512Mi", "workers": {"limits": {"memory": "512Mi"}}}))

    def test_substrate_suspends_asynchronously_unless_the_case_says(self):
        self.assertEqual(check("c", GLUTTON)["locust"]["worker"]["suspend-mode"], "async")
        sync = check("c", glutton({"worker": {"workload-name": "glutton", "suspend-mode": "sync"}}))
        self.assertEqual(sync["locust"]["worker"]["suspend-mode"], "sync")
        with self.assertRaisesRegex(ValueError, "must be async or sync, got 'pause'"):
            check("c", glutton({"worker": {"workload-name": "glutton", "suspend-mode": "pause"}}))
        agent_sandbox = {"install": {"agent-sandbox": {"sandbox-class": "gvisor"}}}
        self.assertNotIn("suspend-mode", check("c", glutton(agent_sandbox))["locust"]["worker"])
        with self.assertRaisesRegex(ValueError, "suspend-mode` is substrate's"):
            check("c", glutton({**agent_sandbox, "worker": {"workload-name": "glutton", "suspend-mode": "sync"}}))

    def test_worker_limits_are_substrate_s(self):
        agent_sandbox = {"install": {"agent-sandbox": {"sandbox-class": "gvisor"}}}
        with self.assertRaisesRegex(ValueError, "agent-sandbox has none"):
            check("c", glutton({**agent_sandbox, "workers": {"limits": {"memory": "1Gi"}}}))
        check("c", glutton({**agent_sandbox, "workers": {"count": 3}}))

    def test_several_worker_pods_are_expected_by_the_master(self):
        case = check("c", glutton({"locust-workers": 3}))
        args = locust.master_args(case["locust"])
        self.assertEqual(args[args.index("--expect-workers") + 1], "3")

    def test_runworkload_by_default(self):
        options = check("c", GLUTTON)["locust"]
        self.assertEqual(options["user-class"], "runworkload")
        self.assertEqual(
            locust.master_args(options),
            ["-f", "/app/tests/runworkload.py", "--headless", "--master", "--expect-workers", "1",
             "--expect-workers-max-wait", "300", "--exit-code-on-error", "0",
             "--run-time=1m", "--users=10", "--spawn-rate=10"],
        )  # fmt: skip
        self.assertEqual(
            locust.worker_args(options),
            [f"--master-host={locust.MASTER_HOST}", "--user-class=runworkload", "--suspend-mode=async",
             "--workload-name=glutton", "--workload-template=glutton"],
        )  # fmt: skip

    def test_the_user_class_picks_the_master_s_locustfile_and_the_worker_s_class(self):
        tests = Path(tempfile.mkdtemp())
        for locustfile in ("runworkload.py", "other.py", "_master.py"):
            (tests / locustfile).touch()
        with mock.patch.object(locust, "LOCUSTFILES_DIR", tests):
            self.assertEqual(locust.user_classes(), ["other", "runworkload"])
            options = check("c", glutton({"user-class": "other"}))["locust"]
        master = locust.master_args(options)
        self.assertEqual(master[:2], ["-f", "/app/tests/other.py"])
        self.assertIn("--user-class=other", locust.worker_args(options))

    def test_the_master_has_a_locustfile_for_density_and_runworkload(self):
        self.assertEqual(locust.user_classes(), ["density", "runworkload"])

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

    def test_gke_sandbox_is_for_agent_sandbox_and_needed_by_it(self):
        agent_sandbox = {"install": {"agent-sandbox": {"sandbox-class": "gvisor"}}}
        with self.assertRaisesRegex(ValueError, "set its `locust.infra.cluster.gke-sandbox: true`"):
            load({"c": glutton(agent_sandbox)})
        with self.assertRaisesRegex(ValueError, "`locust.infra.cluster.gke-sandbox` is only for cases on agent-sandbox"):
            load({"c": glutton({"infra": {"cluster": {"gke-sandbox": True}}})})
        case = load({"c": glutton({**agent_sandbox, "infra": {"cluster": {"gke-sandbox": True}}})})["c"]
        self.assertIs(benchmarks.cluster_config("c", case).gke_sandbox, True)

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
            {**GLUTTON, "workers": {"count": 10}},
            {**GLUTTON, "locust": None},
            {**GLUTTON, "install": {"substrate": {"sandbox-class": "gvisor"}}},
            glutton({"install": []}),
            glutton({"install": {"k8s": {}}}),
            glutton({"install": {"substrate": None}}),
            glutton({"install": {"substrate": {}}}),
            glutton({"install": {"substrate": {"sandbox-class": "kata"}}}),
            glutton({"install": {"substrate": {"sandbox-class": "gvisor", "x": 1}}}),
            glutton({"install": {**on("gvisor")["install"], "agent-sandbox": {"sandbox-class": "gvisor"}}}),
            glutton({"install": {"agent-sandbox": {"sandbox-class": "microvm"}}}),
            glutton({"install": {"agent-sandbox": {"sandbox-class": "gvisor"}}}),
            glutton({"infra": {"cluster": {"gke-sandbox": True}}}),
            {"noop": {"infra": {"cluster": {"gke-sandbox": True}}}},
            {"noop": {"install": {"substrate": {}}}},
        ):
            with self.subTest(case=case), self.assertRaises(ValueError):
                load({"c": case})
        with self.assertRaises(ValueError):
            load({"c": {**GLUTTON, "bare-metal": "yes"}})


class Clusters(unittest.TestCase):
    def test_locust_asks_for_its_configuration(self):
        cluster = {"sandboxed-nodes": 3, "nested-virtualization": True}
        case = load({"c": glutton({"infra": {"cluster": cluster}, **on("microvm")})})["c"]
        self.assertEqual(benchmarks.cluster_config("c", case), CommonCluster(3, nested_virtualization=True))
        openclaw = load({"c": glutton({"infra": {"cluster": {"configuration": "openclaw"}}})})["c"]
        self.assertEqual(benchmarks.cluster_config("c", openclaw), OpenclawCluster())

    def test_a_workload_that_calls_mock_llm_needs_openclaw(self):
        with self.assertRaisesRegex(ValueError, "set `locust.infra.cluster.configuration: openclaw`"):
            load({"c": glutton({"worker": OPENCLAW["worker"]})})
        load({"c": glutton(OPENCLAW)})

    def test_a_noop_picks_its_configuration_common_by_default(self):
        self.assertEqual(benchmarks.cluster_config("n", load({"n": {"noop": None}})["n"]), CommonCluster())
        dummy = load({"n": {"noop": {"infra": {"cluster": {"configuration": "dummy", "sandboxed-nodes": 2}}}}})["n"]
        self.assertEqual(benchmarks.cluster_config("n", dummy), DummyCluster(2))

    def test_what_is_wrong_is_named_where_it_is(self):
        with self.assertRaisesRegex(ValueError, r"case c\S*'s `locust.infra.cluster`: has unknown size; it takes"):
            load({"c": glutton({"infra": {"cluster": {"size": 1}}})})


class Variants(unittest.TestCase):
    def test_shipped_sandbox_benchmark_is_a_case_per_provider(self):
        cases = SHIPPED.cases
        members = grouped(cases)["sandbox-benchmark"]
        self.assertEqual(members[0], "sandbox-benchmark-substrate")
        self.assertNotIn("sandbox-benchmark", cases)
        for name in members:
            options = cases[name]["sandbox-benchmark"]
            self.assertEqual(name, f"sandbox-benchmark-{options['provider']['name']}")
            self.assertEqual(options["tests"], ["test_suspend_resume"])
            self.assertEqual(cases[name][VARIANT], {"of": "sandbox-benchmark", "name": options["provider"]["name"]})
        self.assertEqual(cases["sandbox-benchmark-substrate"]["sandbox-benchmark"]["provider"]["env-worker-count"], 10)
        for name in ("e2b", "modal", "daytona"):
            provider = cases[f"sandbox-benchmark-{name}"]["sandbox-benchmark"]["provider"]
            self.assertEqual(provider, {"name": name, "cpu": "2", "memory": "2Gi"})
        openai = cases["sandbox-benchmark-openai"]["sandbox-benchmark"]["provider"]
        self.assertEqual(openai, {"name": "openai", "container-size": "medium"})

    def test_a_variant_s_keys_are_merged_on_top_of_the_case(self):
        variants = {"micro": {"locust": {**on("microvm"), "master": {"users": 3}}}, "plain": {}}
        cases = load({"c": {**GLUTTON, "variants": variants}, "d": GLUTTON})
        self.assertEqual(list(cases), ["c-micro", "c-plain", "d"])
        self.assertEqual(cases["c-micro"]["locust"]["install"], on("microvm")["install"])
        self.assertEqual(cases["c-micro"]["locust"]["master"], {"run-time": "1m", "users": 3})
        self.assertEqual(cases["c-micro"]["locust"]["worker"], GLUTTON["locust"]["worker"])
        self.assertEqual(cases["c-plain"]["locust"]["install"], GLUTTON["locust"]["install"])
        self.assertEqual(cases["c-plain"]["locust"]["master"], GLUTTON["locust"]["master"])
        self.assertEqual(grouped(cases), {"c": ["c-micro", "c-plain"], "d": ["d"]})

    def test_bad_variants_are_refused(self):
        for case in (
            {**GLUTTON, "variants": {}},
            {**GLUTTON, "variants": ["a"]},
            {**GLUTTON, "variants": {"a": 1}},
            {**GLUTTON, "variants": {"a b": {}}},
            {**GLUTTON, VARIANT: {"of": "x", "name": "y"}},
            {**GLUTTON, "variants": {"a": {"variants": {"b": {}}}}},
        ):
            with self.subTest(case=case), self.assertRaises(ValueError):
                load({"c": case})
        with self.assertRaisesRegex(ValueError, "which is another case"):
            load({"c": {**GLUTTON, "variants": {"a": {}}}, "c-a": GLUTTON})
        # A variant fills in no placeholder.
        case = glutton({"master": {"run-time": "1m", "users": "{users}"}})
        with self.assertRaisesRegex(ValueError, r"case c has \{users\}, which none of its parameters set"):
            load({"c": {**case, "variants": {"a": {"users": 1}}}})

    def test_a_key_a_variant_sets_to_null_is_dropped(self):
        case = glutton({"master": {"run-time": "1m", "users": 10, "warm-up": "10s"}})
        cases = load({"c": {**case, "variants": {"warm": {}, "cold": {"locust": {"master": {"warm-up": None}}}}}})
        self.assertEqual(cases["c-warm"]["locust"]["master"]["warm-up"], "10s")
        self.assertNotIn("warm-up", cases["c-cold"]["locust"]["master"])

    def test_a_variant_on_bare_metal_is_one_of_the_case_on_it(self):
        case = {**GLUTTON, "variants": {"a": {"bare-metal": True}, "b": {"bare-metal": False}}}
        cases = load({"c": case})
        cases = with_bare_metal(cases, set(cases), False, None, PLACEMENTS)
        self.assertEqual(grouped(cases), {"c-metal": ["c-a-metal"], "c": ["c-b"]})


class Parameters(unittest.TestCase):
    CASE = glutton({**on("{class}"), "master": {"run-time": "1m", "users": "{users}"}}, name="{workload}{suffix}")

    def expand(self, case: dict, parameters: dict) -> dict:
        name = case.pop("name", "c")
        return load({name: {**case, "parameters": parameters}})

    def test_a_case_of_each_choice_in_order(self):
        cases = self.expand(
            dict(self.CASE),
            {
                "load": [{"workload": "glutton", "users": 10}, {"workload": "busy", "users": 99}],
                "class": [{"suffix": "", "class": "gvisor"}, {"suffix": "-microvm", "class": "microvm"}],
            },
        )
        self.assertEqual(list(cases), ["glutton", "glutton-microvm", "busy", "busy-microvm"])
        self.assertEqual(cases["busy-microvm"]["locust"]["master"], {"run-time": "1m", "users": 99})
        self.assertEqual(cases["busy-microvm"]["locust"]["install"], on("microvm")["install"])
        self.assertNotIn("parameters", cases["glutton"])

    def test_a_value_keeps_its_type_and_null_drops_its_key(self):
        case = glutton({"master": {"run-time": "{time}", "users": "{users}", "warm-up": "{warm-up}"}})
        values = {"time": "1m", "users": 3, "count": 2, "warm-up": None}
        cases = self.expand({**case, "repeats": {"count": "{count}"}}, {"p": [values]})
        self.assertEqual(cases["c"]["locust"]["master"], {"run-time": "1m", "users": 3})
        self.assertEqual(cases["c"]["repeats"]["count"], 2)

    def test_a_key_takes_its_text(self):
        install = {"install": {"{plane}": {"sandbox-class": "{class}"}}, "infra": {"cluster": {"gke-sandbox": "{gke}"}}}
        case = glutton(install, name="{plane}-{class}")
        agent_sandbox = {"plane": "agent-sandbox", "class": "gvisor", "gke": True}
        parameters = {"p": [{"plane": "substrate", "class": "microvm"}, agent_sandbox]}
        cases = self.expand(case, parameters)
        self.assertEqual(cases["substrate-microvm"]["locust"]["install"], on("microvm")["install"])
        self.assertEqual(
            cases["agent-sandbox-gvisor"]["locust"]["install"], {"agent-sandbox": {"sandbox-class": "gvisor"}}
        )
        with self.assertRaisesRegex(ValueError, r"\{plane\} in '\{plane\}' must be a string or a number, got None"):
            self.expand(glutton({"install": {"{plane}": {"sandbox-class": "gvisor"}}}), {"p": [{"plane": None}]})
        twice = glutton({"master": {"{a}": 1, "{b}": 2}})
        with self.assertRaisesRegex(ValueError, "two keys are 'users'"):
            self.expand(twice, {"p": [{"a": "users", "b": "users"}]})

    def test_a_placeholder_an_entry_does_not_set_is_null(self):
        case = glutton({"master": {"run-time": "1m", "users": 10, "warm-up": "{warm-up}"}}, name="{workload}")
        cases = self.expand(case, {"p": [{"workload": "warm", "warm-up": "10s"}, {"workload": "cold"}]})
        self.assertEqual(cases["warm"]["locust"]["master"]["warm-up"], "10s")
        self.assertNotIn("warm-up", cases["cold"]["locust"]["master"])
        error = r"case \{workload\}-x has \{workload\}, which none of its parameters set"
        with self.assertRaisesRegex(ValueError, error):
            self.expand(glutton(name="{workload}-x"), {"p": [{"workload": "a"}, {}]})

    def test_parameters_fill_in_the_variants_too(self):
        case = glutton({"master": {"run-time": "{run-time}", "users": 10}})
        variants = {"short": {}, "long": {"locust": {"master": {"run-time": "{run-time-long}"}}}}
        cases = self.expand({**case, "variants": variants}, {"p": [{"run-time": "1m", "run-time-long": "9m"}]})
        self.assertEqual(cases["c-short"]["locust"]["master"], {"run-time": "1m", "users": 10})
        self.assertEqual(cases["c-long"]["locust"]["master"], {"run-time": "9m", "users": 10})

    def test_bad_parameters_are_refused(self):
        for parameters, error in (
            ([{"users": 1}], "must be a mapping of names to lists"),
            ({}, "must be a mapping of names to lists"),
            ({"p": []}, "must be a mapping of names to lists"),
            ({"p": [1]}, "must be a mapping of names to lists"),
            ({"p": [{"users": 1}], "q": [{"users": 2}]}, "sets users in more than one parameter"),
            ({"p": [{"users": 1}, {"users": 2}]}, "there are two cases c"),
            ({"p": [{"users": 1, "user": 1}]}, "parameters set user, which it does not use"),
        ):
            case = glutton({"master": {"run-time": "1m", "users": "{users}"}})
            with self.subTest(parameters=parameters), self.assertRaisesRegex(ValueError, error):
                self.expand(case, parameters)
        with self.assertRaisesRegex(ValueError, r"case c has \{users\}, which none of its parameters set"):
            self.expand(glutton({"master": {"run-time": "{time}", "users": "{users}"}}), {"p": [{"time": "1m"}]})
        with self.assertRaisesRegex(ValueError, r"\{workload\} in '\{workload\}-x' must be a string or a number"):
            load({"{workload}-x": {**GLUTTON, "parameters": {"p": [{"workload": None}]}}})
        # A case without parameters has no placeholder.
        with self.assertRaisesRegex(ValueError, r"case c has \{users\}, which none of its parameters set"):
            load({"c": glutton({"master": {"run-time": "1m", "users": "{users}"}})})


class SandboxBenchmarkCases(unittest.TestCase):
    OPTIONS = {"tests": ["test_x"], "runs": 1}

    def check(self, provider: object = None, install: dict | None = on("gvisor")["install"], **case) -> dict:
        options = {**self.OPTIONS, **({"provider": provider} if provider is not None else {})}
        if install is not None:
            options["install"] = install
        with mock.patch.object(sandbox_benchmark, "test_names", return_value=None):
            case = {"type": "sandbox-benchmark", "sandbox-benchmark": options, **case}
            return sandbox_benchmark.check_case("c", case, True)

    def test_substrate_by_default_with_its_options_filled_in(self):
        def provider(given=None) -> dict:
            return self.check(given)["sandbox-benchmark"]["provider"]

        self.assertEqual(provider(), {"name": "substrate", **sandbox_benchmark.PROVIDER_OPTIONS["substrate"]})
        self.assertEqual(provider({"name": "substrate", "actor-cpu": "4"})["actor-cpu"], "4")
        self.assertEqual(provider({"name": "local"}), {"name": "local"})
        self.assertEqual(provider({"name": "modal"}), {"name": "modal", "cpu": None, "memory": None})
        self.assertEqual(provider({"name": "modal", "cpu": "1500m", "memory": "1.5Gi"})["cpu"], "1500m")
        self.assertEqual(provider({"name": "daytona", "cpu": "2", "memory": "2Gi"})["memory"], "2Gi")
        self.assertEqual(provider({"name": "openai"}), {"name": "openai", "container-size": None})
        self.assertEqual(provider({"name": "openai", "container-size": "large"})["container-size"], "large")

    def test_bad_providers_are_refused(self):
        for provider in (
            "e2b",
            {},
            {"name": "aws"},
            {"name": "e2b", "actor-cpu": "2"},
            {"name": "substrate", "x": 1},
            {"name": "substrate", "env-worker-count": 0},
            {"name": "substrate", "actor-cpu": 2},
            {"name": "local", "cpu": "2"},
            {"name": "e2b", "cpu": "1500m"},
            {"name": "modal", "cpu": 2},
            {"name": "modal", "memory": "1G"},
            {"name": "daytona", "cpu": "1500m"},
            {"name": "daytona", "memory": "1536Mi"},
            {"name": "openai", "container-size": "huge"},
            {"name": "openai", "cpu": "2"},
        ):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                self.check(provider)

    def test_substrate_needs_its_control_plane(self):
        with self.assertRaisesRegex(ValueError, "needs `sandbox-benchmark.install.substrate.sandbox-class`"):
            self.check(install=None)
        self.check({"name": "e2b"}, install=None)

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


def load_suites_of(suites) -> dict:
    return load_file({"suites": suites, "cases": {"c": GLUTTON}}).suites


class Suites(unittest.TestCase):
    def test_shipped_suites_name_shipped_cases(self):
        cases = SHIPPED.cases
        known = set(cases) | set(grouped(cases))
        suites = SHIPPED.suites
        self.assertTrue({"nightly", "staging"} <= suites.keys())
        for name, members in suites.items():
            with self.subTest(suite=name):
                self.assertEqual([m for m in members if m not in known], [])

    def test_suites_default_to_none(self):
        self.assertEqual(load_suites_of({}), {})

    def test_bad_suites_are_refused(self):
        for suites in (["c"], {"s": "c"}, {"s": []}, {"s": [1]}):
            with self.subTest(suites=suites), self.assertRaises(ValueError):
                load_suites_of(suites)

    def test_a_suite_selects_its_cases_in_order(self):
        from scenario_runner.cli import run

        args = argparse.Namespace(
            scenarios_file=SCENARIOS_FILE, all=False, scenario=None, suite="staging",
            test=None, bare_metal=None, region="us-central1",
        )  # fmt: skip
        self.assertEqual(list(run.selected_cases(args)), SHIPPED.suites["staging"])
        with mock.patch.object(run, "log"), self.assertRaises(SystemExit):
            run.selected_cases(argparse.Namespace(**{**vars(args), "suite": "unknown"}))


class BareMetal(unittest.TestCase):
    def test_shipped_settings_load(self):
        settings = SHIPPED.bare_metal
        self.assertIsInstance(settings.default, bool)
        self.assertTrue(settings.placements)
        self.assertTrue(all(p["machine-type"].endswith("-metal") for p in settings.placements))

    def test_settings_default_to_none(self):
        self.assertEqual(load_metal({}), Settings())

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
