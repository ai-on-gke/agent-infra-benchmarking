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

"""Which cases run their sandboxed pool on bare metal, and where: a case's
`bare-metal`, and the scenarios file's settings for the cases that do not say."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..benchmarks import noop
from .subcases import SUBCASE

BARE_METAL = "bare-metal"
# What a case on bare metal is recorded under: its name with this added.
SUFFIX = "-metal"
PLACEMENT_KEYS = {"zone", "machine-type"}


@dataclass(frozen=True)
class Settings:
    """The scenarios file's `bare-metal`: default, whether the cases that do
    not say run on bare metal, and placements, the zones and -metal machine
    types their sandboxed pool is tried in, in order."""

    default: bool = False
    placements: list[dict] = field(default_factory=list)

    def placements_in(self, region: str) -> list[dict]:
        return [p for p in self.placements if p["zone"].startswith(f"{region}-")]


def check_settings(where: str, settings: object) -> Settings:
    where = f"{where}: `{BARE_METAL}`"
    if not isinstance(settings, dict) or settings.keys() - {"default", "placements"}:
        raise ValueError(f"{where} must be a mapping of default and placements")
    if not isinstance(settings.get("default", False), bool):
        raise ValueError(f"{where}.default must be true or false")
    placements = settings.get("placements", [])
    if not isinstance(placements, list) or not all(
        isinstance(p, dict) and p.keys() == PLACEMENT_KEYS and all(isinstance(v, str) for v in p.values())
        for p in placements
    ):
        raise ValueError(f"{where}.placements must be a list of zone and machine-type")
    for p in placements:
        if not p["machine-type"].endswith("-metal"):
            raise ValueError(f"{where}.placements: {p['machine-type']} is not a bare-metal (-metal) machine type")
    return Settings(settings.get("default", False), placements)


def check_case(case: dict) -> None:
    if not isinstance(case.get(BARE_METAL, False), bool):
        raise ValueError(f"`{BARE_METAL}` must be true or false")


def with_bare_metal(
    cases: dict[str, dict], all_names: set[str], default: bool, override: bool | None, placements: list[dict]
) -> dict[str, dict]:
    """The cases, each one that runs on bare metal renamed <name>-metal, as
    its records and reports are; its cluster goes on placements when applied.
    A case's own `bare-metal` wins over default, and override, unless None,
    over both. Neither default nor override puts a noop on bare metal, as it
    runs nothing there. A subcase on bare metal is one of <case>-metal, so
    subcases on and off it are compared apart."""
    result = {}
    for name, case in cases.items():
        metal = case.get(BARE_METAL, default if case["type"] != noop.TYPE else False)
        if override is not None and case["type"] != noop.TYPE:
            metal = override
        if not metal:
            result[name] = {**case, BARE_METAL: False}
            continue
        renamed = name + SUFFIX
        if renamed in all_names:
            raise ValueError(f"case {name} on bare metal would be recorded as {renamed}, which is another case")
        if not placements:
            raise ValueError(f"case {name} runs on bare metal, but `{BARE_METAL}.placements` has none in its region")
        result[renamed] = {**case, BARE_METAL: True}
        if SUBCASE in case:
            group = case[SUBCASE]["of"] + SUFFIX
            if group in all_names:
                raise ValueError(f"case {name} on bare metal would be a subcase of {group}, which is another case")
            result[renamed][SUBCASE] = {**case[SUBCASE], "of": group}
    return result
