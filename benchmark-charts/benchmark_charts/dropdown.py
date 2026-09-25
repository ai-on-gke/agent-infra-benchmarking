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

"""A drop-down list built from matplotlib's own Buttons. matplotlib has none,
and a GUI toolkit's (Tk, Qt, ...) would tie the package to one backend."""

from __future__ import annotations

from typing import Callable

from matplotlib.widgets import Button

FACE = "white"
HOVER = "#f0efec"
BORDER = "#c3c2b7"


class Dropdown:
    """The selected option in a box at `rect` (figure coordinates); clicking
    it lists every option below, over whatever is there, and clicking one
    selects it and calls `on_select` with it. A click anywhere else closes the
    list.

    Hold a reference to it for as long as the figure is open: the canvas holds
    widget handlers only weakly.
    """

    def __init__(
        self,
        fig,
        rect: tuple[float, float, float, float],
        options: list[str],
        active: int = 0,
        on_select: Callable[[str], None] | None = None,
    ):
        x, y, width, height = rect
        self.fig = fig
        self.selected = options[active]
        self._on_select = on_select

        self._header = self._button(rect, options[active])
        self._header.ax.text(
            0.94, 0.5, "▾", ha="right", va="center", transform=self._header.ax.transAxes
        )
        self._header.on_clicked(lambda _: self._set_open(not self._open))

        self._items = {}
        for i, option in enumerate(options):
            item = self._button((x, y - (i + 1) * height, width, height), option)
            # Above the panels the list opens across.
            item.ax.set_zorder(10)
            item.on_clicked(lambda _, option=option: self.select(option))
            self._items[option] = item

        fig.canvas.mpl_connect("button_press_event", self._on_press)
        self._set_open(False)
        self._mark_selected()

    def select(self, option: str) -> None:
        self.selected = option
        self._header.label.set_text(option)
        self._mark_selected()
        self._set_open(False)
        if self._on_select:
            self._on_select(option)
        self.fig.canvas.draw_idle()

    def _button(self, rect, label: str) -> Button:
        button = Button(self.fig.add_axes(rect), label, color=FACE, hovercolor=HOVER)
        button.label.set_horizontalalignment("left")
        button.label.set_x(0.08)
        for spine in button.ax.spines.values():
            spine.set_color(BORDER)
        return button

    def _set_open(self, open_: bool) -> None:
        self._open = open_
        for item in self._items.values():
            item.ax.set_visible(open_)
            # A hidden Button still takes clicks in its area unless inactive.
            item.set_active(open_)
        self.fig.canvas.draw_idle()

    def _mark_selected(self) -> None:
        for option, item in self._items.items():
            item.label.set_fontweight("bold" if option == self.selected else "normal")

    def _on_press(self, event) -> None:
        mine = {self._header.ax, *(item.ax for item in self._items.values())}
        if self._open and event.inaxes not in mine:
            self._set_open(False)
