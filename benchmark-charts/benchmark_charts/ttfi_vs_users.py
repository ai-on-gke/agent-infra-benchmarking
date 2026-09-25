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

"""TTFI percentiles, worker nodes' CPU utilization and the actor counts over the
run, from the rows of locust_stats_history.csv, cluster_vitals_history.csv and
custom_stats_history.csv.

Panels on one time axis, each on its own y scale: latency, then CPU, then
actors: all of them, and the worker's active and idle (suspended) ones. Each
locust user holds one actor, so all of them is locust's user count.

CPU is drawn for the nodes the vitals mark Pool=worker, the ones the
WorkerPool runs on, rather than for every node or the cluster as a whole.

Each row's percentiles cover locust's trailing 10s window, so a step in users
shows up in latency spread over the 10s after it.

Every percentile column in the file is drawn; p50 alone starts visible, and
the legend toggles the rest. Idle actors start hidden too.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.ticker import MaxNLocator

from . import data

REQUEST_NAME = "TTFI"

DEFAULT_VISIBLE = {"50%"}

# Ordinal blue ramp, light to dark, that the percentiles are spread across so
# the tail reads heaviest.
RAMP = (
    "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6",
    "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b",
)

CPU_METRIC = "CPUUtilizationPercent"
WORKER_POOL = "worker"
# Categorical slots after blue, which the latency panel already means, and
# without the two ACTORS takes. A node past the last slot is drawn muted rather
# than in a repeated hue.
NODE_COLORS = ("#eb6834", "#1baf7a", "#eda100", "#008300", "#e34948")

ACTORS_COLOR = "#52514e"
# (custom_stats_history.csv Name, legend label, color).
ACTORS = (
    ("ActiveActors", "active actors", "#4a3aa7"),
    ("IdleActors", "idle actors", "#e87ba4"),
)
# Off until toggled on in the legend, like the percentiles past DEFAULT_VISIBLE.
ACTORS_DEFAULT_HIDDEN = {"IdleActors"}
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"


def percentile_columns(rows: list[dict[str, str]]) -> list[str]:
    """The file's percentile columns ("50%", ..., "100%"), lowest first."""
    header = rows[0] if rows else {}
    return sorted((c for c in header if c.endswith("%")), key=lambda c: float(c[:-1]))


def latency(rows: list[dict[str, str]]) -> tuple[list[datetime], dict[str, list[float]]]:
    """The TTFI rows' times, and each percentile column's values in ms,
    lowest percentile first."""
    ttfi = [r for r in rows if r["Name"] == REQUEST_NAME]
    # locust writes 0 for a window that held no requests; NaN breaks the line
    # there instead of drawing it down to zero.
    values = {
        col: [data.number(r[col]) or math.nan for r in ttfi]
        for col in percentile_columns(rows)
    }
    return [_time(r["Timestamp"]) for r in ttfi], values


def cpu(vitals: list[dict[str, str]]) -> dict[str, tuple[list[datetime], list[float]]]:
    """CPU utilization % over time for each worker node, by name. Empty for a
    file from before the Pool column, which marks no node a worker."""
    series: dict[str, tuple[list[datetime], list[float]]] = {}
    for r in vitals:
        value = data.number(r["Value"])
        if r["Name"] != CPU_METRIC or r.get("Pool") != WORKER_POOL or value is None:
            continue
        times, values = series.setdefault(r["Type"], ([], []))
        times.append(_time(r["Timestamp"]))
        values.append(value)
    return dict(sorted(series.items()))


def actors(custom: list[dict[str, str]]) -> dict[str, tuple[list[datetime], list[float]]]:
    """Each ACTORS gauge over time, by Name; a gauge the file lacks is left out."""
    names = {name for name, _, _ in ACTORS}
    series: dict[str, tuple[list[datetime], list[float]]] = {}
    for r in custom:
        value = data.number(r["Value"])
        if r["Name"] not in names or value is None:
            continue
        times, values = series.setdefault(r["Name"], ([], []))
        times.append(_time(r["Timestamp"]))
        values.append(value)
    return series


def users(rows: list[dict[str, str]]) -> tuple[list[datetime], list[int]]:
    """The user count at each second, which every row of that second repeats."""
    counts = {int(r["Timestamp"]): int(r["User Count"]) for r in rows}
    seconds = sorted(counts)
    return [_time(s) for s in seconds], [counts[s] for s in seconds]


def show(rows: list[dict[str, str]], run_id: str = "", vitals=(), custom=()) -> None:
    """Draw the chart and block on its matplotlib window."""
    figure(rows, run_id, vitals, custom)
    plt.show()


