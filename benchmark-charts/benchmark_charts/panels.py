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

"""The panels every chart shares, under a chart's own main panel.

draw() stacks panels on one time axis, each on its own y scale: the main
panel, then its request's throughput, then worker nodes' CPU and memory
utilization, then actors: all of them, and the worker's active and idle
(suspended) ones. Each locust user holds one actor, so all of them is locust's
user count. Given several main panels, it shows one at a time, and swaps
between them in place, throughput along with it.

CPU and memory are drawn for the nodes the vitals mark Pool=worker, the ones
the WorkerPool runs on, rather than for every node or the cluster as a whole.
A pool can have many nodes, so a drop-down beside the panel picks them, all or
one alone, rather than a legend listing each.
Idle actors start hidden; the legend toggles them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

import matplotlib.dates as mdates
from matplotlib.lines import Line2D
from matplotlib.ticker import MaxNLocator

from . import data
from .dropdown import HEIGHT_IN, Dropdown

CPU_METRIC = "CPUUtilizationPercent"
MEMORY_METRIC = "MemoryUtilizationPercent"
WORKER_POOL = "worker"
# Categorical slots after blue, which the main panel's latency already means,
# and without the two ACTORS takes. A node past the last slot is drawn muted
# rather than in a repeated hue.
NODE_COLORS = ("#eb6834", "#1baf7a", "#eda100", "#008300", "#e34948")

ACTORS_COLOR = "#52514e"
# (custom_stats_history.csv Name, legend label, color).
ACTORS = (
    ("ActiveActors", "active actors", "#4a3aa7"),
    ("IdleActors", "idle actors", "#e87ba4"),
)
ACTORS_DEFAULT_HIDDEN = {"IdleActors"}
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"


class Panel(Protocol):
    """A chart's own panel, which draw() puts above the shared ones."""

    # Names the panel in the figure's title.
    title: str

    def draw(self, fig, ax, ax_rate) -> None:
        """Draw the request on `ax`, and its throughput on `ax_rate`, the
        panel under it."""


def pool_metric(
    vitals: list[dict[str, str]], metric: str
) -> dict[str, tuple[list[datetime], list[float]]]:
    """`metric` over time for each worker node, by name. Empty for a file from
    before the Pool column, which marks no node a worker."""
    series: dict[str, tuple[list[datetime], list[float]]] = {}
    for r in vitals:
        value = data.number(r["Value"])
        if r["Name"] != metric or r.get("Pool") != WORKER_POOL or value is None:
            continue
        times, values = series.setdefault(r["Type"], ([], []))
        times.append(to_time(r["Timestamp"]))
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
        times.append(to_time(r["Timestamp"]))
        values.append(value)
    return series


def users(rows: list[dict[str, str]]) -> tuple[list[datetime], list[int]]:
    """The user count at each second, which every row of that second repeats."""
    counts = {int(r["Timestamp"]): int(r["User Count"]) for r in rows}
    seconds = sorted(counts)
    return [to_time(s) for s in seconds], [counts[s] for s in seconds]


def draw(
    fig,
    mains: list[Panel],
    rows: list[dict[str, str]],
    name: str = "",
    vitals=(),
    custom=(),
    active: int = 0,
    top: float = 1.0,
):
    """`mains[active]` above the shared panels, drawn into `fig` below `top`
    (a figure fraction), which leaves the space above for widgets. `rows` from
    locust_stats_history.csv; `vitals` from cluster_vitals_history.csv, without
    which there is no CPU panel; `custom` from custom_stats_history.csv,
    without which there are no actor lines.

    Returns the main panel's axes, a function that swaps it in place to
    another of `mains`, by index, and the panels' drop-downs, to remove() before
    the figure is cleared.
    """
    cpu_series = pool_metric(vitals, CPU_METRIC)
    memory_series = pool_metric(vitals, MEMORY_METRIC)

    heights = (3, 1.5, 2, 1.5) if cpu_series else (3, 1.5, 1.5)
    axes = fig.subplots(len(heights), 1, sharex=True, gridspec_kw={"height_ratios": heights})
    ax_main, ax_rate, ax_actors = axes[0], axes[1], axes[-1]

    pool_title = ""
    if cpu_series:
        pool = pool_panel(fig, axes[2], cpu_series, memory_series)
        pool_title = "pool CPU/memory" if memory_series else "pool CPU"

    actors_panel(fig, ax_actors, users(rows), actors(custom))
    ax_actors.set_xlabel("time (UTC)", color=INK_MUTED)
    locator = mdates.AutoDateLocator()
    ax_actors.xaxis.set_major_locator(locator)
    ax_actors.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    for ax in axes:
        style(ax)

    def show(index: int) -> None:
        # What the last main panel drew goes, and the axes stay, so the shared
        # time axis keeps its range and format. The removed legend's click
        # handler stays connected, but nothing it knows can be clicked again.
        for ax in (ax_main, ax_rate):
            for artist in [*ax.lines, *ax.collections, *ax.patches]:
                artist.remove()
            if ax.get_legend():
                ax.get_legend().remove()
        main = mains[index]
        main.draw(fig, ax_main, ax_rate)
        parts = [p for p in (main.title, "throughput", pool_title, "actors") if p]
        title = f"{', '.join(parts[:-1])} and {parts[-1]} over time"
        title += f" — run {name}" if name else ""
        ax_main.set_title(title, loc="left")
        fig.canvas.manager.set_window_title(title)

    show(active)
    fig.tight_layout(rect=(0, 0, 1, top))
    # Placed once the layout has settled where the panel is.
    switches = [node_switch(fig, pool)] if cpu_series else []
    return ax_main, show, switches


