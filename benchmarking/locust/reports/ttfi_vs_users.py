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

"""TTFI p50 against concurrent users, as one HTML file.

Most useful on a ramped run (--spawn-rate below --users), where the ramp
sweeps the user count and the run traces a load curve. A fixed-load run
produces a single point, which is still a valid file.
"""

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402

from .base import INK_MUTED, SERIES_BLUE, RunContext, html_page, inline_svg  # noqa: E402
from .prometheus import range_query, series  # noqa: E402

STEP_SECS = 10
RATE_WINDOW = "1m"

# The rate window spans a changing user count on a ramp, so a sample is
# attributed to the user count at the window's midpoint rather than at its
# trailing edge.
HALF_WINDOW_SECS = 30

P50_QUERY = (
    "histogram_quantile(0.5, sum by (le) ("
    "rate(locust_request_duration_milliseconds_bucket"
    '{{run_id="{run_id}",name="TTFI",status="success"}}[{window}])))'
)
USERS_QUERY = 'sum(locust_users{{run_id="{run_id}"}})'


class TTFIvsUsersReport:
    name = "ttfi-vs-users"
    filename = "ttfi-vs-users.html"

    def render(self, ctx: RunContext) -> str:
        points = self._collect(ctx)
        if not points:
            raise ValueError(f"no TTFI p50 samples for run_id={ctx.run_id}")
        return html_page(
            title="TTFI p50 vs concurrent users",
            subtitle=f"run {ctx.run_id} — {ctx.workload} on {ctx.template or 'default template'}",
            meta={
                "run_id": ctx.run_id,
                "users": ctx.users,
                "spawn rate": f"{ctx.spawn_rate:g}/s",
                "duration": ctx.duration,
                "user class": ctx.user_class,
                "load levels": len(points),
                "p50 range": "%.0f–%.0f ms" % (
                    min(v for _, v in points),
                    max(v for _, v in points),
                ),
            },
            body=self._chart(points),
        )

    def _collect(self, ctx: RunContext) -> list[tuple[int, float]]:
        """(users, mean p50 ms) with one entry per distinct user count."""
        p50 = series(
            range_query(
                ctx.prometheus_url,
                P50_QUERY.format(run_id=ctx.run_id, window=RATE_WINDOW),
                ctx.started,
                ctx.ended,
                STEP_SECS,
            )
        )
        users = series(
            range_query(
                ctx.prometheus_url,
                USERS_QUERY.format(run_id=ctx.run_id),
                ctx.started,
                ctx.ended,
                STEP_SECS,
            )
        )

        buckets: dict[int, list[float]] = {}
        for ts, value in p50.items():
            at_midpoint = users.get(ts - HALF_WINDOW_SECS)
            now = users.get(ts)
            if at_midpoint is None or now is None or at_midpoint <= 0:
                continue
            # A shrinking pool is teardown, where the window still holds the
            # run's observations but the user count no longer describes them.
            if now < at_midpoint:
                continue
            buckets.setdefault(round(at_midpoint), []).append(value)

        return sorted((u, sum(v) / len(v)) for u, v in buckets.items())

    def _chart(self, points: list[tuple[int, float]]) -> str:
        xs = [u for u, _ in points]
        ys = [v for _, v in points]

        fig, ax = plt.subplots(figsize=(8, 4.5))
        ax.plot(xs, ys, color=SERIES_BLUE, linewidth=2, marker="o", markersize=4)

        ax.set_xlabel("concurrent users", color=INK_MUTED, fontsize=11)
        ax.set_ylabel("TTFI p50 (ms)", color=INK_MUTED, fontsize=11)
        ax.set_ylim(bottom=0)
        ax.set_xlim(left=0)
        ax.grid(axis="y", color=INK_MUTED, alpha=0.25, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(colors=INK_MUTED, labelsize=10)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("bottom", "left"):
            ax.spines[side].set_color(INK_MUTED)

        try:
            return inline_svg(fig)
        finally:
            plt.close(fig)
