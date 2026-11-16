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

"""The checks run before anything is applied."""

import shutil
import unittest
from unittest import mock

from scenario_runner import preflight
from scenario_runner.infra.cluster_configs import CommonCluster

CONFIG = CommonCluster()
SETTINGS = {
    "nested_virtualization": False,
    "sandboxed_machine_type": "c2-standard-16",
    "additional_node_pools": [],
    "kubernetes_version": "1.35.8-gke.1225000",
    "zone": "us-central1-a",
    "regional_control_plane": False,
}


class Rules(unittest.TestCase):
    def test_nested_virtualization_is_intel_only(self):
        for machine_type in ("n2-standard-8", "c2-standard-16", "c3-standard-8", "n1-standard-4", "n2-custom-4-8192"):
            self.assertTrue(preflight.nested_virtualization(machine_type), machine_type)
        for machine_type in ("e2-standard-8", "n2d-standard-8", "c2d-standard-8", "t2a-standard-4", "c4a-standard-8"):
            self.assertFalse(preflight.nested_virtualization(machine_type), machine_type)

    def test_a_good_case_has_no_problems(self):
        self.assertEqual(preflight.cluster_problems("c", CONFIG, SETTINGS, ["1.35.8-gke.1225000"], True, "z"), [])

    def test_nested_virtualization_on_e2(self):
        settings = {**SETTINGS, "nested_virtualization": True, "sandboxed_machine_type": "e2-standard-8"}
        problems = preflight.cluster_problems("c", CONFIG, settings, None, True, "z")
        self.assertRegex(problems[0], "has nested virtualization, but its sandboxed pool is e2-standard-8")

    def test_a_version_the_channel_does_not_offer_where_the_cluster_goes(self):
        problems = preflight.cluster_problems(
            "c", CONFIG, SETTINGS, ["1.35.9-gke.100", "1.36.1-gke.200"], True, "us-central1-a"
        )
        self.assertRegex(problems[0], "does not offer in us-central1-a")
        self.assertIn("1.35.9-gke.100", problems[0])
        self.assertNotIn("1.36.1", problems[0])

    def test_a_minor_is_offered_when_a_build_of_it_is(self):
        offered = ["1.35.8-gke.1439001", "1.36.4-gke.1391002"]
        for version in ("1.35", "1.35.8", "1.35.8-gke.1439001"):
            settings = {**SETTINGS, "kubernetes_version": version}
            self.assertEqual(preflight.cluster_problems("c", CONFIG, settings, offered, True, "z"), [], version)
        settings = {**SETTINGS, "kubernetes_version": "1.3"}
        self.assertTrue(preflight.cluster_problems("c", CONFIG, settings, offered, True, "z"))

    def test_a_minor_no_longer_offered_names_the_minors_that_are(self):
        settings = {**SETTINGS, "kubernetes_version": "1.33"}
        problems = preflight.cluster_problems("c", CONFIG, settings, ["1.35.8-gke.1439001", "1.36.4-gke.1"], True, "z")
        self.assertIn("of 1.33 it offers none there, of all 1.35, 1.36", problems[0])

    def test_unknown_versions_are_not_held_against_it(self):
        self.assertEqual(preflight.cluster_problems("c", CONFIG, SETTINGS, None, True, "z"), [])
        self.assertEqual(preflight.cluster_problems("c", CONFIG, SETTINGS, [], False, "z"), [])

    def test_versions_are_read_where_the_cluster_goes(self):
        self.assertEqual(preflight.cluster_location(SETTINGS, "us-central1"), "us-central1-a")
        regional = {**SETTINGS, "regional_control_plane": True}
        self.assertEqual(preflight.cluster_location(regional, "us-central1"), "us-central1")
        self.assertEqual(preflight.cluster_location({}, "us-central1"), "us-central1")


class AppliedClusters(unittest.TestCase):
    def applied(self, record: dict | None, dev_infra: str | None = "dev", update: bool = False) -> list[str]:
        from argparse import Namespace
        from unittest import mock

        from scenario_runner.cli import run

        args = Namespace(dev_infra=dev_infra, dev_infra_update=update)
        with mock.patch.object(run.dev_infra, "read_record", return_value=record):
            return run.applied_clusters(args, {"a": {}, "b": {}})

    def test_own_infra_applies_every_cases_cluster(self):
        self.assertEqual(self.applied(None, dev_infra=None), ["a", "b"])

    def test_a_dev_infra_applies_the_first_cases_until_complete(self):
        self.assertEqual(self.applied(None), ["a"])
        self.assertEqual(self.applied({"complete": False}), ["a"])
        self.assertEqual(self.applied({"complete": True}, update=True), ["a"])

    def test_a_complete_dev_infra_applies_none(self):
        self.assertEqual(self.applied({"complete": True}), [])


