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

"""Per-run report files.

Adding a report is one module implementing base.Report plus an entry in
_REPORTS. runner.py selects them by name with --report.
"""

from pathlib import Path

from .base import Report, RunContext
from .cluster_vitals import ClusterVitalsReport
from .custom_history import CustomHistoryReport
from .stats_csv import StatsCSVReport
from .upload import DEFAULT_PREFIX, UploadError, upload

_REPORTS: dict[str, Report] = {
    r.name: r
    for r in (
        StatsCSVReport(),
        CustomHistoryReport(),
        ClusterVitalsReport(),
    )
}

__all__ = [
    "DEFAULT_PREFIX",
    "RunContext",
    "UploadError",
    "generate",
    "names",
    "upload",
]


def names() -> list[str]:
    return sorted(_REPORTS)


def resolve(selection: list[str]) -> list[Report]:
    """Reports named by `selection`; "all" selects every registered one."""
    if "all" in selection:
        return [_REPORTS[n] for n in names()]
    unknown = [n for n in selection if n not in _REPORTS]
    if unknown:
        raise KeyError(f"unknown report(s) {', '.join(unknown)}; known: {', '.join(names())}")
    return [_REPORTS[n] for n in selection]


def generate(
    selection: list[str], ctx: RunContext, out_dir: Path
) -> list[tuple[str, Path | None, str | None]]:
    """Write each selected report. Returns (name, path, error) per report;
    one failing report does not stop the others, because a report is a
    by-product of the run and never its verdict."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for report in resolve(selection):
        path = out_dir / report.filename
        try:
            path.write_text(report.render(ctx), encoding="utf-8")
        except Exception as exc:
            results.append((report.name, None, str(exc)))
        else:
            results.append((report.name, path, None))
    return results
