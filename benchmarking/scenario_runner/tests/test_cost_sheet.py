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

"""The sandbox cost sheet: each sandbox priced at the size its case states."""

import unittest

from scenario_runner.benchmarks.sandbox_benchmark import cost_sheet as costs


def source(provider: str, options: dict) -> dict:
    case = {"sandbox-benchmark": {"provider": {"name": provider, **options}}}
    run = {"test": costs.TEST, "metrics": {"Suspend": [1000.0], "Resume": [2000.0]}, "errors": []}
    return {"provider": provider, "info": {"case": case}, "runs": [run]}


def cell(rows: list[list[str]], label: str, provider: str) -> str:
    column = 1 + list(costs.RESOURCE_PRICED).index(provider)
    return next(row for row in rows if row and row[0] == label)[column]


class Sizes(unittest.TestCase):
    def test_the_size_is_the_case_s(self):
        self.assertEqual(costs.size(source("substrate", {"actor-cpu": "1500m", "actor-memory": "512Mi"})), (1.5, 0.5))
        self.assertEqual(costs.size(source("modal", {"cpu": "2", "memory": "2Gi"})), (2.0, 2.0))
        self.assertIsNone(costs.size(source("e2b", {"cpu": None, "memory": None})))
        self.assertIsNone(costs.size({"provider": "e2b", "info": {}, "runs": []}))

    def test_each_sandbox_is_priced_at_its_size(self):
        rows = costs.sheet(
            [source("e2b", {"cpu": "2", "memory": "2Gi"}), source("daytona", {"cpu": "4", "memory": "8Gi"})]
        )
        self.assertEqual(cell(rows, "Sandbox Size", "e2b"), "2 vCPU, 2 GiB")
        self.assertEqual(cell(rows, "Sandbox Size", "daytona"), "4 vCPU, 8 GiB")
        _, vcpu_price, ram_price = costs.RESOURCE_PRICED["daytona"]
        billable = costs.ACTIVE_SECS_PER_HOUR + 3
        self.assertEqual(cell(rows, "vCPU Cost (per hour)", "daytona"), f"${billable * vcpu_price * 4:.7f}")
        self.assertEqual(cell(rows, "RAM Cost (per hour)", "daytona"), f"${billable * ram_price * 8:.7f}")

    def test_a_sandbox_of_no_stated_size_is_left_unpriced(self):
        rows = costs.sheet([source("e2b", {"cpu": None, "memory": None})])
        self.assertEqual(cell(rows, "Suspend Overhead (seconds per hour)", "e2b"), "1.00000")
        self.assertEqual(cell(rows, "Total Cost (per month)", "e2b"), "")

    def test_openai_shows_its_tier(self):
        rows = costs.sheet([source("openai", {"container-size": "large"})])
        openai = next(row for row in rows if row and row[0] == "Sandbox Size")[-1]
        self.assertEqual(openai, "large tier")


if __name__ == "__main__":
    unittest.main()
