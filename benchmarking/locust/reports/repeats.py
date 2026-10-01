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

"""A case's repeats summarized: the min, median and max of every steady-state
stat across them, on stdout as CSV or as a README.

    python -m reports.repeats [--format csv|md] <run-dir> <case> <repeats>

Reads <run-dir>/<case>-<n>/locust_stats_steady.csv for each repeat whose
run.json has status 0; the others are left out, and named. Repeats is how many
repeats a value was found in, which is fewer than were used where a repeat had
no requests of that name.
"""

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

from . import readme

STEADY_STATS = "locust_stats_steady.csv"
RUN_JSON = "run.json"

# Not stats: the row's key, a duplicate of 50%, and a size that is never weighed.
SKIPPED = {"Type", "Name", "Median Response Time", "Average Content Size"}

COLUMNS = ["Type", "Name", "Stat", "Repeats", "Min", "Median", "Max"]


def read(run_dir: Path, case: str, repeats: int) -> list[tuple[str, dict | None, str | None]]:
    """Each repeat's folder name, its run.json, and why it is left out of the
    summary, or None if it is not."""
    out = []
    for n in range(1, repeats + 1):
        job = run_dir / f"{case}-{n}"
        try:
            info = json.loads((job / RUN_JSON).read_text())
        except (OSError, ValueError):
            info = None
        if not isinstance(info, dict):
            out.append((job.name, None, f"no readable {RUN_JSON}"))
            continue
        status = info.get("status")
        if status != 0:
            reason = f"it failed with status {status}"
        elif not (job / STEADY_STATS).is_file():
            reason = f"no {STEADY_STATS}"
        else:
            reason = None
        out.append((job.name, info, reason))
    return out


def summarize(run_dir: Path, jobs: list[tuple[str, dict | None, str | None]]) -> list[list[str]]:
    """The summary's rows, over the repeats `read` did not leave out."""
    values: dict[tuple[str, str, str], list[float]] = {}
    for name, _, reason in jobs:
        if reason:
            continue
        with (run_dir / name / STEADY_STATS).open(newline="") as f:
            for row in csv.DictReader(f):
                for stat, cell in row.items():
                    if stat in SKIPPED:
                        continue
                    try:
                        value = float(cell)
                    except (TypeError, ValueError):
                        continue  # locust's N/A
                    values.setdefault((row["Type"], row["Name"], stat), []).append(value)
    # locust's order, which a name only a later repeat has would put after
    # the Aggregated rows.
    ordered = sorted(values.items(), key=lambda kv: kv[0][1] == "Aggregated")
    return [
        [type_, name, stat, str(len(vs)), _num(min(vs)), _num(statistics.median(vs)), _num(max(vs))]
        for (type_, name, stat), vs in ordered
    ]


def _num(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m reports.repeats", description=__doc__)
    p.add_argument("--format", choices=("csv", "md"), default="csv")
    p.add_argument("run_dir", type=Path)
    p.add_argument("case")
    p.add_argument("repeats", type=int)
    args = p.parse_args()

    jobs = read(args.run_dir, args.case, args.repeats)
    rows = summarize(args.run_dir, jobs)
    if args.format == "md":
        sys.stdout.write(readme.render_summary(args.case, jobs, rows))
        return

    for name, _, reason in jobs:
        if reason:
            print(f"{name}: left out, {reason}", file=sys.stderr)
    if not rows:
        sys.exit(f"none of {args.case}'s {args.repeats} repeats can be summarized")
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(rows)


if __name__ == "__main__":
    main()
