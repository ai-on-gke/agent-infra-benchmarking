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

"""The BigQuery views over the reports (automation-infra's
terraform/modules/cloud-run/reports.tf) take each CSV's columns by position.
These tests fail when the locust pinned in requirements.txt, or the master's
own reports, change the columns those views were written for."""

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from locust.stats import PERCENTILES_TO_REPORT, RequestStats, StatsCSV, StatsCSVFileWriter

import cluster_vitals
import custom_history

REPORTS_TF = Path(__file__).resolve().parents[4] / "automation-infra/terraform/modules/cloud-run/reports.tf"

# The views' column names, in the order of each file's header.
STATS = ["type", "name", "request_count", "failure_count", "median_ms", "avg_ms", "min_ms", "max_ms",
         "avg_content_size", "requests_per_sec", "failures_per_sec"]  # fmt: skip
PERCENTILES = [f"p{p}_ms" for p in ("50", "66", "75", "80", "90", "95", "98", "99", "99_9", "99_99", "100")]
HISTORY = ["unix_secs", "user_count", "type", "name"]
STATS_HISTORY = [*HISTORY, "requests_per_sec", "failures_per_sec", *PERCENTILES, "total_request_count",
                 "total_failure_count", "total_median_ms", "total_avg_ms", "total_min_ms", "total_max_ms",
                 "total_avg_content_size"]  # fmt: skip


class ReportColumns(unittest.TestCase):
    def test_reports_tf_names_these_columns(self):
        text = REPORTS_TF.read_text()
        for name in (*STATS, *HISTORY, "value", "pool", "total_avg_content_size"):
            self.assertIn(f'"{name}"', text)
        percentiles = re.search(r'for p in \[([^\]]*)\]', text)[1]
        self.assertEqual([f"p{p}_ms" for p in re.findall(r'"([^"]+)"', percentiles)], PERCENTILES)

    def test_locust_stats_header(self):
        header = StatsCSV(SimpleNamespace(stats=RequestStats()), PERCENTILES_TO_REPORT).requests_csv_columns
        self.assertEqual(len(header), len(STATS) + len(PERCENTILES))
        self.assertEqual(header[: len(STATS)], ["Type", "Name", "Request Count", "Failure Count", "Median Response Time",
                         "Average Response Time", "Min Response Time", "Max Response Time", "Average Content Size",
                         "Requests/s", "Failures/s"])  # fmt: skip

    def test_locust_stats_history_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = SimpleNamespace(stats=RequestStats(), runner=None, parsed_options=None)
            writer = StatsCSVFileWriter(env, PERCENTILES_TO_REPORT, f"{tmp}/locust")
            header = writer.stats_history_csv_columns
            for handle in (writer.requests_csv_filehandle, writer.stats_history_csv_filehandle,
                           writer.failures_csv_filehandle, writer.exceptions_csv_filehandle):  # fmt: skip
                handle.close()
        self.assertEqual(len(header), len(STATS_HISTORY))
        self.assertEqual(header[:4], ["Timestamp", "User Count", "Type", "Name"])

    def test_the_masters_own_histories(self):
        self.assertEqual(custom_history.COLUMNS, ["Timestamp", "User Count", "Type", "Name", "Value"])
        self.assertEqual(len(custom_history.COLUMNS), len(HISTORY) + 1)
        # cluster_vitals writes the same columns, with Pool after them.
        self.assertIs(cluster_vitals.write_history, custom_history.write_history)
