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

"""The worker's actor gauges over the run, read back from Prometheus into
custom_stats_history.csv, which benchmark-charts draws its actors panel from.

The leading columns are locust's _stats_history.csv's (Timestamp, User Count,
Type, Name), with one row per gauge per second, so the two files line up on
Timestamp. A gauge is a single reading, so a Value column stands in for the
rate and percentile ones.
"""

import csv
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus.benchmarking.svc.cluster.local:9090")
FILENAME = "custom_stats_history.csv"
COLUMNS = ["Timestamp", "User Count", "Type", "Name", "Value"]
# One row a second, as locust writes its own. Prometheus scrapes every 10s, so
# a reading repeats until the next scrape.
STEP_SECS = 1
# The run's last scrape lands up to one scrape interval after it ends.
SETTLE_SECS = 15
QUERY_TIMEOUT_SECS = 15

# (Type, Name, metric), each summed across the worker's pods.
GAUGES = (
    ("gauge", "ActiveActors", "boomer_active_actors"),
    # Idle is the worker's suspended gauge: actors waiting to be resumed.
    ("gauge", "IdleActors", "boomer_suspended_actors"),
)


def range_query(query: str, start: float, end: float) -> dict[int, float]:
    """The query's one series as {unix second: value}. NaN samples are dropped:
    they mean no observation, not zero."""
    params = urllib.parse.urlencode({"query": query, "start": f"{start:.0f}", "end": f"{end:.0f}", "step": STEP_SECS})
    with urllib.request.urlopen(f"{PROMETHEUS_URL}/api/v1/query_range?{params}", timeout=QUERY_TIMEOUT_SECS) as resp:
        payload = json.load(resp)
    if payload.get("status") != "success":
        raise ValueError(f"{query}: {payload.get('error', 'query rejected')}")
    result = payload["data"]["result"]
    return {int(float(ts)): float(value) for ts, value in result[0]["values"] if value != "NaN"} if result else {}


def user_counts(stats_history: Path) -> dict[int, str]:
    """locust's user count at each second, off its Aggregated rows."""
    with stats_history.open(newline="") as f:
        return {int(row["Timestamp"]): row["User Count"] for row in csv.DictReader(f) if row["Name"] == "Aggregated"}


def write(csv_prefix: Path, run_id: str, test_case: str, started: float) -> Path:
    """Writes the file beside locust's CSVs for the run since started, once its
    last scrape is in, and returns its path."""
    time.sleep(SETTLE_SECS)
    ended = time.time()
    labels = f'run_id="{run_id}",test_case="{test_case}"'
    samples = {name: range_query(f"sum({metric}{{{labels}}})", started, ended) for _, name, metric in GAUGES}
    if not any(samples.values()):
        raise ValueError(f"Prometheus has no actor gauges for run_id={run_id} test_case={test_case}")
    try:
        users = user_counts(Path(f"{csv_prefix}_stats_history.csv"))
    except (OSError, KeyError, ValueError):
        users = {}

    path = csv_prefix.parent / FILENAME
    with path.open("w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(COLUMNS)
        for ts in sorted({ts for values in samples.values() for ts in values}):
            for kind, name, _ in GAUGES:
                if ts in samples[name]:
                    value = samples[name][ts]
                    writer.writerow([ts, users.get(ts, ""), kind, name, f"{value:.0f}" if value.is_integer() else value])
    return path