class DevInfraConfiguration(unittest.TestCase):
    def prepare(self, applied: str, asked: str) -> str | None:
        """What a case asking for configuration asked fails with on a complete
        dev infra applied as applied; None if it runs on it."""
        from argparse import Namespace

        from scenario_runner.cli import run
        from scenario_runner.infra.cluster_configs import CLUSTER_CONFIGS, to_record

        args = Namespace(
            dev_infra="dev", dev_infra_update=False, project_id="p", region="r", zone=run.DEFAULT_ZONE,
            terraform_output="text",
        )  # fmt: skip
        record = {
            "project_id": "p",
            "region": "r",
            "cluster": to_record(CLUSTER_CONFIGS[applied]()),
            "bare_metal": False,
            "complete": True,
        }
        case = {"type": "noop", "bare-metal": False, "noop": {"infra": {"cluster": {"configuration": asked}}}}
        with mock.patch.object(run.dev_infra, "read_record", return_value=record), \
             mock.patch.object(run, "log") as log:  # fmt: skip
            try:
                run.prepare_dev_infra(args, {"c": case})
            except SystemExit:
                return log.call_args.args[0]
        return None

    def test_a_case_runs_on_its_configuration_or_one_that_has_more(self):
        self.assertIsNone(self.prepare("common", "common"))
        self.assertIsNone(self.prepare("openclaw", "openclaw"))
        self.assertIsNone(self.prepare("openclaw", "common"))

    def test_a_case_is_refused_one_without_what_it_asks_for(self):
        for applied, asked in (("common", "openclaw"), ("common", "dummy"), ("dummy", "common")):
            with self.subTest(applied=applied, asked=asked):
                self.assertIn(
                    f"case c runs on configuration {asked}, but dev infra dev is {applied}", self.prepare(applied, asked)
                )


class BareMetal(unittest.TestCase):
    PLACEMENT = [{"zone": "us-central1-a", "machine-type": "c3-standard-192-metal"}]

    def problems(self, cpus, quota, nodes=1):
        settings = {**SETTINGS, "sandboxed_node_count": nodes}
        with mock.patch.object(preflight, "machine_type_cpus", return_value=cpus), \
             mock.patch.object(preflight, "cpu_quota", return_value=quota), \
             mock.patch.object(preflight, "log") as log:
            return preflight.bare_metal_problems("c", settings, self.PLACEMENT, "p", "us-central1"), log

    def test_a_pool_that_fits_the_quota(self):
        problems, log = self.problems(192, (0.0, 300.0))
        self.assertEqual(problems, [])
        log.assert_not_called()

    def test_a_pool_beyond_the_quota(self):
        problems, _ = self.problems(192, (0.0, 300.0), nodes=2)
        self.assertRegex(problems[0], "needs 384 C3 vCPUs, but the quota in us-central1 is 300")

    def test_a_pool_beyond_what_is_free_now_is_only_warned_of(self):
        problems, log = self.problems(192, (200.0, 300.0))
        self.assertEqual(problems, [])
        self.assertIn("100 free now", log.call_args.args[0])

    def test_a_zone_that_does_not_offer_the_machine_type(self):
        problems, _ = self.problems("us-central1-a does not offer c3-standard-192-metal", None)
        self.assertRegex(problems[0], "does not offer")

    def test_what_cannot_be_read_is_not_held_against_it(self):
        problems, log = self.problems(None, None)
        self.assertEqual(problems, [])
        self.assertIn("not checking it", log.call_args.args[0])


