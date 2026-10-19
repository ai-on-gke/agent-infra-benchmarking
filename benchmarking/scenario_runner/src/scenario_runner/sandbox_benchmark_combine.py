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
comparison ai-sandbox-benchmark's comparator prints for a run of them all:
results.json, every run of every provider, and comparison.md, the
comparator's tables. sandbox_benchmark.run combines its providers' runs in
the bucket; combine, runs already downloaded.

The comparator runs every provider's tests at once and reports only each
provider's averages, so its runs of different providers are not paired and
separate runs combine into the same tables. They were not measured at the
same time, though, which comparison.md's sources table shows."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from .log import log

RESULTS_JSON = "results.json"
RUN_JSON = "run.json"
COMPARISON_MD = "comparison.md"
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


def test_table(sources: list[dict], test: str) -> list[list[str]]:
    """The comparator's performance table of the test, as it computes it: of
    every run, failed ones too, each metric's mean, averaged, ± their
    population deviation; then the mean total time, and each provider's
    against the first's."""
    rows = []
    of_test = {s["name"]: [r for r in s["runs"] if r["test"] == test] for s in sources}
    for metric in TABLE_METRICS:
        row = [metric]
        for name, runs in of_test.items():
            means = [statistics.fmean(r["metrics"][metric]) for r in runs if r["metrics"].get(metric)]
            row.append(f"{statistics.fmean(means):.2f}ms (±{statistics.pstdev(means):.2f})" if means else "N/A")
        rows.append(row)
    totals = {name: mean([total_time(r) for r in runs]) for name, runs in of_test.items()}
    rows.append(["Total Time"] + [f"{t:.2f}ms" if t is not None else "N/A" for t in totals.values()])
    base = sources[0]["name"]
    vs = []
    for name, total in totals.items():
        if name == base or not totals[base]:
            vs.append("0%")
        elif total is not None:
            vs.append(f"{(total - totals[base]) / totals[base] * 100:+.1f}%")
        else:
            vs.append("N/A")
    rows.append([f"vs {base} %"] + vs)
    return rows


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


def source_record(source: dict) -> dict:
    """Which run the source was, and when."""
    info = source["info"]
    return {
        "name": source["name"],
        "provider": source["provider"],
        **{key: info.get(key) for key in ("run_id", "record", "status", "started", "ended")},
    }


def render(sources: list[dict]) -> str:
    info = sources[0]["info"]
    names = [s["name"] for s in sources]
    records = [source_record(s) for s in sources]
    out = [
        "# Sandbox benchmark comparison\n",
        (
            f"Tests: {', '.join(info['tests'])}; {info['runs']} runs, {info['warmup_runs']} warmup;"
            f" ai-sandbox-benchmark {info.get('ai_sandbox_benchmark_commit')}.\n"
        ),
        "## Sources\n",
        markdown_table(
            ["Name", "Provider", "Run", "Record", "Status", "Started", "Ended"],
            [[str(value) for value in record.values()] for record in records],
        ),
    ]
    for test in info["tests"]:
        out += [f"## {test}\n", markdown_table(["Metric"] + names, test_table(sources, test))]
        # The comparator counts only the errors a provider raises; these are all of them.
        failed = {s["name"]: sum(1 for r in s["runs"] if r["test"] == test and r["errors"]) for s in sources}
        if any(failed.values()):
            out.append(
                markdown_table(
                    ["Provider", f"Failed Runs (out of {info['runs']})"],
                    [[n, f"{f}/{info['runs']}"] for n, f in failed.items()],
                )
            )
        else:
            out.append("No failures recorded for this test.\n")
    return "\n".join(out)


def combined_results(sources: list[dict]) -> dict:
    return {
        "providers": [s["name"] for s in sources],
        "sources": [source_record(s) for s in sources],
        **{key: sources[0]["info"].get(key) for key in SAME},
        "runs": [{"provider": s["name"], **run} for s in sources for run in s["runs"]],
    }


def combine(folders: list[Path], out_dir: Path, names: list[str] | None = None) -> None:
    """Combines the folders, each a run's downloaded reports, into out_dir.
    The first is the one the others are compared with; names, if given, are
    theirs in the tables, in their order."""
    sources = read(folders, names)
    check(sources)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / RESULTS_JSON).write_text(json.dumps(combined_results(sources), indent=2) + "\n")
    (out_dir / COMPARISON_MD).write_text(render(sources))
    log(f"sandbox-benchmark: {len(sources)} runs combined in {out_dir / COMPARISON_MD}")
