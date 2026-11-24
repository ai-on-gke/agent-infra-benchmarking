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

from scenario_runner.benchmarks import computesdk


class CheckCase(unittest.TestCase):
    def valid_case(self, **overrides) -> dict:
        return {
            "type": "computesdk",
            "computesdk": {
                "benchmark": "burst-tti",
                "provider": "substrate",
                "iterations": 5,
                "concurrency": 3,
                "install": {"substrate": {"sandbox-class": "gvisor"}},
                **overrides,
            },
        }

    def test_valid_case_with_defaults(self):
        case = computesdk.check_case("test-case", self.valid_case())
        options = case["computesdk"]
        self.assertEqual(options["benchmark"], "burst-tti")
        self.assertEqual(options["provider"], "substrate")
        self.assertEqual(options["iterations"], 5)
        self.assertEqual(options["concurrency"], 3)
        self.assertEqual(options["actor-memory"], "2Gi")
        self.assertEqual(options["actor-cpu"], "2")

    def test_dax_default_memory_is_16gi(self):
        case = computesdk.check_case("test-dax", self.valid_case(benchmark="dax", iterations=1, concurrency=1))
        self.assertEqual(case["computesdk"]["actor-memory"], "16Gi")

    def test_unknown_benchmark_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be one of burst-tti"):
            computesdk.check_case("test-case", self.valid_case(benchmark="unknown"))

    def test_unknown_provider_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be one of substrate"):
            computesdk.check_case("test-case", self.valid_case(provider="unknown"))

    def test_substrate_requires_substrate_install(self):
        with self.assertRaisesRegex(ValueError, "substrate provider needs"):
            computesdk.check_case("test-case", {"type": "computesdk", "computesdk": {"benchmark": "burst-tti", "provider": "substrate"}})

    def test_invalid_iterations_rejected(self):
        with self.assertRaisesRegex(ValueError, "iterations.*whole number"):
            computesdk.check_case("test-case", self.valid_case(iterations=0))

    def test_invalid_concurrency_rejected(self):
        with self.assertRaisesRegex(ValueError, "concurrency.*whole number"):
            computesdk.check_case("test-case", self.valid_case(concurrency=-1))

    def test_report_folder(self):
        case = self.valid_case()
        folder = computesdk.report_folder("test-case", "test-case", case)
        self.assertEqual(folder, "test-case/substrate-burst-tti")