class Quota(unittest.TestCase):
    # The nightly's 30 c2-standard-16 nodes in us-central1, and two empty regions.
    QUOTAS = {
        "asia-east1": {"C2_CPUS": (0.0, 500.0), "CPUS": (0.0, 600.0)},
        "us-central1": {"C2_CPUS": (496.0, 500.0), "CPUS": (12.0, 600.0)},
        "us-east1": {"C2_CPUS": (0.0, 500.0), "CPUS": (0.0, 600.0)},
        "us-east4": {"C2_CPUS": (0.0, 500.0), "CPUS": (0.0, 600.0)},
        "us-west1": {"C2_CPUS": (0.0, 24.0), "CPUS": (0.0, 600.0)},
    }

    def test_each_family_counts_against_its_quota(self):
        self.assertEqual(preflight.quota_metric("c2-standard-16"), "C2_CPUS")
        self.assertEqual(preflight.quota_metric("n2-standard-8"), "N2_CPUS")
        self.assertEqual(preflight.quota_metric("e2-standard-4"), "CPUS")

    def test_every_pool_of_the_cluster_counts(self):
        settings = {
            **SETTINGS,
            "default_machine_type": "c2-standard-16",
            "sandboxed_node_count": 2,
            "sandboxed_max_node_count": 3,
            "additional_node_pools": [{"name": "mock-llm", "machine_type": "e2-standard-8", "node_count": 1}],
        }
        pools = [("c2-standard-16", 1), ("e2-standard-4", 1), ("c2-standard-16", 3), ("e2-standard-8", 1)]
        self.assertEqual(preflight.cluster_pools(settings), pools)

    def test_a_cluster_that_fits_what_is_free(self):
        self.assertEqual(preflight.quota_problems("c", {"C2_CPUS": 32, "CPUS": 4}, "us-east1", self.QUOTAS), [])

    def test_a_cluster_the_quota_never_fits(self):
        problems = preflight.quota_problems("c", {"C2_CPUS": 32}, "us-west1", self.QUOTAS)
        self.assertRegex(problems[0], "needs 32 vCPUs of the C2_CPUS quota, but it is 24 in us-west1")

    def test_a_cluster_other_runs_leave_no_room_for_names_regions_with_room(self):
        problems = preflight.quota_problems("c", {"C2_CPUS": 32, "CPUS": 4}, "us-central1", self.QUOTAS)
        self.assertEqual(len(problems), 1)
        self.assertIn("32 vCPUs of C2_CPUS, of which 4 are free (496 of 500 in use)", problems[0])
        self.assertIn("such as us-east1, us-east4, asia-east1, with --region", problems[0])

    def test_no_region_with_room(self):
        quotas = {"us-central1": self.QUOTAS["us-central1"]}
        problems = preflight.quota_problems("c", {"C2_CPUS": 32}, "us-central1", quotas)
        self.assertIn("no region has room for it now", problems[0])

    def test_a_quota_that_cannot_be_read_is_not_held_against_it(self):
        with mock.patch.object(preflight, "log") as log:
            self.assertEqual(preflight.quota_problems("c", {"N2_CPUS": 8}, "us-east1", self.QUOTAS), [])
        self.assertIn("not checking it", log.call_args.args[0])


@unittest.skipUnless(shutil.which("terraform"), "needs terraform")
class ClusterSettings(unittest.TestCase):
    """Evaluated by terraform from the shipped common config, as the run would
    apply it."""

    def test_a_case_asks_for_nested_virtualization(self):
        settings = preflight.cluster_settings(CommonCluster(nested_virtualization=True), "my-project", "us-central1")
        self.assertIs(settings["nested_virtualization"], True)
        self.assertEqual(settings["sandboxed_machine_type"], "c2-standard-16")
        self.assertEqual(preflight.cluster_location(settings, "us-central1"), "us-central1-a")

    def test_the_cluster_goes_in_the_zone_given(self):
        settings = preflight.cluster_settings(CommonCluster(), "my-project", "us-east4", "us-east4-b")
        self.assertEqual(preflight.cluster_location(settings, "us-east4"), "us-east4-b")
        self.assertEqual(settings["default_machine_type"], "c2-standard-16")

    def test_the_pool_is_pinned(self):
        settings = preflight.cluster_settings(CommonCluster(sandboxed_nodes=3), "my-project", "us-central1")
        counts = [settings[f"sandboxed_{key}node_count"] for key in ("", "min_", "max_")]
        self.assertEqual(counts, [3, 3, 3])

    def test_the_config_takes_a_cloud_run_jobs_network(self):
        from unittest import mock

        from scenario_runner.infra import cluster_configs

        network = {"CLUSTER_NETWORK": "projects/p/global/networks/runs",
                   "CLUSTER_SUBNETWORK": "projects/p/regions/us-central1/subnetworks/runs"}  # fmt: skip
        with mock.patch.multiple(cluster_configs, **network):
            settings = preflight.cluster_settings(CommonCluster(), "my-project", "us-central1")
        self.assertIs(settings["nested_virtualization"], False)

    def test_a_metal_machine_type_runs_microvm(self):
        placement = {"zone": "us-central1-c", "machine-type": "c3-highcpu-192-metal"}
        config = CommonCluster(nested_virtualization=True).placed(placement)
        settings = preflight.cluster_settings(config, "my-project", "us-central1")
        self.assertEqual(settings["sandboxed_machine_type"], "c3-highcpu-192-metal")
        self.assertEqual(preflight.cluster_location(settings, "us-central1"), "us-central1-c")

    def test_the_config_refuses_nested_virtualization_off_intel(self):
        config = CommonCluster(sandboxed_machine_type="e2-standard-8", nested_virtualization=True)
        with self.assertRaisesRegex(ValueError, "Nested virtualization is offered only on Intel"):
            preflight.cluster_settings(config, "my-project", "us-central1")


class TfFiles(unittest.TestCase):
    def test_hidden_files_are_skipped_as_terraform_skips_them(self):
        import tempfile
        from pathlib import Path

        config = Path(tempfile.mkdtemp())
        (config / "main.tf").write_text('variable "a" {}\nresource "google_container_cluster" "c" {}\n')
        # What macOS tar adds beside each file: binary, and not UTF-8.
        (config / "._main.tf").write_bytes(b"\x00\x05\x16\x07\xa3\xff")
        self.assertEqual(preflight.declared_variables(config), {"a"})
        self.assertTrue(preflight.creates_gke_cluster(config))
