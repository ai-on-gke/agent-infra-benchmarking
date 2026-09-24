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

"""The run's stats as a CSV shaped like locust's own.

One row per request, columns in the order locust writes `_stats.csv` in, so
this opens beside an export from a locust run and diffs against one. The rows
are the worker's request names (TTFI, FirstResume, Instruction, ...) rather
than URLs, because that is what this benchmark measures.

Four columns are present for the shape and left empty, rather than filled with
a number that would read as a measurement:

  * Min / Max response time, and 100% — the worker reports latency as a
    histogram, so the smallest and largest observations are not recovered. The
    nearest available values are bucket bounds, which describe the bucket
    layout (ExponentialBuckets(1, 2, 16)) and not the run.
  * Average Content Size — response bodies are never weighed.

The percentile and average columns cover successful requests only. A failure
is usually a timeout, and folding one into the distribution moves the
percentiles by the size of the timeout rather than by anything the sandbox
did. The count columns cover every request, so the two are read together.
"""

import csv
import io

from .base import RunContext
from .prometheus import instant_query, labelled

# locust's own `_stats.csv` percentile set. 100 is in the header and never
# queried: it is the maximum, which a histogram cannot give — see above.
PERCENTILES = ("50", "66", "75", "80", "90", "95", "98", "99", "99.9", "99.99", "100")
QUERIED = tuple(p for p in PERCENTILES if p != "100")

# Header, in locust's column order.
COLUMNS = [
    "Type",
    "Name",
    "Request Count",
    "Failure Count",
    "Median Response Time",
    "Average Response Time",
    "Min Response Time",
    "Max Response Time",
    "Average Content Size",
    "Requests/s",
    "Failures/s",
    *[f"{p}%" for p in PERCENTILES],
]

# Margin on the lookback so the window reaches the run's first scrape. The
# counters are cumulative and the pod was new, so last_over_time takes each
# series' final sample, which is the run's own total.
WINDOW_MARGIN_SECS = 300

COUNT_QUERY = (
    'sum by (method,name,status) (last_over_time('
    'locust_requests_total{{run_id="{run_id}"}}[{window}s]))'
)
SUM_QUERY = (
    "sum by (method,name) (last_over_time("
    'locust_request_duration_milliseconds_sum{{run_id="{run_id}",status="success"}}[{window}s]))'
)
OBSERVATIONS_QUERY = (
    "sum by (method,name) (last_over_time("
    'locust_request_duration_milliseconds_count{{run_id="{run_id}",status="success"}}[{window}s]))'
)
QUANTILE_QUERY = (
    "histogram_quantile({quantile}, sum by (le,method,name) (last_over_time("
    'locust_request_duration_milliseconds_bucket{{run_id="{run_id}",status="success"}}[{window}s])))'
)
# The same distribution pooled across every request, which is what locust's
# trailing row is a quantile of.
AGGREGATE_QUANTILE_QUERY = (
    "histogram_quantile({quantile}, sum by (le) (last_over_time("
    'locust_request_duration_milliseconds_bucket{{run_id="{run_id}",status="success"}}[{window}s])))'
)


class StatsCSVReport:
    name = "stats"
    filename = "stats.csv"

    def render(self, ctx: RunContext) -> str:
        window = int(ctx.ended - ctx.started) + WINDOW_MARGIN_SECS
        elapsed = max(ctx.ended - ctx.started, 1.0)

        counts = labelled(
            instant_query(ctx.prometheus_url, COUNT_QUERY.format(run_id=ctx.run_id, window=window)),
            "method",
            "name",
            "status",
        )
        if not counts:
            raise ValueError(f"no request counters for run_id={ctx.run_id}")

        latency_sum = labelled(
            instant_query(ctx.prometheus_url, SUM_QUERY.format(run_id=ctx.run_id, window=window)),
            "method",
            "name",
        )
        observations = labelled(
            instant_query(
                ctx.prometheus_url, OBSERVATIONS_QUERY.format(run_id=ctx.run_id, window=window)
            ),
            "method",
            "name",
        )
        quantiles = {
            p: labelled(
                instant_query(
                    ctx.prometheus_url,
                    QUANTILE_QUERY.format(
                        quantile=float(p) / 100, run_id=ctx.run_id, window=window
                    ),
                ),
                "method",
                "name",
            )
            for p in QUERIED
        }
        pooled = {
            p: labelled(
                instant_query(
                    ctx.prometheus_url,
                    AGGREGATE_QUANTILE_QUERY.format(
                        quantile=float(p) / 100, run_id=ctx.run_id, window=window
                    ),
                )
            ).get(())
            for p in QUERIED
        }

        requests: dict[tuple[str, str], float] = {}
        failures: dict[tuple[str, str], float] = {}
        for (method, name, status), value in counts.items():
            key = (method, name)
            requests[key] = requests.get(key, 0.0) + value
            if status == "failure":
                failures[key] = failures.get(key, 0.0) + value

        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(COLUMNS)
        for key in sorted(requests):
            writer.writerow(
                self._row(key, requests, failures, latency_sum, observations, quantiles, elapsed)
            )
        writer.writerow(
            self._aggregated(requests, failures, latency_sum, observations, pooled, elapsed)
        )
        return out.getvalue()

    def _row(self, key, requests, failures, latency_sum, observations, quantiles, elapsed):
        method, name = key
        count = requests.get(key, 0.0)
        failed = failures.get(key, 0.0)
        observed = observations.get(key, 0.0)
        average = latency_sum.get(key, 0.0) / observed if observed else None
        return [
            method,
            name,
            _int(count),
            _int(failed),
            _ms(quantiles["50"].get(key)),
            _ms(average),
            "",  # Min — see the module docstring.
            "",  # Max
            "",  # Average Content Size
            f"{count / elapsed:.2f}",
            f"{failed / elapsed:.2f}",
            *[_ms(quantiles[p].get(key) if p in quantiles else None) for p in PERCENTILES],
        ]

    def _aggregated(self, requests, failures, latency_sum, observations, pooled, elapsed):
        """locust's trailing total row: the counts summed, and the percentiles
        taken over every request's observations pooled into one histogram."""
        count = sum(requests.values())
        failed = sum(failures.values())
        observed = sum(observations.values())
        average = sum(latency_sum.values()) / observed if observed else None
        return [
            "",
            "Aggregated",
            _int(count),
            _int(failed),
            _ms(pooled.get("50")),
            _ms(average),
            "",
            "",
            "",
            f"{count / elapsed:.2f}",
            f"{failed / elapsed:.2f}",
            *[_ms(pooled.get(p)) for p in PERCENTILES],
        ]


def _int(value: float) -> str:
    return f"{round(value)}"


def _ms(value: float | None) -> str:
    """One latency cell. Empty when the run recorded no distribution to take
    it from, which is not the same as a latency of zero."""
    if value is None or value != value or value in (float("inf"), float("-inf")):
        return ""
    return f"{value:.1f}"
