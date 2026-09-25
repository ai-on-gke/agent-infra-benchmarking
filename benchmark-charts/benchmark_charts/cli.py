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

"""Chart one benchmark run: request latency percentiles, worker nodes' CPU
utilization and the actor counts over time, read from the run's
locust_stats_history.csv, cluster_vitals_history.csv and
custom_stats_history.csv and shown in a matplotlib window. The top panel
switches in the window between the requests in CHARTS.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import data

# Where run_locust_job.sh downloads runs to, relative to the current directory.
REPORTS_DIR = Path("reports")

# Chart name -> the request name in locust_stats_history.csv whose latency the
# chart draws above the shared panels. Every one the run has samples of is in
# the window's switch; --chart picks the one it opens on.
CHARTS = {
    "ttfi": "TTFI",
    "execution-time": "ExecutionTime",
}


def latest_run(reports_dir: Path) -> Path:
    """The newest run folder in `reports_dir`. Run ids are UTC timestamps by
    default, so the last by name is the latest."""
    if not reports_dir.is_dir():
        raise FileNotFoundError(f"no {reports_dir}/ folder in {Path.cwd()}")
    runs = sorted(p for p in reports_dir.iterdir() if p.is_dir())
    if not runs:
        raise FileNotFoundError(f"no run folders in {reports_dir.resolve()}")
    return runs[-1]


def read_optional(run_dir: Path, filename: str, part: str) -> list[dict[str, str]]:
    """A file only part of the chart needs. Reports reach Prometheus only
    best-effort, so a run can lack it; the chart then goes without that part
    rather than not at all."""
    try:
        return data.read(run_dir, filename)
    except FileNotFoundError as exc:
        print(f"benchmark-report: {exc}; drawing without {part}", file=sys.stderr)
        return []


def main() -> None:
    p = argparse.ArgumentParser(prog="benchmark-report", description=__doc__)
    p.add_argument(
        "path",
        nargs="?",
        type=Path,
        help=f"A run's folder of CSVs (default: the latest one in ./{REPORTS_DIR}).",
    )
    p.add_argument(
        "--chart",
        choices=list(CHARTS),
        default="ttfi",
        help="The top panel the window opens on (default: ttfi). The drop-down "
        "above it switches to the others.",
    )
    args = p.parse_args()

    try:
        run_dir = (args.path or latest_run(REPORTS_DIR)).resolve()
        rows = data.read(run_dir, data.STATS_HISTORY)
    except OSError as exc:
        sys.exit(f"benchmark-report: {exc}")

    vitals = read_optional(run_dir, data.CLUSTER_VITALS, "CPU")
    custom = read_optional(run_dir, data.CUSTOM_HISTORY, "actors")

    try:
        import matplotlib.pyplot as plt

        from . import latency, panels
    except ModuleNotFoundError as exc:
        if exc.name != "matplotlib":
            raise
        sys.exit("benchmark-report needs matplotlib: pip install matplotlib")

    if vitals and not panels.cpu(vitals):
        print(
            f"benchmark-report: no Pool=worker rows in {data.CLUSTER_VITALS}, as in any "
            "run from before that column; drawing without CPU",
            file=sys.stderr,
        )

    print(f"run: {run_dir}")
    mains, active = [], 0
    for chart, request_name in CHARTS.items():
        try:
            panel = latency.LatencyPanel(rows, request_name)
        except ValueError as exc:
            if chart == args.chart:
                sys.exit(f"benchmark-report: {exc}")
            print(f"benchmark-report: {exc}; leaving out {chart}", file=sys.stderr)
            continue
        if chart == args.chart:
            active = len(mains)
        mains.append(panel)
    panels.figure(mains, rows, run_dir.name, vitals, custom, active=active)
    plt.show()


if __name__ == "__main__":
    main()