def figure(rows: list[dict[str, str]], run_id: str = "", vitals=(), custom=()):
    """`rows` from locust_stats_history.csv; `vitals` from
    cluster_vitals_history.csv, without which there is no CPU panel; `custom`
    from custom_stats_history.csv, without which there are no actor lines."""
    times, values = latency(rows)
    if all(math.isnan(v) for col in values.values() for v in col):
        raise ValueError(f"no {REQUEST_NAME} samples in {data.STATS_HISTORY}")
    user_times, user_counts = users(rows)
    cpu_series = cpu(vitals)
    actor_series = actors(custom)
    parts = [p for p in (REQUEST_NAME, cpu_series and "pool CPU", "actors") if p]
    title = f"{', '.join(parts[:-1])} and {parts[-1]} over time"
    title += f" — run {run_id}" if run_id else ""

    heights = (3, 2, 1.5) if cpu_series else (3, 1.5)
    fig, axes = plt.subplots(
        len(heights),
        1,
        sharex=True,
        figsize=(10, 2 * len(heights) + 2),
        gridspec_kw={"height_ratios": heights},
    )
    ax_ms, ax_actors = axes[0], axes[-1]

    # Drawn tail first, so the legend lists the percentiles top to bottom in
    # the order the lines stack.
    columns = list(values)
    colors = dict(zip(columns, _ramp(len(columns))))
    lines = [
        ax_ms.plot(
            times,
            values[col],
            color=colors[col],
            linewidth=1.5,
            label=f"p{col[:-1]}",
            visible=col in DEFAULT_VISIBLE,
        )[0]
        for col in reversed(columns)
    ]
    ax_ms.set_title(title, loc="left")
    ax_ms.set_ylabel(f"{REQUEST_NAME} (ms)", color=INK_MUTED)
    legend = ax_ms.legend(
        frameon=False, loc="upper left", bbox_to_anchor=(1, 1), title="click to toggle"
    )
    legend.get_title().set_color(INK_MUTED)
    _toggle_on_click(fig, ax_ms, legend, lines)

    if cpu_series:
        _cpu_panel(fig, axes[1], cpu_series)

    _actors_panel(fig, ax_actors, (user_times, user_counts), actor_series)
    ax_actors.set_xlabel("time (UTC)", color=INK_MUTED)
    locator = mdates.AutoDateLocator()
    ax_actors.xaxis.set_major_locator(locator)
    ax_actors.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    for ax in axes:
        _style(ax)
    fig.tight_layout()
    fig.canvas.manager.set_window_title(title)
    return fig


def _cpu_panel(fig, ax, series: dict[str, tuple[list[datetime], list[float]]]) -> None:
    node_colors = iter(NODE_COLORS)
    lines = [
        ax.plot(
            times,
            values,
            color=next(node_colors, INK_MUTED),
            linewidth=1.5,
            label=_short_name(node),
        )[0]
        for node, (times, values) in series.items()
    ]
    ax.set_ylabel("pool CPU (%)", color=INK_MUTED)
    legend = ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(1, 1))
    # From 0, unlike latency: utilization is a share of capacity.
    _toggle_on_click(fig, ax, legend, lines, bottom=0)


def _actors_panel(fig, ax, user_series, actor_series) -> None:
    """Every actor, which is locust's user count, and the active and idle
    gauges beside it on the same scale."""
    lines = ax.step(
        *user_series, where="post", color=ACTORS_COLOR, linewidth=1.5, label="actors"
    )
    for name, label, color in ACTORS:
        if name in actor_series:
            lines += ax.step(
                *actor_series[name],
                where="post",
                color=color,
                linewidth=1.5,
                label=label,
                visible=name not in ACTORS_DEFAULT_HIDDEN,
            )
    ax.set_ylabel("actors", color=INK_MUTED)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    if actor_series:
        legend = ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(1, 1))
        _toggle_on_click(fig, ax, legend, lines, bottom=0)
    else:
        ax.set_ylim(bottom=0)


def _short_name(node: str) -> str:
    """A GKE node, gke-<cluster>-<pool>-<hash>-<id>, as <hash>-<id>: the
    rest is the same for every node of the pool. Other names are kept."""
    if not node.startswith("gke-"):
        return node
    return "-".join(node.split("-")[-2:])


def _toggle_on_click(fig, ax, legend, lines, bottom: float | None = None) -> None:
    """Clicking a legend entry, its line or its label, shows or hides that
    line, and fits the y axis to the lines left showing. Without `bottom` the
    fit is to the lines rather than to 0, so a flat line's variation stays
    readable."""
    entries = {}
    for line, handle, text in zip(lines, legend.get_lines(), legend.get_texts()):
        # The legend copies a hidden line's visibility onto its sample; the
        # sample stays drawn, faded, so the entry can be seen and clicked.
        handle.set_visible(True)
        for artist in (handle, text):
            artist.set_picker(5)
            artist.set_alpha(1.0 if line.get_visible() else 0.25)
            entries[artist] = (line, handle, text)

    def fit_y() -> None:
        ax.relim(visible_only=True)
        ax.autoscale(axis="y")
        if bottom is not None:
            ax.set_ylim(bottom=bottom)

    def on_pick(event) -> None:
        if event.artist not in entries:
            return
        line, handle, text = entries[event.artist]
        line.set_visible(not line.get_visible())
        for artist in (handle, text):
            artist.set_alpha(1.0 if line.get_visible() else 0.25)
        fit_y()
        fig.canvas.draw_idle()

    fit_y()
    fig.canvas.mpl_connect("pick_event", on_pick)


def _ramp(n: int) -> list:
    """`n` colors spread evenly over RAMP, lightest first."""
    cmap = LinearSegmentedColormap.from_list("ramp", RAMP)
    return [cmap(i / max(n - 1, 1)) for i in range(n)]


def _time(timestamp: str | int) -> datetime:
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)


def _style(ax) -> None:
    ax.grid(axis="y", color=GRIDLINE, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_MUTED)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("bottom", "left"):
        ax.spines[side].set_color(INK_MUTED)
