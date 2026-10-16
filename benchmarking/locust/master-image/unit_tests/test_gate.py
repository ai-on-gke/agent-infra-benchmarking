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

"""What fails a run: failed cycles over max-failure-ratio."""

import tempfile
import unittest
from pathlib import Path

import run_master

HEADER = "Type,Name,Request Count,Failure Count\n"


def stats(path: Path, rows: dict[str, tuple[int, int]]) -> Path:
    lines = [f"x,{name},{count},{failed}" for name, (count, failed) in rows.items()]
    path.write_text(HEADER + "\n".join(lines) + "\n")
    return path


class CycleCounts(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_one_failed_resume_is_one_failed_cycle(self):
        # A failed Resume also fails TTFI and CycleTime: three failed rows, one failed cycle.
        counts = run_master.cycle_counts(stats(self.dir / "s.csv", {
            "Resume": (100, 1), "TTFI": (100, 1), "CycleTime": (100, 1), "Suspend": (99, 0), "Aggregated": (399, 3),
        }))  # fmt: skip
        self.assertEqual((counts["cycles"], counts["failed_cycles"], counts["ratio"]), (100, 1, 0.01))
        self.assertEqual(counts["failed_requests"], 3)

    def test_an_actor_not_started_counts_as_a_failed_cycle(self):
        counts = run_master.cycle_counts(stats(self.dir / "s.csv", {"CreateActor": (10, 2), "CycleTime": (98, 0)}))
        self.assertEqual(counts["failed_starts"], 2)
        self.assertEqual(counts["ratio"], 2 / 100)

    def test_no_cycles(self):
        counts = run_master.cycle_counts(stats(self.dir / "s.csv", {"CreateAtespace": (1, 0)}))
        self.assertIsNone(counts["ratio"])

    def test_count_failures_reads_the_steady_state_too(self):
        prefix = self.dir / "locust"
        stats(Path(f"{prefix}_stats.csv"), {"CycleTime": (100, 2)})
        stats(Path(f"{prefix}_stats_steady.csv"), {"CycleTime": (80, 0)})
        Path(f"{prefix}_exceptions.csv").write_text("Count,Message,Traceback,Nodes\n")
        counts = run_master.count_failures(prefix)
        self.assertEqual((counts["failed_cycles"], counts["exceptions"]), (2, 0))
        self.assertEqual(counts["steady"]["cycles"], 80)

    def test_unreadable_reports_fail_the_gate(self):
        self.assertIsNone(run_master.count_failures(self.dir / "missing"))
        self.assertFalse(run_master.within_limits(None, 0.01))


class Limits(unittest.TestCase):
    def counts(self, cycles: int, failed: int, exceptions: int = 0) -> dict:
        return {"cycles": cycles, "failed_cycles": failed, "failed_starts": 0,
                "ratio": failed / cycles if cycles else None, "exceptions": exceptions}  # fmt: skip

    def test_within_and_over(self):
        self.assertTrue(run_master.within_limits(self.counts(100, 1), 0.01))
        self.assertFalse(run_master.within_limits(self.counts(100, 2), 0.01))

    def test_no_cycle_or_an_exception_fails(self):
        self.assertFalse(run_master.within_limits(self.counts(0, 0), 0.5))
        self.assertFalse(run_master.within_limits(self.counts(100, 0, exceptions=1), 0.5))


class RunJson(unittest.TestCase):
    def test_run_json_records_the_run_and_what_the_runner_passed(self):
        import base64
        import json
        import os
        from unittest import mock

        prefix = Path(tempfile.mkdtemp()) / "locust"
        stats(Path(f"{prefix}_stats_steady.csv"), {"CycleTime": (80, 0)})
        Path(f"{prefix}_steady_window.json").write_text(
            json.dumps({"warm_up_secs": 10, "start": "2026-10-13T00:00:10+00:00", "end": "2026-10-13T00:01:00+00:00"})
        )
        passed = {"case": {"worker": {"workload-name": "glutton"}}, "commit": "abc"}
        env = {
            "BENCHMARK_RUN_INFO": base64.b64encode(json.dumps(passed).encode()).decode(),
            "BENCHMARK_RUN_ID": "r",
            "BENCHMARK_CASE": "glutton",
            "BENCHMARK_TEST_CASE": "glutton-2",
            "BENCHMARK_REPEAT": "2",
            "MAX_FAILURE_RATIO": "0.01",
        }
        failures = {"cycles": 80, "failed_cycles": 0, "failed_starts": 0, "ratio": 0.0, "exceptions": 0}
        with mock.patch.dict(os.environ, env):
            run_master.write_run_json(prefix, 0.0, 60.0, 0, failures)
        info = json.loads((prefix.parent / "run.json").read_text())
        self.assertEqual((info["test_case"], info["record"], info["repeat"]), ("glutton", "glutton-2", 2))
        self.assertEqual(info["warm_up"], "10s")
        self.assertEqual(info["commit"], "abc")
        readme_text = (prefix.parent / "README.md").read_text()
        self.assertIn("From `locust_stats_steady.csv`", readme_text)
