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

"""TTFI percentiles against concurrent users, from the rows of
locust_stats_history.csv.

Most useful on a ramped run (`spawn-rate` below `users` in the case), where the
ramp sweeps the user count. A fixed-load run gives one point per percentile.

Each history row's percentiles cover locust's trailing 10s window, so a row is
attributed to the user count at the window's midpoint, and the rows at one
user count are averaged into one point.
"""

from __future__ import annotations

import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

from . import stats_history

REQUEST_NAME = "TTFI"

HALF_WINDOW_SECS = 5

# Ordinal blue ramp, light to dark, so the tail reads heaviest.
PERCENTILES = (
    ("50%", "p50", "#86b6ef"),
    ("90%", "p90", "#3987e5"),
    ("95%", "p95", "#1c5cab"),
    ("99%", "p99", "#0d366b"),
)

INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"


def points(rows: list[dict[str, str]]) -> dict[str, list[tuple[int, float]]]:
    """{column: [(users, mean latency ms), ...]} for each percentile column."""
    users_at = {int(r["Timestamp"]): int(r["User Count"]) for r in rows}
    levels: dict[str, dict[int, list[float]]] = {col: {} for col, _, _ in PERCENTILES}
    for row in rows:
        if row["Name"] != REQUEST_NAME:
            continue
        users = users_at.get(int(row["Timestamp"]) - HALF_WINDOW_SECS)
        if not users:
            continue
        for col, _, _ in PERCENTILES:
            value = stats_history.number(row[col])
            # locust writes 0 for a window that held no requests.
            if value:
                levels[col].setdefault(users, []).append(value)
    return {
        col: sorted((u, sum(v) / len(v)) for u, v in by_users.items())
        for col, by_users in levels.items()
    }


def show(rows: list[dict[str, str]], run_id: str = "") -> None:
    """Draw the chart and block on its matplotlib window."""
    figure(rows, run_id)
    plt.show()


def figure(rows: list[dict[str, str]], run_id: str = ""):
    series = points(rows)
    if not any(series.values()):
        raise ValueError(f"no {REQUEST_NAME} samples in {stats_history.FILENAME}")
    title = f"{REQUEST_NAME} vs concurrent users" + (f" — run {run_id}" if run_id else "")

    fig, ax = plt.subplots(figsize=(9, 5))
    # Drawn tail first, so the legend lists the percentiles top to bottom in
    # the order the lines stack.
    for col, label, color in reversed(PERCENTILES):
        if not series[col]:
            continue
        xs, ys = zip(*series[col])
        ax.plot(xs, ys, color=color, linewidth=2, marker="o", markersize=5, label=label)

    ax.set_title(title, loc="left")
    ax.set_xlabel("concurrent users", color=INK_MUTED)
    ax.set_ylabel(f"{REQUEST_NAME} (ms)", color=INK_MUTED)
    ax.set_xlim(left=0)
    ax.set_ylim(bottom=0)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.grid(axis="y", color=GRIDLINE, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_MUTED)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("bottom", "left"):
        ax.spines[side].set_color(INK_MUTED)
    ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(1, 1))
    fig.tight_layout()
    fig.canvas.manager.set_window_title(title)
    return fig
