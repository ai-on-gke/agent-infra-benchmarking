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

"""The cases scenarios.yaml's cases make: a case of each choice of a case's
`parameters`, and a case of each of its `subcases`, with their values in its
placeholders, `{name}` in its name or any of its values."""

from __future__ import annotations

import copy
import itertools
import re

from .. import LABEL_RE

# Lists of the values a case's placeholders take: a case is made of each
# choice of one entry from every list.
PARAMETERS = "parameters"
# The values each of a case's subcases sets, run as a case of its own,
# <case>-<subcase>.
SUBCASES = "subcases"
# The scenario-runner's own key in a case made from another's subcase:
# {"of": that case's name, "name": the subcase's}.
SUBCASE = "subcase"
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_-]+)\}")
# What a placeholder set to null leaves of the key it is the value of.
DROPPED = object()


def placeholders(value: object) -> set[str]:
    """The names of the placeholders left in value."""
    if isinstance(value, dict):
        return set().union(*map(placeholders, value.values()))
    if isinstance(value, list):
        return set().union(*map(placeholders, value))
    return set(PLACEHOLDER_RE.findall(value)) if isinstance(value, str) else set()


def substituted(value: object, values: dict, where: str) -> object:
    """value with its placeholders that values names replaced, those it does
    not left: a string that is one placeholder by the value as it is, a key or
    an item set to null dropped, and a placeholder in a longer string by its
    text."""
    out = _substituted(value, values, where)
    return None if out is DROPPED else out


def _substituted(value: object, values: dict, where: str) -> object:
    if isinstance(value, dict):
        out = {key: _substituted(item, values, where) for key, item in value.items()}
        return {key: item for key, item in out.items() if item is not DROPPED}
    if isinstance(value, list):
        return [item for item in (_substituted(item, values, where) for item in value) if item is not DROPPED]
    if not isinstance(value, str):
        return value
    whole = PLACEHOLDER_RE.fullmatch(value)
    if whole and whole[1] in values:
        given = values[whole[1]]
        return DROPPED if given is None else copy.deepcopy(given)

    def text(match: re.Match) -> str:
        if match[1] not in values:
            return match[0]
        given = values[match[1]]
        if isinstance(given, bool) or not isinstance(given, (str, int, float)):
            raise ValueError(f"{where}: {match[0]} in {value!r} must be a string or a number, got {given!r}")
        return str(given)

    return PLACEHOLDER_RE.sub(text, value)


def listed(names: set[str]) -> str:
    return ", ".join("{" + name + "}" for name in sorted(names))


def with_parameters(where: str, cases: dict) -> dict:
    """The cases, one with `parameters` replaced by a case of each choice of
    one entry from every list of them, in their order. Those of its
    placeholders its subcases set are left to them."""
    out = {}
    for name, case in cases.items():
        made = parametrized(where, name, case) if isinstance(case, dict) and PARAMETERS in case else [(name, case)]
        for renamed, expanded in made:
            if renamed in out:
                raise ValueError(f"{where}: there are two cases {renamed}")
            out[renamed] = expanded
    return out


