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

"""Chart one benchmark run: TTFI against concurrent users, read from the run's
locust_stats_history.csv and shown in a matplotlib window.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import stats_history

# Where run_locust_job.sh downloads runs to, relative to the current directory.
REPORTS_DIR = Path("reports")


def latest_run(reports_dir: Path) -> Path:
    """The newest run folder in `reports_dir`. Run ids are UTC timestamps by
    default, so the last by name is the latest."""
    if not reports_dir.is_dir():
        raise FileNotFoundError(f"no {reports_dir}/ folder in {Path.cwd()}")
    runs = sorted(p for p in reports_dir.iterdir() if p.is_dir())
    if not runs:
        raise FileNotFoundError(f"no run folders in {reports_dir.resolve()}")
    return runs[-1]


def main() -> None:
    p = argparse.ArgumentParser(prog="benchmark-report", description=__doc__)
    p.add_argument(
        "path",
        nargs="?",
        type=Path,
        help=f"A run's folder of CSVs (default: the latest one in ./{REPORTS_DIR}).",
    )
    args = p.parse_args()

    try:
        run_dir = (args.path or latest_run(REPORTS_DIR)).resolve()
        rows = stats_history.read(run_dir)
    except OSError as exc:
        sys.exit(f"benchmark-report: {exc}")

    try:
        from . import ttfi_vs_users
    except ModuleNotFoundError as exc:
        if exc.name != "matplotlib":
            raise
        sys.exit("benchmark-report needs matplotlib: pip install matplotlib")

    print(f"run: {run_dir}")
    try:
        ttfi_vs_users.show(rows, run_dir.name)
    except ValueError as exc:
        sys.exit(f"benchmark-report: {exc}")


if __name__ == "__main__":
    main()
