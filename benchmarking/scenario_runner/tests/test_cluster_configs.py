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

"""The infra/clusters/ configs as the scenario-runner asks for them: what a
case may give, and the Terraform variables it comes to."""

import json
import tempfile
import unittest
from pathlib import Path

from scenario_runner.infra.cluster_configs import (
    CLUSTER_CONFIGS,
    CommonCluster,
    DummyCluster,
    Execution,
    OpenclawCluster,
    from_record,
    to_record,
)
from scenario_runner.infra.deployment import VARIABLES_FILE, Deployment, declared_variables, tf_vars

NAMED = {
    **tf_vars("p", "us-central1", "run-x-12345678"),
    "short_resource_name": "run-12345678",
    "bucket_name": "p-run-x-12345678",
    "labels": {"benchmark-run-id": "run-x"},
}
# Everything the execution may decide.
EXECUTION = Execution(
    NAMED,
    image_repository="projects/p/locations/us-central1/repositories/r",
    substrate_version="v1",
    node_service_account="nodes@p.iam.gserviceaccount.com",
    network={"network": "n", "subnetwork": "s"},
)
# Every input.
EVERYTHING = {
    "sandboxed_nodes": 2,
    "sandboxed_max_nodes": 3,
    "sandboxed_machine_type": "n2-standard-16",
    "zone": "us-central1-c",
    "nested_virtualization": True,
    "gke_sandbox": True,
}


class Variables(unittest.TestCase):
    def test_every_variable_is_one_the_config_declares(self):
        for config in (OpenclawCluster(**EVERYTHING), DummyCluster(**EVERYTHING)):
            with self.subTest(config=type(config).__name__):
                undeclared = config.variables(EXECUTION).keys() - declared_variables(config.CONFIG)
                self.assertFalse(undeclared, f"{config.CONFIG} declares none of {sorted(undeclared)}")

    def test_the_sandboxed_pool_is_pinned_unless_it_autoscales(self):
        pinned = CommonCluster(sandboxed_nodes=2).variables(EXECUTION)
        self.assertEqual([pinned[f"sandboxed_{key}node_count"] for key in ("", "min_", "max_")], [2, 2, 2])
        scaling = CommonCluster(sandboxed_nodes=2, sandboxed_max_nodes=5).variables(EXECUTION)
        self.assertEqual([scaling[f"sandboxed_{key}node_count"] for key in ("", "min_", "max_")], [2, 2, 5])

    def test_the_sandboxed_pool_has_kvm_nested_or_on_bare_metal(self):
        self.assertFalse(CommonCluster().sandboxed_kvm)
        self.assertTrue(CommonCluster(nested_virtualization=True).sandboxed_kvm)
        placement = {"zone": "us-central1-c", "machine-type": "c3-highcpu-192-metal"}
        self.assertTrue(CommonCluster().placed(placement).sandboxed_kvm)

    def test_common_has_default_tfvars_defaults(self):
        variables = CommonCluster().variables(Execution(NAMED))
        self.assertEqual(variables["sandboxed_machine_type"], "c2-standard-16")
        self.assertIs(variables["bucket_force_destroy"], True)
        self.assertIs(variables["nested_virtualization"], False)
        self.assertIs(variables["gke_sandbox"], False)
        # The config's own default, and none of what the execution left out.
        for name in ("zone", "additional_node_pools", "image_repository", "substrate_version",
                     "node_service_account", "existing_network", "enable_private_endpoint"):  # fmt: skip
            self.assertNotIn(name, variables)

    def test_openclaw_is_common_with_a_pool_for_mock_llm(self):
        variables = OpenclawCluster(sandboxed_nodes=2).variables(EXECUTION)
        pools = variables.pop("additional_node_pools")
        self.assertEqual([pool["labels"] for pool in pools], [{"benchmarking-mock-llm": "true"}])
        self.assertEqual(variables, CommonCluster(sandboxed_nodes=2).variables(EXECUTION))

    def test_a_cloud_run_job_puts_its_clusters_in_its_network_with_an_internal_endpoint(self):
        variables = CommonCluster().variables(EXECUTION)
        self.assertEqual(variables["existing_network"], {"network": "n", "subnetwork": "s"})
        self.assertIs(variables["enable_private_endpoint"], True)
        self.assertEqual(variables["node_service_account"], "nodes@p.iam.gserviceaccount.com")

    def test_the_execution_names_the_cluster(self):
        variables = DummyCluster().variables(EXECUTION)
        self.assertEqual({key: variables[key] for key in NAMED}, NAMED)

    def test_the_deployment_is_applied_with_them(self):
        config = CommonCluster(sandboxed_nodes=2)
        created = config.deployment(Deployment(Path(tempfile.mkdtemp())), EXECUTION)
        self.assertTrue((created.config / "main.tf").is_file())
        written = json.loads((created.config / VARIABLES_FILE).read_text())
        self.assertEqual(written, config.variables(EXECUTION))


