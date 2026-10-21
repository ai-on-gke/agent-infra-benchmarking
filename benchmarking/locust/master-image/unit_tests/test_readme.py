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

import readme

FAILURES = {"cycles": 200, "failed_cycles": 1, "failed_starts": 1, "ratio": 0.01, "exceptions": 0,
            "steady": {"cycles": 150, "failed_cycles": 0, "failed_starts": 0, "ratio": 0.0}}  # fmt: skip
ROWS = [
    {"Name": name, "Request Count": "200", "Failure Count": "0", "50%": "300", "90%": "400", "99%": "900",
     "Max Response Time": "1234.5"}
    for name in ("Resume", "TTFI", "Suspend", "CycleTime", "ExecutionTime")
]  # fmt: skip


class Render(unittest.TestCase):
    def info(self, workload: str = "glutton") -> dict:
        return {
            "run_id": "r", "test_case": "c", "status": 0, "failures": FAILURES, "max_failure_ratio": 0.01,
            "case": {"worker": {"workload-name": workload}},
        }  # fmt: skip

    def test_failed_cycles_as_the_gate_counts_them(self):
        text = readme.render(self.info(), ROWS, "locust_stats_steady.csv")
        self.assertIn("**Failed cycles:** 2 of 201 (1.000%), 1 of them actors not started, allowed 1%", text)
        self.assertIn("**Failed cycles, steady state:** 0 of 150", text)

    def test_results_in_a_cycles_order(self):
        text = readme.render(self.info(), ROWS, "locust_stats_steady.csv")
        order = [text.index(f"| `{name}`") for name in readme.RESULT_ROWS]
        self.assertEqual(order, sorted(order))
        self.assertIn("1234.5", text)

    def test_openclaw_is_compared_on_resume_and_suspend(self):
        self.assertIn("Compare sandboxes on `Resume` and `Suspend`", readme.render(self.info("openclaw"), ROWS))
        self.assertNotIn("Compare sandboxes", readme.render(self.info(), ROWS))

    def test_nothing_recorded_renders(self):
        text = readme.render({})
        self.assertIn("# -, run -", text)
        self.assertNotIn("## Results", text)

    def test_a_warmup_is_not_titled_as_a_repeat(self):
        text = readme.render({**self.info(), "repeat": None, "warmup": 1})
        self.assertIn("# c, warm-up 1, not one of its repeats, run r", text)

    def test_versions_name_what_ran_and_built_it(self):
        info = {
            "control_plane_images": {
                "Deployment/ate-api-server": {
                    "ate-api-server": {"image": "r:5000/p/ate-api-server:v1", "digests": ["sha256:1"]},
                },
                "DaemonSet/atelet": {"atelet": {"image": "sha256:9", "digests": ["sha256:2"]}},
            },
            "tools": {"terraform": "1.15.6", "docker": None},
            "builders": {"where": "cloud build", "docker": "d@sha256:3"},
        }  # fmt: skip
        text = readme.render(info)
        self.assertIn(
            "| Control plane `Deployment/ate-api-server` / ate-api-server | `r:5000/p/ate-api-server@sha256:1` |", text
        )
        self.assertIn("| Control plane `DaemonSet/atelet` / atelet | `sha256:2` |", text)
        self.assertIn("| Tools | terraform 1.15.6 |", text)
        self.assertIn("| Images built with | cloud build, `d@sha256:3` |", text)

    def test_no_cycles(self):
        failures = {"cycles": 0, "failed_cycles": 0, "failed_starts": 0, "ratio": None}
        self.assertEqual(readme._failures(failures, 0.01), "none; the run completed no cycle")


class Summary(unittest.TestCase):
    def repeat(self, cluster: str, taken: str, machine: str = "c2-standard-16") -> dict:
        return {
            "run_id": "r", "status": 0,
            "cluster": {"name": cluster, "location": "us-central1-a",
                        "node_pools": {"sandboxed": {"machine_type": machine}}},
            "actor_templates": {"glutton": {"sandboxConfig": {"configName": "gvisor-default"},
                                            "status": {"goldenSnapshotStatus": {"takeGoldenSnapshotAt": taken}}}},
        }  # fmt: skip

    def test_repeats_on_clusters_of_their_own_show_their_hardware_once(self):
        jobs = [("c-1", self.repeat("a", "t1"), None), ("c-2", self.repeat("b", "t2"), None)]
        text = readme.render_summary("c", jobs, [])
        self.assertIn("GKE clusters `a`, `b`, one per repeat, in us-central1-a", text)
        self.assertNotIn("Not the same", text)
        self.assertNotIn("golden snapshot taken", text)

    def test_runtime_assets_by_digest_not_by_the_runs_bucket(self):
        jobs = []
        for name, bucket in (("c-1", "b1"), ("c-2", "b2")):
            info = self.repeat(name, "t")
            info["bucket"] = bucket
            info["sandbox_configs"] = {"gvisor-default": {"defaultVersion": "v2", "versions": [
                {"name": "v1", "assets": {"amd64": {"gvisor": {"sha256": "old"}}}},
                {"name": "v2", "assets": {"amd64": {
                    "kernel": {"sha256": "k", "url": f"gs://{bucket}/kata-assets/vmlinux"},
                    "gvisor": {"sha256": "g", "url": "gs://gvisor/r.tar.zstd"},
                }}},
            ]}}  # fmt: skip
            jobs.append((name, info, None))
        text = readme.render_summary("c", jobs, [])
        self.assertIn("runtime `gvisor` (sha256 `g`, from `gs://gvisor/r.tar.zstd`), `kernel` (sha256 `k`)", text)
        self.assertNotIn("Not the same", text)

    def test_hardware_that_differs_shows_per_repeat(self):
        jobs = [("c-1", self.repeat("a", "t1"), None), ("c-2", self.repeat("b", "t2", "n2-standard-16"), None)]
        text = readme.render_summary("c", jobs, [])
        self.assertIn("Not the same in every repeat.", text)
        self.assertIn("GKE cluster `b`", text)

    def test_repeats_on_one_cluster_say_so(self):
        jobs = []
        for name in ("c-1", "c-2"):
            info = self.repeat("a", "t")
            info["case"] = {"repeat-cluster": "shared"}
            jobs.append((name, info, None))
        self.assertIn("# c, 2 repeats on one cluster, run r", readme.render_summary("c", jobs, []))
