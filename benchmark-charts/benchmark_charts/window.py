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

"""A run's jobs in one matplotlib window.

A drop-down above the panels switches job, redrawing every panel in place; the
one over the legends switches the main panel, and its choice carries over to
the next job that has it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Union

import matplotlib.pyplot as plt

from . import panels
from .dropdown import HEIGHT_IN, Dropdown

# The strip above the panels that holds the drop-downs.
STRIP_IN = 0.45
# Figure fractions.
JOB_SWITCH_WIDTH = 0.3
MAIN_SWITCH_WIDTH = 0.25
DEFAULT_LEFT = 0.1


@dataclass
class Job:
    """One job's data, as panels.draw takes it."""

    title: str
    mains: list
    rows: list[dict[str, str]]
    vitals: list[dict[str, str]]
    custom: list[dict[str, str]]
    steady: tuple[datetime, datetime] | None = None


class Window:
    """`jobs` maps each job's name to its folder, and `load` reads one into a
    Job, or raises OSError or ValueError when it cannot be drawn. A job is read
    when it is first shown."""

    def __init__(
        self,
        jobs: dict[str, Path],
        load: Callable[[Path], Job],
        job: str,
        main: str = "",
    ):
        self.fig = plt.figure(figsize=(10, 10))
        self._jobs = jobs
        self._load = load
        self._loaded: dict[str, Union[Job, str]] = {}
        self._main = main
        self.show(job)

    def show(self, name: str) -> None:
        job = self._job(name)
        fig = self.fig
        for switch in getattr(fig, "switches", []):
            switch.remove()
        fig.clear()
        strip = STRIP_IN / fig.get_figheight()
        height = HEIGHT_IN / fig.get_figheight()
        y = 1 - (strip + height) / 2
        switches = []
        # False when the job could not be read, and the window says why.
        self.drawn = not isinstance(job, str)

        if not self.drawn:
            fig.text(0.5, 0.5, f"{name}: {job}", ha="center", va="center", wrap=True)
            fig.canvas.manager.set_window_title(name)
            left = DEFAULT_LEFT
        else:
            titles = [m.title for m in job.mains]
            if self._main not in titles:
                self._main = titles[0]
            has_switch = len(self._jobs) > 1 or len(titles) > 1
            ax_main, show_main, switches = panels.draw(
                fig,
                job.mains,
                job.rows,
                job.title,
                job.vitals,
                job.custom,
                job.steady,
                active=titles.index(self._main),
                top=1 - strip if has_switch else 1,
            )
            left = ax_main.get_position().x0
            if len(titles) > 1:
                # Ending over the legend column, which is where the other
                # toggles are, and reaching left of it for the titles to fit.
                x = 1 - 0.015 - MAIN_SWITCH_WIDTH

                def on_main(title: str) -> None:
                    self._main = title
                    show_main(titles.index(title))

                switches.append(
                    Dropdown(
                        fig,
                        (x, y, MAIN_SWITCH_WIDTH, height),
                        titles,
                        titles.index(self._main),
                        on_select=on_main,
                    )
                )

        if len(self._jobs) > 1:
            names = list(self._jobs)
            fig.text(left - 0.01, y + height / 2, "job", ha="right", va="center", color=panels.INK_MUTED)
            switches.append(
                Dropdown(fig, (left, y, JOB_SWITCH_WIDTH, height), names, names.index(name), self.show)
            )
        # Held by the figure, since the canvas holds widget handlers only weakly.
        fig.switches = switches
        fig.canvas.draw_idle()

    def _job(self, name: str) -> Union[Job, str]:
        """The job, or why it cannot be drawn."""
        if name not in self._loaded:
            try:
                self._loaded[name] = self._load(self._jobs[name])
            except (OSError, ValueError) as exc:
                print(f"benchmark-report: {name}: {exc}", file=sys.stderr)
                self._loaded[name] = str(exc)
        return self._loaded[name]
