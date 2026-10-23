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


"""The cases a case's `subcases` make, once it is made of its parameters: a
case of each, with the subcase's keys merged on top, and the cases by the one
they are subcases of."""

from __future__ import annotations

import copy

from .. import LABEL_RE

# The keys each of a case's subcases changes, run as a case of its own,
# <case>-<subcase>.
SUBCASES = "subcases"
# The scenario-runner's own key in a case made from another's subcase:
# {"of": that case's name, "name": the subcase's}.
SUBCASE = "subcase"


def merged(base: dict, over: dict) -> dict:
    """base with over's keys on top, a mapping in both merged in turn, and
    those over has null dropped."""
    out = dict(base)
    for key, value in over.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merged(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def with_subcases(where: str, cases: dict) -> dict[str, dict]:
    """The cases, made of their parameters already, one with `subcases`
    replaced by a case per subcase, in their order: the case with the
    subcase's keys merged on top, which SUBCASE says it is a subcase of."""
    out = {}
    for name, case in cases.items():
        if SUBCASE in case:
            raise ValueError(f"{where}: case {name}'s `{SUBCASE}` is the scenario-runner's own")
        if SUBCASES not in case:
            out[name] = case
            continue
        subcases = case[SUBCASES]
        if not isinstance(subcases, dict) or not subcases:
            raise ValueError(f"{where}: case {name}'s `{SUBCASES}` must be a mapping of names to the keys they change")
        base = {key: value for key, value in case.items() if key != SUBCASES}
        for subcase, keys in subcases.items():
            if not isinstance(subcase, str) or not LABEL_RE.fullmatch(subcase):
                raise ValueError(f"{where}: case {name}'s subcase {subcase!r} is not a valid label value")
            keys = keys or {}
            if not isinstance(keys, dict):
                raise ValueError(f"{where}: case {name}'s subcase {subcase} is not a mapping")
            fixed = keys.keys() & {SUBCASES, SUBCASE}
            if fixed:
                raise ValueError(f"{where}: case {name}'s subcase {subcase} cannot change {', '.join(sorted(fixed))}")
            renamed = f"{name}-{subcase}"
            if renamed in cases or renamed in out:
                raise ValueError(f"{where}: case {name}'s subcase {subcase} would be {renamed}, which is another case")
            out[renamed] = {**merged(copy.deepcopy(base), keys), SUBCASE: {"of": name, "name": subcase}}
    return out


def grouped(cases: dict[str, dict]) -> dict[str, list[str]]:
    """The cases by the one they are subcases of, a case of none by its own
    name, in the order the first of each comes."""
    groups = {}
    for name, case in cases.items():
        groups.setdefault(case[SUBCASE]["of"] if SUBCASE in case else name, []).append(name)
    return groups


def group_record(record: str, key: str, case: dict) -> str:
    """What a record of the subcase named key is in its group: the name
    of the case it is a subcase of, with the record's repeat or warm-up
    suffix."""
    return case[SUBCASE]["of"] + record.removeprefix(key)
