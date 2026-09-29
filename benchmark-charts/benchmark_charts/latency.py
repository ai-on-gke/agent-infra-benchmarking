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

"""One request's latency percentiles over the run, from the rows of
locust_stats_history.csv, as a main panel for panels.draw.

Each row's percentiles cover locust's trailing 10s window, so a step in users
shows up in latency spread over the 10s after it.

Every percentile column in the file is drawn; p50 alone starts visible, and
the legend toggles the rest.

Under them, in a panel of its own, is the request's throughput from the same
rows: all of it, and its successful and failed parts.
"""

from __future__ import annotations

import math
from datetime import datetime

from matplotlib.colors import LinearSegmentedColormap

from . import data
from .panels import ACTORS_COLOR, INK_MUTED, to_time, toggle_on_click

DEFAULT_VISIBLE = {"50%"}

# (throughput() key, legend label, color, line style). Success and failure
# wear the status colors, since that is what they mean; all of it the neutral
# that all actors do. That one is dashed and on top, so it still shows where it
# is the successful line too, which is whenever nothing failed.
THROUGHPUT = (
    ("total", "all", ACTORS_COLOR, "--"),
    ("successful", "successful", "#0ca30c", "-"),
    ("failed", "failed", "#d03b3b", "-"),
)

# Ordinal blue ramp, light to dark, that the percentiles are spread across so
# the tail reads heaviest.
RAMP = (
    "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6",
    "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b",
)


class LatencyPanel:
    """Percentiles over time of the rows named `request_name` (TTFI,
    ExecutionTime, ...), which `title` names in the window if given. Raises
    ValueError when the run has none."""

    def __init__(self, rows: list[dict[str, str]], request_name: str, title: str = ""):
        self.title = title or request_name
        self.times, self.values = latency(rows, request_name)
        if all(math.isnan(v) for col in self.values.values() for v in col):
            raise ValueError(f"no {request_name} samples in {data.STATS_HISTORY}")
        self.throughput = throughput(rows, request_name)

    def draw(self, fig, ax, ax_rate) -> None:
        # Drawn tail first, so the legend lists the percentiles top to bottom
        # in the order the lines stack.
        columns = list(self.values)
        colors = dict(zip(columns, _ramp(len(columns))))
        lines = [
            ax.plot(
                self.times,
                self.values[col],
                color=colors[col],
                linewidth=1.5,
                label=f"p{col[:-1]}",
                visible=col in DEFAULT_VISIBLE,
            )[0]
            for col in reversed(columns)
        ]
        ax.set_ylabel(f"{self.title} (ms)", color=INK_MUTED)
        legend = ax.legend(
            frameon=False, loc="upper left", bbox_to_anchor=(1, 1), title="click to toggle"
        )
        legend.get_title().set_color(INK_MUTED)
        toggle_on_click(fig, ax, legend, lines)

        rates = [
            ax_rate.plot(
                self.times,
                self.throughput[key],
                color=color,
                linestyle=style,
                linewidth=1.5,
                label=label,
                zorder=3 if key == "total" else 2,
            )[0]
            for key, label, color, style in THROUGHPUT
        ]
        ax_rate.set_ylabel("throughput (req/s)", color=INK_MUTED)
        legend = ax_rate.legend(frameon=False, loc="upper left", bbox_to_anchor=(1, 1))
        # From 0, unlike latency: how much got done is read against none.
        toggle_on_click(fig, ax_rate, legend, rates, bottom=0)


def percentile_columns(rows: list[dict[str, str]]) -> list[str]:
    """The file's percentile columns ("50%", ..., "100%"), lowest first."""
    header = rows[0] if rows else {}
    return sorted((c for c in header if c.endswith("%")), key=lambda c: float(c[:-1]))


def latency(
    rows: list[dict[str, str]], request_name: str
) -> tuple[list[datetime], dict[str, list[float]]]:
    """The times of the rows named `request_name`, and each percentile
    column's values in ms, lowest percentile first."""
    named = [r for r in rows if r["Name"] == request_name]
    # locust writes 0 for a window that held no requests; NaN breaks the line
    # there instead of drawing it down to zero.
    values = {
        col: [data.number(r[col]) or math.nan for r in named]
        for col in percentile_columns(rows)
    }
    return [to_time(r["Timestamp"]) for r in named], values


def throughput(rows: list[dict[str, str]], request_name: str) -> dict[str, list[float]]:
    """Requests per second of the rows named `request_name`, in the order
    latency() gives their times: in total, successful and failed. Unlike
    latency, 0 is drawn: a window where nothing got done is a throughput of
    none."""
    rates: dict[str, list[float]] = {"total": [], "successful": [], "failed": []}
    for r in rows:
        if r["Name"] != request_name:
            continue
        total, failed = data.number(r["Requests/s"]), data.number(r["Failures/s"])
        if total is None or failed is None:
            total = failed = math.nan
        rates["total"].append(total)
        rates["failed"].append(failed)
        # locust counts a failed request among Requests/s too.
        rates["successful"].append(total - failed)
    return rates


def _ramp(n: int) -> list:
    """`n` colors spread evenly over RAMP, lightest first."""
    cmap = LinearSegmentedColormap.from_list("ramp", RAMP)
    return [cmap(i / max(n - 1, 1)) for i in range(n)]
