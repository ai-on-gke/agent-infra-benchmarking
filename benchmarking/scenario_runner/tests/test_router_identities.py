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

"""What substrate's router lets through and a dev infra keeps of it: the
identities besides the runner's that an install is given, for the case types
whose service dials the router."""

import unittest
from unittest import mock

from scenario_runner.control_planes import substrate
from scenario_runner.infra import dev_infra


class SubstrateInstall(unittest.TestCase):
    """The router's allow-list is what substrate is installed with."""

    def installed(self, *ids: str) -> str:
        cluster = mock.Mock(runner_ksa="runner")
        with mock.patch.object(substrate, "install_env", return_value={}), \
             mock.patch.object(substrate, "mirror_gvisor_assets"), \
             mock.patch.object(substrate, "stream") as stream:  # fmt: skip
            substrate.install_once(cluster, False, ids)
        args = stream.call_args.args[0]
        return next(a for a in args if a.startswith("--ingress-allowed-spiffe-ids=")).split("=", 1)[1]

    def test_the_runner_alone_by_default(self):
        self.assertEqual(self.installed(), "spiffe://cluster.local/ns/benchmarking/sa/runner")

    def test_other_identities_are_added_comma_separated(self):
        self.assertEqual(
            self.installed("spiffe://cluster.local/ns/ate-env/sa/ate-env-api", "spiffe://x/y"),
            "spiffe://cluster.local/ns/benchmarking/sa/runner,spiffe://cluster.local/ns/ate-env/sa/ate-env-api,spiffe://x/y",
        )

    def test_the_bootstrap_owners_stay_the_runners(self):
        cluster = mock.Mock(runner_ksa="runner")
        with mock.patch.object(substrate, "install_env", return_value={}), \
             mock.patch.object(substrate, "mirror_gvisor_assets"), \
             mock.patch.object(substrate, "stream") as stream:  # fmt: skip
            substrate.install_once(cluster, False, ["spiffe://x/y"])
        owners = next(a for a in stream.call_args.args[0] if a.startswith("--ateapi-authz-bootstrap-owners="))
        self.assertNotIn("spiffe://x/y", owners)


class DevInfra(unittest.TestCase):
    """A dev infra keeps what it was installed with, and widens it when a
    case needs more."""

    API = "spiffe://cluster.local/ns/ate-env/sa/ate-env-api"

    def install(self, record: dict, microvm: bool = False, ids: tuple = ()):
        with mock.patch.object(dev_infra, "substrate") as sub, \
             mock.patch.object(dev_infra, "cluster", return_value="the cluster"), \
             mock.patch.object(dev_infra, "write_record") as write:  # fmt: skip
            result = dev_infra.install_substrate(record, microvm, ids)
        return result, sub, write

    def test_a_first_install_has_the_identities_and_records_them(self):
        record, sub, write = self.install({"name": "d"}, False, (self.API,))
        sub.install.assert_called_once_with("the cluster", False, [self.API])
        self.assertEqual(record["ingress_ids"], [self.API])
        self.assertIs(record["substrate_installed"], True)
        write.assert_called_once_with(record)

    def test_an_install_without_identities_is_what_it_was(self):
        record, sub, _ = self.install({"name": "d"})
        sub.install.assert_called_once_with("the cluster", False, [])
        self.assertEqual(record["ingress_ids"], [])

    def test_an_installed_infra_with_them_already_is_left_alone(self):
        installed = {"name": "d", "substrate_installed": True, "ingress_ids": [self.API]}
        record, sub, write = self.install(installed, False, (self.API,))
        self.assertIs(record, installed)
        sub.install.assert_not_called()
        write.assert_not_called()

    def test_an_infra_installed_without_one_installs_again_with_it(self):
        installed = {"name": "d", "substrate_installed": True, "microvm_installed": True}
        record, sub, write = self.install(installed, False, (self.API,))
        # Its microvm class stays, whatever the case that widened the list asked.
        sub.install.assert_called_once_with("the cluster", True, [self.API])
        self.assertEqual((record["ingress_ids"], record["microvm_installed"]), ([self.API], True))
        write.assert_called_once()

    def test_the_identities_it_has_are_kept_when_a_case_names_others(self):
        installed = {"name": "d", "substrate_installed": True, "ingress_ids": ["spiffe://a/b"]}
        record, sub, _ = self.install(installed, False, (self.API,))
        sub.install.assert_called_once_with("the cluster", False, sorted(["spiffe://a/b", self.API]))

    def test_microvm_alone_is_still_added_without_installing_again(self):
        installed = {"name": "d", "substrate_installed": True, "ingress_ids": []}
        record, sub, _ = self.install(installed, True)
        sub.install.assert_not_called()
        sub.install_microvm.assert_called_once_with("the cluster")
        self.assertIs(record["microvm_installed"], True)


if __name__ == "__main__":
    unittest.main()
