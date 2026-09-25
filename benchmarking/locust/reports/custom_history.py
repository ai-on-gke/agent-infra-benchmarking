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

"""Metrics outside locust's own stats, as a CSV shaped like its
`_stats_history.csv`.

The leading columns are locust's (Timestamp, User Count, Type, Name), with one
row per metric per sample where locust has one per request name, so the two
files line up on Timestamp. A gauge is a single reading rather than a
distribution, so the rate and percentile columns give way to one Value.
"""

import csv
import io

from .base import RunContext
from .prometheus import range_query, series

# One row a second, as locust writes its own. Prometheus scrapes every 10s, so
# a reading repeats until the next scrape.
STEP_SECS = 1

COLUMNS = ["Timestamp", "User Count", "Type", "Name", "Value"]

USERS_QUERY = 'sum(locust_users{{run_id="{run_id}"}})'

# (Type, Name, query). Adding a metric to the file is adding a row here.
METRICS = (
    ("gauge", "ActiveActors", 'sum(boomer_active_actors{{run_id="{run_id}"}})'),
    # Idle is the worker's suspended gauge: actors waiting to be resumed.
    ("gauge", "IdleActors", 'sum(boomer_suspended_actors{{run_id="{run_id}"}})'),
)


class CustomHistoryReport:
    name = "custom-history"
    filename = "custom_stats_history.csv"

    def render(self, ctx: RunContext) -> str:
        users = self._query(ctx, USERS_QUERY)
        samples = {(kind, name): self._query(ctx, query) for kind, name, query in METRICS}
        if not any(samples.values()):
            raise ValueError(f"no custom metrics for run_id={ctx.run_id}")
        return history_csv(users, samples)

    def _query(self, ctx: RunContext, query: str) -> dict[int, float]:
        return series(
            range_query(
                ctx.prometheus_url,
                query.format(run_id=ctx.run_id),
                ctx.started,
                ctx.ended,
                STEP_SECS,
            )
        )


def history_csv(users: dict[int, float], samples: dict[tuple[str, str], dict[int, float]]) -> str:
    """`samples`, keyed by (Type, Name), as rows ordered by timestamp and then
    by `samples` order. Shared with cluster_vitals so both files keep one
    shape."""
    timestamps = sorted({ts for values in samples.values() for ts in values})

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    for ts in timestamps:
        user_count = users.get(ts)
        for (kind, name), values in samples.items():
            if ts not in values:
                continue
            writer.writerow(
                [
                    ts,
                    "" if user_count is None else f"{user_count:.0f}",
                    kind,
                    name,
                    _value(values[ts]),
                ]
            )
    return out.getvalue()


def _value(value: float) -> str:
    return f"{value:.0f}" if value.is_integer() else f"{value:f}"
