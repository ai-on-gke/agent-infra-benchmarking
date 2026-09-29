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

from matplotlib.patches import Rectangle
from matplotlib.widgets import Button

# The box's height, and each option's in the list.
HEIGHT_IN = 0.28
FACE = "white"
HOVER = "#f0efec"
BORDER = "#c3c2b7"
# Options a scroll wheel notch moves the list by.
SCROLL_ROWS = 3


class Dropdown:
    """The selected option in a box at `rect` (figure coordinates); clicking
    it lists every option below, over whatever is there, and clicking one
    selects it and calls `on_select` with it. A click anywhere else closes the
    list. A list longer than the space below the box shows what fits, and
    scrolls through the rest.

    `colors` maps an option to the color of a line sample drawn beside it, as
    a legend would.

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
        colors: dict[str, str] | None = None,
    ):
        x, y, width, height = rect
        self.fig = fig
        self.selected = options[active]
        self._options = options
        self._on_select = on_select
        self._colors = colors or {}
        self._first = 0

        self._header = self._button(rect)
        self._header.ax.text(
            0.94, 0.5, "▾", ha="right", va="center", transform=self._header.ax.transAxes
        )
        self._header.on_clicked(lambda _: self._set_open(not self._open))

        self._items = []
        for i in range(min(len(options), max(int(y / height), 1))):
            item = self._button((x, y - (i + 1) * height, width, height))
            # Above the panels the list opens across.
            item.ax.set_zorder(10)
            item.on_clicked(lambda _, i=i: self.select(self._options[self._first + i]))
            self._items.append(item)
        self._thumb = None
        if len(self._items) < len(options):
            self._thumb = fig.add_artist(
                Rectangle((x + width - 0.004, 0), 0.003, 0, color=BORDER, zorder=11)
            )

        self._cids = [
            fig.canvas.mpl_connect("button_press_event", self._on_press),
            fig.canvas.mpl_connect("scroll_event", self._on_scroll),
        ]
        self._set_open(False)

    def select(self, option: str) -> None:
        self.selected = option
        self._set_open(False)
        if self._on_select:
            self._on_select(option)
        self.fig.canvas.draw_idle()

    def remove(self) -> None:
        """Disconnect from the canvas. Call it before the figure is cleared: a
        Button takes clicks in its axes' old area even once they are gone."""
        for button in (self._header, *self._items):
            button.disconnect_events()
        for cid in self._cids:
            self.fig.canvas.mpl_disconnect(cid)

    def _button(self, rect) -> Button:
        button = Button(self.fig.add_axes(rect), "", color=FACE, hovercolor=HOVER)
        button.label.set_horizontalalignment("left")
        button.label.set_x(0.08)
        if self._colors:
            (button.sample,) = button.ax.plot(
                [0.05, 0.15], [0.5, 0.5], linewidth=1.5, transform=button.ax.transAxes
            )
        for spine in button.ax.spines.values():
            spine.set_color(BORDER)
        return button

    def _set_open(self, open_: bool) -> None:
        self._open = open_
        if open_:
            # Scrolled to the selected option, if it is out of view.
            index = self._options.index(self.selected)
            if not self._first <= index < self._first + len(self._items):
                self._scroll_to(index - len(self._items) // 2)
        for item in self._items:
            item.ax.set_visible(open_)
            # A hidden Button still takes clicks in its area unless inactive.
            item.set_active(open_)
        if self._thumb:
            self._thumb.set_visible(open_)
        self._fill()

    def _scroll_to(self, first: int) -> None:
        self._first = min(max(first, 0), len(self._options) - len(self._items))

    def _fill(self) -> None:
        """Label the header and the items with the options they show now."""
        shown = zip([self._header, *self._items], [self.selected, *self._options[self._first :]])
        for button, option in shown:
            button.label.set_text(option)
            if button is not self._header:
                button.label.set_fontweight("bold" if option == self.selected else "normal")
            if self._colors:
                button.sample.set_visible(option in self._colors)
                button.sample.set_color(self._colors.get(option, FACE))
                button.label.set_x(0.2 if option in self._colors else 0.08)
        if self._thumb:
            top = self._items[0].ax.get_position().y1
            bottom = self._items[-1].ax.get_position().y0
            span = (top - bottom) / len(self._options)
            self._thumb.set_y(top - (self._first + len(self._items)) * span)
            self._thumb.set_height(len(self._items) * span)
        self.fig.canvas.draw_idle()

    def _on_press(self, event) -> None:
        mine = {self._header.ax, *(item.ax for item in self._items)}
        if self._open and event.inaxes not in mine:
            self._set_open(False)

    def _on_scroll(self, event) -> None:
        if not self._open or event.inaxes not in {item.ax for item in self._items}:
            return
        self._scroll_to(self._first + (-SCROLL_ROWS if event.step > 0 else SCROLL_ROWS))
        self._fill()
