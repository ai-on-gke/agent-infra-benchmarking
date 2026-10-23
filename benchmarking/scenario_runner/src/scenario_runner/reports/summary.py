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

"""A case's repeats summarized from their downloaded reports: the min, median
and max of every steady-state stat across the repeats that passed, as
<case>-summary.csv and <case>-summary.md beside their folders."""

from __future__ import annotations

import csv
import importlib.util
import json
import statistics
from pathlib import Path

from .. import RUNNER_DIR
from ..log import log

STEADY_STATS = "locust_stats_steady.csv"
RUN_JSON = "run.json"
# Not stats: the row's key, a duplicate of 50%, and a size that is never weighed.
SKIPPED = {"Type", "Name", "Median Response Time", "Average Content Size"}
COLUMNS = ["Type", "Name", "Stat", "Repeats", "Min", "Median", "Max"]
# The master image's renderer, which writes each repeat's README.md too, so a
# summary reads like the READMEs it summarizes.
README_PY = RUNNER_DIR.parent / "locust" / "master-image" / "readme.py"


def read(run_dir: Path, records: list[str]) -> list[tuple[str, dict | None, str | None]]:
    """Each repeat's folder name, its run.json, and why it is left out of the
    summary, or None if it is not."""
    out = []
    for record in records:
        job = run_dir / record
        try:
            info = json.loads((job / RUN_JSON).read_text())
        except (OSError, ValueError):
            info = None
        if not isinstance(info, dict):
            out.append((record, None, f"no readable {RUN_JSON}"))
            continue
        status = info.get("status")
        if status != 0:
            reason = f"it failed with status {status}"
        elif not (job / STEADY_STATS).is_file():
            reason = f"no {STEADY_STATS}"
        else:
            reason = None
        out.append((record, info, reason))
    return out


def summarize(run_dir: Path, jobs: list[tuple[str, dict | None, str | None]]) -> list[list[str]]:
    """The summary's rows, over the repeats read did not leave out."""
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
    # locust's order, which a name only a later repeat has would put after the
    # Aggregated rows.
    ordered = sorted(values.items(), key=lambda kv: kv[0][1] == "Aggregated")
    return [
        [type_, name, stat, str(len(vs)), _num(min(vs)), _num(statistics.median(vs)), _num(max(vs))]
        for (type_, name, stat), vs in ordered
    ]


def _num(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _readme():
    spec = importlib.util.spec_from_file_location("readme", README_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(run_dir: Path, case: str, records: list[str]) -> None:
    """Writes the case's summary into run_dir. Best-effort: each repeat's own
    reports are the record either way."""
    if not any((run_dir / record).is_dir() for record in records):
        log(f"{case}: no summary, as none of its repeats left reports")
        return
    try:
        jobs = read(run_dir, records)
        for name, _, reason in jobs:
            if reason:
                log(f"{case}: {name} left out of the summary: {reason}", "WARNING")
        rows = summarize(run_dir, jobs)
        with (run_dir / f"{case}-summary.csv").open("w", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(COLUMNS)
            writer.writerows(rows)
        (run_dir / f"{case}-summary.md").write_text(_readme().render_summary(case, jobs, rows))
    except Exception as exc:
        log(f"{case}: no summary of its repeats: {type(exc).__name__}: {exc}", "WARNING")
        return
    log(f"{case}: summary of {len(records)} repeats in {run_dir / f'{case}-summary.md'}")
