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

"""Readers for a run's CSVs."""

from __future__ import annotations

import csv
from pathlib import Path

# locust's --csv-full-history: one row per second per request name, plus an
# Aggregated row.
STATS_HISTORY = "locust_stats_history.csv"

# runner.py's custom-history report, from Prometheus: the worker's actor gauges,
# one row per second per gauge.
CUSTOM_HISTORY = "custom_stats_history.csv"

# runner.py's cluster-vitals report, from Prometheus: one row per second per
# metric, for each node and for the cluster as a whole.
CLUSTER_VITALS = "cluster_vitals_history.csv"


def read(data_dir: Path, filename: str) -> list[dict[str, str]]:
    path = data_dir / filename
    if not path.is_file():
        raise FileNotFoundError(f"no {filename} in {data_dir}")
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def number(value: str | None) -> float | None:
    """A cell as a float, or None for locust's "N/A"."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
