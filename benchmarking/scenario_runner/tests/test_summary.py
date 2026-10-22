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

import json
import tempfile
import unittest
from pathlib import Path

from scenario_runner.reports import summary

STATS = """Type,Name,Request Count,Failure Count,Median Response Time,Average Response Time,Min Response Time,Max Response Time,Average Content Size,Requests/s,Failures/s,50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%
runworkload,TTFI,{n},0,300,310,200,900,0,9.5,0,300,310,320,330,360,400,500,{p99},900,900,900
,Aggregated,{n},0,300,310,200,900,0,9.5,0,300,310,320,330,360,400,500,{p99},900,900,900
"""


def repeat(run_dir: Path, name: str, status: int, n: int, p99: int) -> None:
    job = run_dir / name
    job.mkdir()
    (job / "locust_stats_steady.csv").write_text(STATS.format(n=n, p99=p99))
    failures = {"cycles": n, "failed_cycles": 1, "failed_starts": 0, "ratio": 1 / n, "exceptions": 0}
    (job / "run.json").write_text(json.dumps({"run_id": "r", "status": status, "failures": failures}))


class Summary(unittest.TestCase):
    def test_passed_repeats_are_summarized_and_failed_ones_left_out(self):
        run_dir = Path(tempfile.mkdtemp())
        repeat(run_dir, "c-1", 0, 100, 600)
        repeat(run_dir, "c-2", 0, 120, 800)
        repeat(run_dir, "c-3", 4, 90, 5000)
        summary.write(run_dir, "c", ["c-1", "c-2", "c-3"])

        rows = (run_dir / "c-summary.csv").read_text().splitlines()
        p99 = next(row for row in rows if row.startswith("runworkload,TTFI,99%,"))
        self.assertEqual(p99, "runworkload,TTFI,99%,2,600,700,800")
        readme = (run_dir / "c-summary.md").read_text()
        self.assertIn("2 of 3 repeats", readme)
        # Every repeat's failed cycles count toward the total, the failed one's too.
        self.assertIn("**Failed cycles, all repeats:** 3 of 310", readme)

    def test_no_repeat_left_reports(self):
        run_dir = Path(tempfile.mkdtemp())
        summary.write(run_dir / "missing", "c", ["c-1", "c-2"])
        self.assertFalse((run_dir / "missing").exists())
