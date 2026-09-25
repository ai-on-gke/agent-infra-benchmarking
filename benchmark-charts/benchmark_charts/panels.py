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

figure() stacks panels on one time axis, each on its own y scale: the main
panel, then worker nodes' CPU utilization, then actors: all of them, and the
worker's active and idle (suspended) ones. Each locust user holds one actor,
so all of them is locust's user count. Given several main panels, it shows one
at a time, and a drop-down above the chart switches between them in place.

CPU is drawn for the nodes the vitals mark Pool=worker, the ones the
WorkerPool runs on, rather than for every node or the cluster as a whole.
Idle actors start hidden; the legend toggles them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

from . import data
from .dropdown import Dropdown

CPU_METRIC = "CPUUtilizationPercent"
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

# The strip above the panels that holds the main panel switch, and the
# switch's own height within it.
SWITCH_STRIP_IN = 0.45
SWITCH_HEIGHT_IN = 0.28


class Panel(Protocol):
    """A chart's own panel, which figure() puts above the shared ones."""

    # Names the panel in the figure's title.
    title: str

    def draw(self, fig, ax) -> None: ...


def cpu(vitals: list[dict[str, str]]) -> dict[str, tuple[list[datetime], list[float]]]:
    """CPU utilization % over time for each worker node, by name. Empty for a
    file from before the Pool column, which marks no node a worker."""
    series: dict[str, tuple[list[datetime], list[float]]] = {}
    for r in vitals:
        value = data.number(r["Value"])
        if r["Name"] != CPU_METRIC or r.get("Pool") != WORKER_POOL or value is None:
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


def figure(
    mains: list[Panel],
    rows: list[dict[str, str]],
    run_id: str = "",
    vitals=(),
    custom=(),
    active: int = 0,
):
    """`mains[active]` above the shared panels, switchable to the rest of
    `mains`. `rows` from locust_stats_history.csv; `vitals` from
    cluster_vitals_history.csv, without which there is no CPU panel; `custom`
    from custom_stats_history.csv, without which there are no actor lines."""
    cpu_series = cpu(vitals)

    heights = (3, 2, 1.5) if cpu_series else (3, 1.5)
    fig, axes = plt.subplots(
        len(heights),
        1,
        sharex=True,
        figsize=(10, 2 * len(heights) + 2),
        gridspec_kw={"height_ratios": heights},
    )
    ax_main, ax_actors = axes[0], axes[-1]

    if cpu_series:
        cpu_panel(fig, axes[1], cpu_series)

    actors_panel(fig, ax_actors, users(rows), actors(custom))
    ax_actors.set_xlabel("time (UTC)", color=INK_MUTED)
    locator = mdates.AutoDateLocator()
    ax_actors.xaxis.set_major_locator(locator)
    ax_actors.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    for ax in axes:
        style(ax)

    def show(main: Panel) -> None:
        # What the last main panel drew goes, and the axes stay, so the shared
        # time axis keeps its range and format. The removed legend's click
        # handler stays connected, but nothing it knows can be clicked again.
        for artist in [*ax_main.lines, *ax_main.collections, *ax_main.patches]:
            artist.remove()
        if ax_main.get_legend():
            ax_main.get_legend().remove()
        main.draw(fig, ax_main)
        parts = [p for p in (main.title, cpu_series and "pool CPU", "actors") if p]
        title = f"{', '.join(parts[:-1])} and {parts[-1]} over time"
        title += f" — run {run_id}" if run_id else ""
        ax_main.set_title(title, loc="left")
        fig.canvas.manager.set_window_title(title)

    show(mains[active])
    if len(mains) == 1:
        fig.tight_layout()
        return fig

    strip = SWITCH_STRIP_IN / fig.get_figheight()
    height = SWITCH_HEIGHT_IN / fig.get_figheight()
    fig.tight_layout(rect=(0, 0, 1, 1 - strip))
    # Over the legend column, which is where the other toggles are.
    left = ax_main.get_position().x1 + 0.01
    by_title = {m.title: m for m in mains}
    # Held by the figure, since the canvas holds widget handlers only weakly.
    fig.main_switch = Dropdown(
        fig,
        (left, 1 - (strip + height) / 2, 1 - left - 0.015, height),
        list(by_title),
        active,
        on_select=lambda title: show(by_title[title]),
    )
    return fig


def cpu_panel(fig, ax, series: dict[str, tuple[list[datetime], list[float]]]) -> None:
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
    toggle_on_click(fig, ax, legend, lines, bottom=0)


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


def _short_name(node: str) -> str:
    """A GKE node, gke-<cluster>-<pool>-<hash>-<id>, as <hash>-<id>: the
    rest is the same for every node of the pool. Other names are kept."""
    if not node.startswith("gke-"):
        return node
    return "-".join(node.split("-")[-2:])
