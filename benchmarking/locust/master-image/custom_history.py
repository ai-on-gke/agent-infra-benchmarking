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
rate and percentile ones. cluster_vitals writes its file in the same shape.
"""

import csv
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus.benchmarking.svc.cluster.local:9090")
FILENAME = "custom_stats_history.csv"
COLUMNS = ["Timestamp", "User Count", "Type", "Name", "Value"]
# One row a second, as locust writes its own. Prometheus scrapes every 10s, so
# a reading repeats until the next scrape.
STEP_SECS = 1
# The run's last scrape lands up to one scrape interval after it ends, so the
# reports wait this long before they query.
SETTLE_SECS = 15
QUERY_TIMEOUT_SECS = 15

# (Type, Name, metric), each summed across the worker's pods.
GAUGES = (
    ("gauge", "ActiveActors", "boomer_active_actors"),
    # Idle is the worker's suspended gauge: actors waiting to be resumed.
    ("gauge", "IdleActors", "boomer_suspended_actors"),
)


def range_query(query: str, start: float, end: float) -> list[dict]:
    """The query's raw result series."""
    params = urllib.parse.urlencode({"query": query, "start": f"{start:.0f}", "end": f"{end:.0f}", "step": STEP_SECS})
    with urllib.request.urlopen(f"{PROMETHEUS_URL}/api/v1/query_range?{params}", timeout=QUERY_TIMEOUT_SECS) as resp:
        payload = json.load(resp)
    if payload.get("status") != "success":
        raise ValueError(f"{query}: {payload.get('error', 'query rejected')}")
    return payload["data"]["result"]


def samples(item: dict) -> dict[int, float]:
    """One series as {unix second: value}. NaN samples are dropped: they mean no
    observation, not zero."""
    return {int(float(ts)): float(value) for ts, value in item["values"] if value != "NaN"}


def series(result: list[dict]) -> dict[int, float]:
    """The first series of a result, which an aggregate without `by` has at most."""
    return samples(result[0]) if result else {}


def series_by(result: list[dict], label: str) -> dict[str, dict[int, float]]:
    """Every series, by its `label` value."""
    return {item["metric"].get(label, ""): samples(item) for item in result}


def user_counts(csv_prefix: Path) -> dict[int, str]:
    """locust's user count at each second, off its history's Aggregated rows;
    none if it cannot be read."""
    try:
        with Path(f"{csv_prefix}_stats_history.csv").open(newline="") as f:
            return {int(r["Timestamp"]): r["User Count"] for r in csv.DictReader(f) if r["Name"] == "Aggregated"}
    except (OSError, KeyError, ValueError):
        return {}


def write_history(
    path: Path,
    users: dict[int, str],
    values: dict[tuple[str, str], dict[int, float]],
    pools: dict[str, str] | None = None,
) -> Path:
    """Writes values, by (Type, Name), as rows ordered by timestamp and then by
    values' order. With pools, a trailing Pool column holds each Type's pool,
    empty for a Type not in it."""
    with path.open("w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(COLUMNS if pools is None else [*COLUMNS, "Pool"])
        for ts in sorted({ts for by_ts in values.values() for ts in by_ts}):
            for (kind, name), by_ts in values.items():
                if ts not in by_ts:
                    continue
                value = by_ts[ts]
                row = [ts, users.get(ts, ""), kind, name, f"{value:.0f}" if value.is_integer() else value]
                writer.writerow(row if pools is None else [*row, pools.get(kind, "")])
    return path


def write(csv_prefix: Path, run_id: str, test_case: str, started: float, ended: float) -> Path:
    """Writes the file beside locust's CSVs and returns its path."""
    labels = f'run_id="{run_id}",test_case="{test_case}"'
    values = {
        (kind, name): series(range_query(f"sum({metric}{{{labels}}})", started, ended)) for kind, name, metric in GAUGES
    }
    if not any(values.values()):
        raise ValueError(f"Prometheus has no actor gauges for run_id={run_id} test_case={test_case}")
    return write_history(csv_prefix.parent / FILENAME, user_counts(csv_prefix), values)
