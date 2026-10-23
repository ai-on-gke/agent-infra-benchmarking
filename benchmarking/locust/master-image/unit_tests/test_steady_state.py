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

"""The steady-state window the master cuts out of the worker reports."""

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from locust.stats import RequestStats

_spec = importlib.util.spec_from_file_location("_master", Path(__file__).resolve().parent.parent / "tests" / "_master.py")
master = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(master)


class Hook:
    def __init__(self):
        self.listeners = []

    def add_listener(self, listener):
        self.listeners.append(listener)


def report(n: int, latency_ms: int) -> dict:
    """What a worker reports: n requests of one name since its last report."""
    stats = RequestStats(use_response_times_cache=False)
    for _ in range(n):
        stats.log_request("runworkload", "TTFI", latency_ms, 0)
    return {"stats": stats.serialize_stats(), "stats_total": stats.total.get_stripped_report()}


class SteadyWindow(unittest.TestCase):
    def setUp(self):
        self.prefix = str(Path(tempfile.mkdtemp()) / "locust")
        events = SimpleNamespace(spawning_complete=Hook(), worker_report=Hook(), test_stopping=Hook(), quitting=Hook())
        self.now = 1000.0
        patcher = mock.patch.object(master.time, "time", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.steady = master.SteadyState(SimpleNamespace(events=events), warm_up=10, csv_prefix=self.prefix)

    def at(self, t: float, client: str, n: int, latency_ms: int) -> None:
        self.now = t
        self.steady.on_worker_report(client, report(n, latency_ms))

    def rows(self) -> dict:
        with open(f"{self.prefix}_stats_steady.csv", newline="") as f:
            return {row["Name"]: row for row in csv.DictReader(f)}

    def test_only_the_window_is_counted(self):
        self.steady.on_spawning_complete()  # at 1000, so the window opens at 1010
        self.at(1005, "w1", 50, 5000)  # warm-up: left out
        self.at(1011, "w1", 30, 5000)  # the first report past it opens the window, partly before it: left out
        self.at(1014, "w1", 30, 100)
        self.at(1017, "w1", 30, 100)
        self.steady.on_test_stopping()
        self.at(1020, "w1", 30, 9000)  # the teardown: left out
        self.steady.on_quitting()
        rows = self.rows()
        self.assertEqual(rows["TTFI"]["Request Count"], "60")
        self.assertEqual(rows["TTFI"]["Max Response Time"], "100")
        window = json.loads(Path(f"{self.prefix}_steady_window.json").read_text())
        self.assertEqual(window["warm_up_secs"], 10)
        self.assertTrue(window["start"].startswith("1970-01-01T00:16:51"))

    def test_each_worker_pod_opens_its_own_part_of_the_window(self):
        self.steady.on_spawning_complete()
        self.at(1011, "w1", 30, 5000)
        self.at(1012, "w2", 30, 5000)  # w2's first report past the warm-up is left out too
        self.at(1014, "w1", 10, 100)
        self.at(1015, "w2", 20, 100)
        self.steady.on_quitting()
        self.assertEqual(self.rows()["TTFI"]["Request Count"], "30")

    def test_a_run_that_ends_in_its_warm_up_has_no_window(self):
        self.steady.on_spawning_complete()
        self.at(1005, "w1", 50, 100)
        self.steady.on_quitting()
        self.assertFalse(Path(f"{self.prefix}_stats_steady.csv").exists())
