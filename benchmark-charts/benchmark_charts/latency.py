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
"""

from __future__ import annotations

import math
from datetime import datetime

from matplotlib.colors import LinearSegmentedColormap

from . import data
from .panels import INK_MUTED, to_time, toggle_on_click

DEFAULT_VISIBLE = {"50%"}

# Ordinal blue ramp, light to dark, that the percentiles are spread across so
# the tail reads heaviest.
RAMP = (
    "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6",
    "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b",
)


class LatencyPanel:
    """Percentiles over time of the rows named `request_name` (TTFI,
    ExecutionTime, ...). Raises ValueError when the run has none."""

    def __init__(self, rows: list[dict[str, str]], request_name: str):
        self.title = request_name
        self.times, self.values = latency(rows, request_name)
        if all(math.isnan(v) for col in self.values.values() for v in col):
            raise ValueError(f"no {request_name} samples in {data.STATS_HISTORY}")

    def draw(self, fig, ax) -> None:
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


def _ramp(n: int) -> list:
    """`n` colors spread evenly over RAMP, lightest first."""
    cmap = LinearSegmentedColormap.from_list("ramp", RAMP)
    return [cmap(i / max(n - 1, 1)) for i in range(n)]