class PoolLines:
    """The pool panel's lines, by node's short name and then metric ("CPU",
    "memory"). A line shows while the drop-down has its node, or every node,
    and the key has its metric on."""

    def __init__(self, ax, lines: dict[str, dict]):
        self.ax = ax
        self.lines = lines
        self.node: str | None = None  # None: every node.
        self.off: set[str] = set()

    def show_node(self, node: str | None) -> None:
        self.node = node
        self.apply()

    def apply(self) -> None:
        for node, by_metric in self.lines.items():
            for metric, line in by_metric.items():
                line.set_visible(self.node in (None, node) and metric not in self.off)
        fit_y(self.ax, bottom=0)


class MetricToggle:
    """One metric's lines, every node's, as a single line to toggle_on_click:
    the key's entry for it shows and hides them all."""

    def __init__(self, pool: PoolLines, metric: str):
        self.pool = pool
        self.metric = metric

    def get_visible(self) -> bool:
        return self.metric not in self.pool.off

    def set_visible(self, on: bool) -> None:
        if on:
            self.pool.off.discard(self.metric)
        else:
            self.pool.off.add(self.metric)
        self.pool.apply()


def pool_panel(fig, ax, cpu_series, memory_series) -> PoolLines:
    """Each node's CPU utilization, and its memory utilization dashed in the
    same color: both a share of the node's capacity, so they read off one
    scale."""
    node_colors = iter(NODE_COLORS)
    lines = {}
    for label, (node, series) in zip(_short_names(list(cpu_series)), cpu_series.items()):
        color = next(node_colors, INK_MUTED)
        lines[label] = {"CPU": ax.plot(*series, color=color, linewidth=1.5)[0]}
        if node in memory_series:
            lines[label]["memory"] = ax.plot(
                *memory_series[node], color=color, linewidth=1.5, linestyle="--"
            )[0]
    pool = PoolLines(ax, lines)
    if memory_series:
        ax.set_ylabel("pool CPU/memory (%)", color=INK_MUTED)
        # The node drop-down takes the top of the legend column; this key
        # sits at its foot, since the node colors alone do not say which
        # line is which.
        key = [
            Line2D([], [], color=INK_MUTED, linewidth=1.5, linestyle=style, label=metric)
            for metric, style in (("CPU", "-"), ("memory", "--"))
        ]
        legend = ax.legend(handles=key, frameon=False, loc="lower left", bbox_to_anchor=(1, 0))
        toggles = [MetricToggle(pool, handle.get_label()) for handle in key]
        toggle_on_click(fig, ax, legend, toggles, bottom=0)
    else:
        ax.set_ylabel("pool CPU (%)", color=INK_MUTED)
    # From 0, unlike latency: utilization is a share of capacity.
    fit_y(ax, bottom=0)
    return pool


def node_switch(fig, pool: PoolLines) -> Dropdown:
    """A drop-down over the legend column beside the pool panel, that shows
    every node's lines or one node's alone."""
    every = f"all nodes ({len(pool.lines)})"
    pos = pool.ax.get_position()
    height = HEIGHT_IN / fig.get_figheight()
    x = pos.x1 + 0.01
    return Dropdown(
        fig,
        (x, pos.y1 - height, 1 - x - 0.015, height),
        [every, *pool.lines],
        on_select=lambda option: pool.show_node(None if option == every else option),
        colors={label: by_metric["CPU"].get_color() for label, by_metric in pool.lines.items()},
    )


def actors_panel(fig, ax, user_series, actor_series) -> None:
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
        toggle_on_click(fig, ax, legend, lines, bottom=0)
    else:
        ax.set_ylim(bottom=0)


def toggle_on_click(fig, ax, legend, lines, bottom: float | None = None) -> None:
    """Clicking a legend entry, its line or its label, shows or hides that
    line, and fits the y axis to the lines left showing. Without `bottom` the
    fit is to the lines rather than to 0, so a flat line's variation stays
    readable. `lines` may hold anything that shows and hides like a line."""
    entries = {}
    for line, handle, text in zip(lines, legend.get_lines(), legend.get_texts()):
        # The legend copies a hidden line's visibility onto its sample; the
        # sample stays drawn, faded, so the entry can be seen and clicked.
        handle.set_visible(True)
        for artist in (handle, text):
            artist.set_picker(5)
            artist.set_alpha(1.0 if line.get_visible() else 0.25)
            entries[artist] = (line, handle, text)

    def on_pick(event) -> None:
        if event.artist not in entries:
            return
        line, handle, text = entries[event.artist]
        line.set_visible(not line.get_visible())
        for artist in (handle, text):
            artist.set_alpha(1.0 if line.get_visible() else 0.25)
        fit_y(ax, bottom)
        fig.canvas.draw_idle()

    fit_y(ax, bottom)
    fig.canvas.mpl_connect("pick_event", on_pick)


def fit_y(ax, bottom: float | None = None) -> None:
    """Fit the y axis to the lines showing, from `bottom` if given."""
    ax.relim(visible_only=True)
    ax.autoscale(axis="y")
    if bottom is not None:
        ax.set_ylim(bottom=bottom)


def to_time(timestamp: str | int) -> datetime:
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)


def style(ax) -> None:
    ax.grid(axis="y", color=GRIDLINE, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_MUTED)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("bottom", "left"):
        ax.spines[side].set_color(INK_MUTED)


def _short_names(nodes: list[str]) -> list[str]:
    """Each name without the leading dash-separated parts all of them share,
    keeping at least the last: GKE's gke-<cluster>-<pool>-<hash>-<id> as <id>
    when the pool is in one zone, and as <hash>-<id> when in several."""
    parts = [node.split("-") for node in nodes]
    shared = 0
    while all(len(p) > shared + 1 and p[shared] == parts[0][shared] for p in parts):
        shared += 1
    return ["-".join(p[shared:]) for p in parts]
