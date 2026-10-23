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

"""Reads and checks scenarios.yaml: its cases, its suites and its bare-metal
settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .. import SCENARIOS_DIR
from . import bare_metal
from .bare_metal import BARE_METAL
from .cases import load_cases
from .expand import with_parameters

SCENARIOS_FILE = SCENARIOS_DIR / "scenarios.yaml"
CASES = "cases"
SUITES = "suites"


@dataclass(frozen=True)
class Scenarios:
    path: Path
    # The file with a case made of each choice of its cases' parameters,
    # which the scenario-runner runs the same; subcases are still in it.
    resolved: dict
    # Every case by name, its subcases in its place, checked.
    cases: dict[str, dict]
    # Lists of case names, each as --scenario takes them, which --suite runs.
    suites: dict[str, list[str]]
    bare_metal: bare_metal.Settings


def load_scenarios(path: Path) -> Scenarios:
    """Raises OSError, yaml.YAMLError or ValueError, its message what is
    wrong with the file."""
    where = str(path)
    with path.open() as f:
        loaded = yaml.safe_load(f) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{where} must be a mapping of {CASES}, {SUITES} and {BARE_METAL}")
    cases = loaded.get(CASES)
    if not isinstance(cases, dict) or not cases:
        raise ValueError(f"{where} has no `{CASES}` mapping")
    resolved = {**loaded, CASES: with_parameters(where, cases)}
    return Scenarios(
        path=path,
        resolved=resolved,
        cases=load_cases(where, resolved[CASES]),
        suites=check_suites(where, loaded.get(SUITES, {})),
        bare_metal=bare_metal.check_settings(where, loaded.get(BARE_METAL, {})),
    )


def check_suites(where: str, suites: object) -> dict[str, list[str]]:
    if not isinstance(suites, dict) or not all(
        isinstance(names, list) and names and all(isinstance(n, str) for n in names) for names in suites.values()
    ):
        raise ValueError(f"{where}: `{SUITES}` must be a mapping of non-empty lists of case names")
    return suites
