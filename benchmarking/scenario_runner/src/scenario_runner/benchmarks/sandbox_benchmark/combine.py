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

"""Several sandbox-benchmark runs, each of one provider, combined into the
comparison ai-sandbox-benchmark's comparator prints for a run of them all, as
perf_results.csv: its metrics' mean, P90 and deviation, to import into Sheets.
sandbox_benchmark.summarize combines a case's subcases' runs; combine, runs
already downloaded.

The comparator runs every provider's tests at once and reports only each
provider's averages, so its runs of different providers are not paired and
separate runs combine into the same tables. They were not measured at the
same time, though, which each run's run.json shows."""

from __future__ import annotations

import csv
import io
import json
import statistics
from pathlib import Path

from ...log import log

RESULTS_JSON = "results.json"
RUN_JSON = "run.json"
PERF_RESULTS_CSV = "perf_results.csv"
# What the sources must agree on to be one run's.
SAME = ("tests", "runs", "warmup_runs", "ai_sandbox_benchmark_commit")
# The comparator's table rows, and the metrics its total time adds up.
TABLE_METRICS = ["Workspace Creation", "Suspend", "Resume", "Code Execution", "Cleanup"]
TOTAL_METRICS = [
    "Workspace Creation", "Suspend", "Resume", "Dependency Installation", "Environment Setup", "Code Execution",
    "Cleanup",
]  # fmt: skip


class CombineError(RuntimeError):
    pass


def source(name: str, results: dict, info: dict, where: str) -> dict:
    """A run's results.json and run.json, under the name it has in the
    tables; where says where they were read from."""
    return {"name": name, "where": where, "info": info, "provider": results["provider"], "runs": results["runs"]}


def read(folders: list[Path], names: list[str] | None = None) -> list[dict]:
    """Each folder's run, under its name: its provider, or the one names gives
    it, as two runs of the same provider need."""
    if names is not None and len(names) != len(folders):
        raise CombineError(f"{len(names)} names for {len(folders)} folders")
    out = []
    for i, folder in enumerate(folders):
        try:
            results = json.loads((folder / RESULTS_JSON).read_text())
            info = json.loads((folder / RUN_JSON).read_text())
        except (OSError, ValueError) as exc:
            raise CombineError(f"{folder}: {exc}") from None
        out.append(source(names[i] if names else results["provider"], results, info, str(folder)))
    return out


def check(sources: list[dict]) -> None:
    """Raises CombineError for sources that are not one run's."""
    seen = [s["name"] for s in sources]
    if len(set(seen)) != len(seen):
        raise CombineError(f"sources share a name ({', '.join(seen)}); name them apart")
    first = sources[0]
    for other in sources[1:]:
        for key in SAME:
            if other["info"].get(key) != first["info"].get(key):
                raise CombineError(
                    f"{other['where']} has {key} {other['info'].get(key)!r}, {first['where']} {first['info'].get(key)!r}"
                )


def mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def total_time(run: dict) -> float:
    """The comparator's total time of a run: the sum of its TOTAL_METRICS'
    means."""
    return sum(statistics.fmean(run["metrics"][m]) for m in TOTAL_METRICS if run["metrics"].get(m))


def against_first(totals: list[float | None]) -> list[str]:
    """Each mean total time against the first's, as the comparator has it."""
    base = totals[0]
    return [
        "0%" if i == 0 or not base else f"{(total - base) / base * 100:+.1f}%" if total is not None else "N/A"
        for i, total in enumerate(totals)
    ]


def stats(values: list[float]) -> list[str]:
    """Mean, P90 and population deviation, as the sheet's columns have them."""
    if not values:
        return ["N/A"] * 3
    p90 = statistics.quantiles(values, n=10, method="inclusive")[-1] if len(values) > 1 else values[0]
    return [f"{v:.2f}" for v in (statistics.fmean(values), p90, statistics.pstdev(values))]


def perf_results(sources: list[dict]) -> list[list[str]]:
    """The perf results sheet's rows: for each test, of every run, failed ones
    too, each metric's mean, P90 and deviation of the runs' means, then the
    total time's and the mean total against the first's; and beside the first
    test the configuration."""
    info = sources[0]["info"]
    names = [s["name"] for s in sources]
    summary = [
        "Test Configuration Summary", "",
        f"Warmup Runs: {info['warmup_runs']}",
        f"Measurement Runs: {info['runs']}",
        f"Tests Used ({len(info['tests'])}): {', '.join(info['tests'])}",
        f"Providers Used: {', '.join(s['provider'] for s in sources)}",
        f"Run: {info.get('run_id')}",
        f"ai-sandbox-benchmark: {(info.get('ai_sandbox_benchmark_commit') or '')[:7]}",
    ]  # fmt: skip
    rows = []
    for test in info["tests"]:
        of_test = {s["name"]: [r for r in s["runs"] if r["test"] == test] for s in sources}
        block = [
            [test] + [cell for name in names for cell in (name, "", "")],
            ["Metric"] + ["Mean (ms)", "P90 (ms)", "Std Dev (ms)"] * len(names),
        ]
        for metric in TABLE_METRICS:
            block.append(
                [metric]
                + [
                    cell
                    for runs in of_test.values()
                    for cell in stats([statistics.fmean(r["metrics"][metric]) for r in runs if r["metrics"].get(metric)])
                ]
            )
        block.append(["Total Time"] + [cell for runs in of_test.values() for cell in stats([total_time(r) for r in runs])])
        vs = against_first([mean([total_time(r) for r in runs]) for runs in of_test.values()])
        block.append([f"vs {names[0]} %"] + [cell for v in vs for cell in (v, "", "")])
        if not rows:
            side = [""] + summary
            block += [[""] * len(block[0]) for _ in range(len(side) - len(block))]
            block = [row + ["", side[i] if i < len(side) else ""] for i, row in enumerate(block)]
        rows += block + [[]]
    return rows[:-1]


def csv_text(rows: list[list[str]]) -> str:
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return out.getvalue()


def combine(folders: list[Path], out_dir: Path, names: list[str] | None = None) -> None:
    """Combines the folders, each a run's downloaded reports, into out_dir.
    The first is the one the others are compared with; names, if given, are
    theirs in the tables, in their order."""
    sources = read(folders, names)
    check(sources)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / PERF_RESULTS_CSV).write_text(csv_text(perf_results(sources)))
    log(f"sandbox-benchmark: {len(sources)} runs combined in {out_dir / PERF_RESULTS_CSV}")
