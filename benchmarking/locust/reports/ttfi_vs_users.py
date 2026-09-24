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

"""TTFI latency against concurrent users: every percentile in one HTML file,
one chart under the next.

Most useful on a ramped run (`spawn-rate` below `users` in the case), where the
ramp sweeps the user count and the run traces a load curve. A fixed-load run
produces a single point per percentile, which is still a valid file.
"""

from html import escape

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

# The percentiles a report is generated for. p50 and p99 bound the span the
# run is judged on; the two between them are where a saturating pool shows up
# first.
PERCENTILES = (50, 90, 95, 99)

QUANTILE_QUERY = (
    "histogram_quantile({quantile}, sum by (le) ("
    "rate(locust_request_duration_milliseconds_bucket"
    '{{run_id="{run_id}",name="TTFI",status="success"}}[{window}])))'
)
USERS_QUERY = 'sum(locust_users{{run_id="{run_id}"}})'


class TTFIvsUsersReport:
    name = "ttfi-vs-users"
    filename = "ttfi-vs-users.html"

    def render(self, ctx: RunContext) -> str:
        # One users series for every chart: it does not vary by percentile, and
        # querying it per chart would be four reads of the same thing.
        users = self._users(ctx)

        sections, ranges, missing = [], {}, []
        load_levels = 0
        for percentile in PERCENTILES:
            label = f"p{percentile}"
            points = self._collect(ctx, percentile, users)
            if not points:
                missing.append(label)
                continue
            load_levels = max(load_levels, len(points))
            ranges[f"{label} range"] = "%.0f–%.0f ms" % (
                min(v for _, v in points),
                max(v for _, v in points),
            )
            sections.append(self._section(label, points))

        # Every percentile reads the same buckets, so an empty result is the
        # run having no TTFI successes at all rather than a thin tail.
        if not sections:
            raise ValueError(f"no TTFI samples for run_id={ctx.run_id}")
        if missing:
            sections.append(
                f'<p class="note">No samples for {escape(", ".join(missing))}.</p>'
            )

        return html_page(
            title="TTFI vs concurrent users",
            subtitle=f"run {ctx.run_id} — {ctx.workload} on {ctx.template or 'default template'}",
            meta={
                "run_id": ctx.run_id,
                "users": ctx.users,
                "spawn rate": f"{ctx.spawn_rate:g}/s",
                "duration": ctx.duration,
                "user class": ctx.user_class,
                "load levels": load_levels,
                **ranges,
            },
            body="\n".join(sections),
        )

    def _users(self, ctx: RunContext) -> dict[float, float]:
        return series(
            range_query(
                ctx.prometheus_url,
                USERS_QUERY.format(run_id=ctx.run_id),
                ctx.started,
                ctx.ended,
                STEP_SECS,
            )
        )

    def _collect(
        self, ctx: RunContext, percentile: int, users: dict[float, float]
    ) -> list[tuple[int, float]]:
        """(users, mean latency ms) with one entry per distinct user count."""
        latency = series(
            range_query(
                ctx.prometheus_url,
                QUANTILE_QUERY.format(
                    quantile=percentile / 100,
                    run_id=ctx.run_id,
                    window=RATE_WINDOW,
                ),
                ctx.started,
                ctx.ended,
                STEP_SECS,
            )
        )

        buckets: dict[int, list[float]] = {}
        for ts, value in latency.items():
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

    def _section(self, label: str, points: list[tuple[int, float]]) -> str:
        return (
            f"<h2>TTFI {escape(label)}</h2>\n"
            f"<figure>{self._chart(label, points)}</figure>"
        )

    def _chart(self, label: str, points: list[tuple[int, float]]) -> str:
        xs = [u for u, _ in points]
        ys = [v for _, v in points]

        fig, ax = plt.subplots(figsize=(8, 4.5))
        ax.plot(xs, ys, color=SERIES_BLUE, linewidth=2, marker="o", markersize=4)

        ax.set_xlabel("concurrent users", color=INK_MUTED, fontsize=11)
        ax.set_ylabel(f"TTFI {label} (ms)", color=INK_MUTED, fontsize=11)
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