def parametrized(where: str, name: str, case: dict) -> list[tuple[str, dict]]:
    """The case made of each choice of its parameters, with the entries'
    values in its placeholders, null in those an entry does not set; each of
    its subcases is given the values of the placeholders the subcases set,
    over which its own win, and its own take the entries' in theirs."""
    parameters = case[PARAMETERS]
    if (
        not isinstance(parameters, dict)
        or not parameters
        or not all(
            isinstance(entries, list) and entries and all(isinstance(entry, dict) for entry in entries)
            for entries in parameters.values()
        )
    ):
        raise ValueError(f"{where}: case {name}'s `{PARAMETERS}` must be a mapping of names to lists of values")
    subcases = case.get(SUBCASES) if isinstance(case.get(SUBCASES), dict) else {}
    subcase_values = [values for values in subcases.values() if isinstance(values, dict)]
    # In the order the subcases give them, as each subcase is printed.
    theirs = list(dict.fromkeys(key for values in subcase_values for key in values))
    body = {key: value for key, value in case.items() if key not in (PARAMETERS, SUBCASES)}
    used = placeholders(name) | placeholders(body)
    taken = placeholders(subcase_values)
    given = set().union(*(entry for entries in parameters.values() for entry in entries))
    # Each set by none of them, or set by one and used by none, is a typo.
    unset = (used - given - set(theirs)) | (taken - given)
    if unset:
        raise ValueError(f"{where}: case {name} has {listed(unset)}, which none of its parameters or subcases set")
    unused = given - used - taken
    if unused:
        raise ValueError(f"{where}: case {name}'s parameters set {', '.join(sorted(unused))}, which it does not use")
    made = []
    for choice in itertools.product(*parameters.values()):
        values = {}
        for entry in choice:
            twice = values.keys() & entry.keys()
            if twice:
                raise ValueError(f"{where}: case {name} sets {', '.join(sorted(twice))} in more than one parameter")
            values.update(entry)
        renamed = substituted(name, values, f"case {name}")
        if not isinstance(renamed, str):
            raise ValueError(f"{where}: case {name} is named {renamed!r}, not a string")
        what = f"case {renamed}"
        mine = {p: values.get(p) for p in placeholders(body) - set(theirs)}
        expanded = {key: substituted(value, mine, what) for key, value in case.items() if key != PARAMETERS}
        if subcases:
            expanded[SUBCASES] = {
                subcase: {
                    **{key: values.get(key) for key in theirs},
                    **{key: substituted(value, values, f"{what}'s subcase {subcase}") for key, value in own.items()},
                }
                if isinstance(own, dict)
                else own
                for subcase, own in subcases.items()
            }
        made.append((renamed, expanded))
    return made


def with_subcases(where: str, cases: dict) -> dict[str, dict]:
    """The cases, one with `subcases` replaced by a case per subcase, in their
    order, with the subcase's values in its placeholders, null in those it
    does not set, which SUBCASE says it is a subcase of."""
    out = {}
    for name, case in cases.items():
        if not isinstance(case, dict):
            raise ValueError(f"{where}: case {name} is not a mapping")
        if SUBCASE in case:
            raise ValueError(f"{where}: case {name}'s `{SUBCASE}` is the scenario-runner's own")
        if SUBCASES not in case:
            out[name] = case
            continue
        subcases = case[SUBCASES]
        if not isinstance(subcases, dict) or not subcases:
            raise ValueError(f"{where}: case {name}'s `{SUBCASES}` must be a mapping of names to the values they set")
        base = {key: value for key, value in case.items() if key != SUBCASES}
        used = placeholders(base)
        unset = used - set().union(*(values for values in subcases.values() if isinstance(values, dict)))
        if unset:
            raise ValueError(f"{where}: case {name} has {listed(unset)}, which none of its subcases set")
        for subcase, values in subcases.items():
            if not isinstance(subcase, str) or not LABEL_RE.fullmatch(subcase):
                raise ValueError(f"{where}: case {name}'s subcase {subcase!r} is not a valid label value")
            values = values or {}
            if not isinstance(values, dict):
                raise ValueError(f"{where}: case {name}'s subcase {subcase} is not a mapping")
            unused = values.keys() - used
            if unused:
                raise ValueError(
                    f"{where}: case {name}'s subcase {subcase} sets {', '.join(sorted(unused))}, which it does not use"
                )
            renamed = f"{name}-{subcase}"
            if renamed in cases or renamed in out:
                raise ValueError(f"{where}: case {name}'s subcase {subcase} would be {renamed}, which is another case")
            out[renamed] = {
                **substituted(base, {**dict.fromkeys(used), **values}, f"case {renamed}"),
                SUBCASE: {"of": name, "name": subcase},
            }
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
