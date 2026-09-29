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
utilization and the actor counts over time, read from each job's
locust_stats_history.csv, cluster_vitals_history.csv and
custom_stats_history.csv and shown in a matplotlib window. A job is one test
case of the run; drop-downs in the window switch between the run's jobs, and
between the requests in CHARTS.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import data

# Where run_locust_job.sh downloads runs to, relative to the current directory.
REPORTS_DIR = Path("reports")

# Chart name -> the request name in locust_stats_history.csv whose latency the
# chart draws above the shared panels, and the title the window shows it by.
# Every one a job has samples of is in the window's switch; --chart picks the
# one it opens on.
CHARTS = {
    "ttfi": ("TTFI", "time to first instruction"),
    "execution-time": ("ExecutionTime", "ExecutionTime"),
}


def nth_run(reports_dir: Path, index: int = -1) -> Path:
    """The run folder in `reports_dir` at `index` by name, as a list is
    indexed: -1 the latest, -2 the one before it, 0 the oldest. Run ids are a
    UTC timestamp by default, so by name is by time."""
    if not reports_dir.is_dir():
        raise FileNotFoundError(f"no {reports_dir}/ folder in {Path.cwd()}")
    runs = sorted(p for p in reports_dir.iterdir() if p.is_dir())
    if not runs:
        raise FileNotFoundError(f"no run folders in {reports_dir.resolve()}")
    try:
        return runs[index]
    except IndexError:
        raise FileNotFoundError(
            f"no run {index} in {reports_dir.resolve()}, which has {len(runs)}: "
            f"from {-len(runs)} to {len(runs) - 1}"
        ) from None


def run_arg(arg: str) -> int | Path:
    """A run's index, or a path."""
    try:
        return int(arg)
    except ValueError:
        return Path(arg)


def job_dirs(path: Path) -> list[Path]:
    """The run's jobs, one folder per test case; or `path` alone when it holds
    the CSVs itself: one job's folder, or a run from before runs had jobs."""
    if (path / data.STATS_HISTORY).is_file():
        return [path]
    jobs = sorted(p for p in path.iterdir() if (p / data.STATS_HISTORY).is_file()) if path.is_dir() else []
    if not jobs:
        raise FileNotFoundError(f"no {data.STATS_HISTORY} in {path} or in any folder in it")
    return jobs


def title(job_dir: Path) -> str:
    """<run-id>/<test-case> when the job is under ./reports."""
    try:
        return job_dir.resolve().relative_to(REPORTS_DIR.resolve()).as_posix()
    except ValueError:
        return job_dir.name


def read_optional(job_dir: Path, filename: str, part: str) -> list[dict[str, str]]:
    """A file only part of the chart needs. Reports reach Prometheus only
    best-effort, so a job can lack it; the chart then goes without that part
    rather than not at all."""
    try:
        return data.read(job_dir, filename)
    except FileNotFoundError as exc:
        print(f"benchmark-report: {exc}; drawing without {part}", file=sys.stderr)
        return []


def load(job_dir: Path):
    from . import latency, panels, window

    rows = data.read(job_dir, data.STATS_HISTORY)
    vitals = read_optional(job_dir, data.CLUSTER_VITALS, "CPU")
    custom = read_optional(job_dir, data.CUSTOM_HISTORY, "actors")
    if vitals and not panels.cpu(vitals):
        print(
            f"benchmark-report: no Pool=worker rows in {job_dir / data.CLUSTER_VITALS}, as "
            "in any run from before that column; drawing without CPU",
            file=sys.stderr,
        )

    mains = []
    for chart, (request_name, chart_title) in CHARTS.items():
        try:
            mains.append(latency.LatencyPanel(rows, request_name, chart_title))
        except ValueError as exc:
            print(f"benchmark-report: {job_dir.name}: {exc}; leaving out {chart}", file=sys.stderr)
    if not mains:
        names = " or ".join(name for name, _ in CHARTS.values())
        raise ValueError(f"no {names} samples in {data.STATS_HISTORY}")
    return window.Job(title(job_dir), mains, rows, vitals, custom)


def main() -> None:
    p = argparse.ArgumentParser(prog="benchmark-report", description=__doc__)
    p.add_argument(
        "run",
        nargs="?",
        type=run_arg,
        default=-1,
        metavar="RUN",
        help=f"A run's index among the runs in ./{REPORTS_DIR} by name, as a "
        "Python list is indexed: -1 the latest, -2 the one before it, 0 the "
        f"oldest. Or a run's folder, {REPORTS_DIR}/<run-id>, or one job's, "
        f"{REPORTS_DIR}/<run-id>/<test-case> (default: -1).",
    )
    p.add_argument(
        "--job",
        metavar="TEST_CASE",
        help="The job the window opens on (default: the first by name). The "
        "drop-down above the chart switches to the others.",
    )
    p.add_argument(
        "--chart",
        choices=list(CHARTS),
        default="ttfi",
        help="The top panel the window opens on, where a job has it (default: "
        "ttfi). The drop-down above it switches to the others.",
    )
    args = p.parse_args()

    try:
        run_dir = args.run if isinstance(args.run, Path) else nth_run(REPORTS_DIR, args.run)
        jobs = {d.name: d for d in job_dirs(run_dir)}
    except OSError as exc:
        sys.exit(f"benchmark-report: {exc}")
    if args.job and args.job not in jobs:
        sys.exit(f"benchmark-report: no job {args.job!r} in {run_dir}; it has {', '.join(jobs)}")

    try:
        import matplotlib.pyplot as plt

        from . import window
    except ModuleNotFoundError as exc:
        if exc.name != "matplotlib":
            raise
        sys.exit("benchmark-report needs matplotlib: pip install matplotlib")

    print(f"run: {run_dir.resolve()}")
    win = window.Window(jobs, load, args.job or next(iter(jobs)), CHARTS[args.chart][1])
    # With no other job to switch to, the error already printed is all there is.
    if len(jobs) == 1 and not win.drawn:
        sys.exit(1)
    plt.show()


if __name__ == "__main__":
    main()
