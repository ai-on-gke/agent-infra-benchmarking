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

"""The cases an execution runs, of those scenarios.yaml has, with what the
scenario-runner's options override in them."""

from __future__ import annotations

from .. import benchmarks
from ..benchmarks import sandbox_benchmark
from .bare_metal import BARE_METAL, with_bare_metal
from .cases import check_records
from .expand import grouped
from .file import Scenarios


def select(
    scenarios: Scenarios,
    names: list[str] | None = None,
    suite: str | None = None,
    *,
    tests: list[str] | None = None,
    bare_metal: bool | None = None,
    placements: list[dict] = (),
) -> dict[str, dict]:
    """The cases to run, by name, in the order they run: those names gives,
    a case's subcases in its place, or else those suite lists, or else every
    one. tests is the ai-sandbox-benchmark tests every sandbox-benchmark case
    runs, and bare_metal, unless None, whether every case runs on bare metal,
    on placements. Raises ValueError, its message what is wrong with them."""
    cases = scenarios.cases
    groups = grouped(cases)
    if suite is not None:
        if suite not in scenarios.suites:
            raise ValueError(f"no suite {suite} in {scenarios.path}; it has {', '.join(scenarios.suites) or 'none'}")
        names = scenarios.suites[suite]
    # dict.fromkeys drops repeated names, keeping the first.
    names = list(dict.fromkeys(cases if names is None else names))
    unknown = [name for name in names if name not in cases and name not in groups]
    if unknown:
        known = ", ".join(dict.fromkeys([*groups, *cases]))
        from_suite = f" (from suite {suite})" if suite else ""
        raise ValueError(f"no case {', '.join(unknown)}{from_suite} in {scenarios.path}; it has {known}")
    selected = {member: cases[member] for name in names for member in groups.get(name, [name])}
    selected = {name: overridden(name, case, tests) for name, case in selected.items()}
    if tests:
        others = [name for name, case in selected.items() if case["type"] != sandbox_benchmark.TYPE]
        if others:
            raise ValueError(f"--test is only for sandbox-benchmark cases, not {', '.join(others)}")
        for name, case in selected.items():
            benchmarks.check(name, case)
    selected = with_bare_metal(selected, set(cases), scenarios.bare_metal.default, bare_metal, list(placements))
    # As they are recorded under other names on bare metal.
    for name, case in selected.items():
        if case[BARE_METAL]:
            check_records(str(scenarios.path), name, case)
    return selected


def overridden(name: str, case: dict, tests: list[str] | None) -> dict:
    kind = case["type"]
    if kind == sandbox_benchmark.TYPE and tests:
        tests = list(dict.fromkeys(t if t.startswith("test_") else f"test_{t}" for t in tests))
        case = {**case, kind: {**case[kind], "tests": tests}}
    return case