class FromYaml(unittest.TestCase):
    def test_kebab_case_inputs_are_the_fields(self):
        given = {"sandboxed-nodes": 2, "sandboxed-max-nodes": 4, "sandboxed-machine-type": "n2-standard-16",
                 "zone": "us-central1-c", "nested-virtualization": True, "gke-sandbox": True,
                 "sandboxed-disk-size-gb": 500}  # fmt: skip
        self.assertEqual(
            CommonCluster.from_yaml(given), CommonCluster(2, 4, "n2-standard-16", "us-central1-c", True, True, 500)
        )

    def test_none_is_every_default(self):
        self.assertEqual(CommonCluster.from_yaml(None), CommonCluster())

    def test_only_its_inputs_are_taken(self):
        for key in ("sandbox-class", "mock-llm-pool", "sandboxed_nodes", "sandboxed_node_count", "name"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, f"unknown {key};.*sandboxed-nodes"):
                CommonCluster.from_yaml({key: 1})

    def test_values_are_checked(self):
        for given, message in [
            ({"sandboxed-nodes": 0}, "`sandboxed-nodes` must be a whole number from 1"),
            ({"sandboxed-nodes": True}, "`sandboxed-nodes` must be a whole number from 1"),
            ({"sandboxed-nodes": "2"}, "`sandboxed-nodes` must be a whole number from 1"),
            ({"sandboxed-nodes": 3, "sandboxed-max-nodes": 2}, "`sandboxed-max-nodes` 2 is less than"),
            ({"sandboxed-machine-type": ""}, "`sandboxed-machine-type` must be a non-empty string"),
            ({"zone": 1}, "`zone` must be a non-empty string"),
            ({"nested-virtualization": "yes"}, "`nested-virtualization` must be true or false"),
            ({"gke-sandbox": 1}, "`gke-sandbox` must be true or false"),
        ]:
            with self.subTest(given=given), self.assertRaisesRegex(ValueError, message):
                DummyCluster.from_yaml(given)

    def test_a_mapping_is_required(self):
        with self.assertRaisesRegex(ValueError, "must be a mapping"):
            CommonCluster.from_yaml([1])


class Sharing(unittest.TestCase):
    def test_clusters_asked_for_alike_are_equal(self):
        self.assertEqual(CommonCluster.from_yaml({"sandboxed-nodes": 1}), CommonCluster())
        self.assertEqual(len({CommonCluster(), CommonCluster(sandboxed_nodes=1)}), 1)
        self.assertNotEqual(CommonCluster(), OpenclawCluster())
        self.assertNotEqual(CommonCluster(), DummyCluster())

    def test_a_bare_metal_placement_moves_the_sandboxed_pool(self):
        placed = CommonCluster(sandboxed_nodes=2).placed(
            {"zone": "us-central1-c", "machine-type": "c3-standard-192-metal"}
        )
        self.assertEqual(placed, CommonCluster(2, None, "c3-standard-192-metal", "us-central1-c"))


class Configs(unittest.TestCase):
    def test_every_config_has_a_configuration(self):
        configs = {path.name for path in CommonCluster.CONFIG.parent.iterdir() if path.is_dir()}
        self.assertEqual(configs, {config.CONFIG.name for config in CLUSTER_CONFIGS.values()})
        for name, config in CLUSTER_CONFIGS.items():
            self.assertEqual(config.CONFIGURATION, name)

    def test_a_record_is_read_back_as_its_configuration(self):
        for config in (CommonCluster(sandboxed_nodes=2), OpenclawCluster(nested_virtualization=True), DummyCluster()):
            with self.subTest(config=type(config).__name__):
                self.assertEqual(from_record(to_record(config)), config)
        self.assertEqual(to_record(OpenclawCluster())["name"], "common")

    def test_a_record_of_a_class_has_nested_virtualization_for_microvm(self):
        record = {**to_record(CommonCluster()), "sandbox_class": "microvm"}
        del record["nested_virtualization"]
        self.assertEqual(from_record(record), CommonCluster(nested_virtualization=True))

    def test_a_record_from_before_configurations_is_refused(self):
        record = to_record(CommonCluster())
        del record["configuration"]
        with self.assertRaisesRegex(ValueError, "not a cluster of"):
            from_record(record)


if __name__ == "__main__":
    unittest.main()
