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

"""The cases scenarios.yaml's cases make of their `parameters`: a case of each
choice of them, with their values in its placeholders, `{name}` in its name or
any of its keys or values, its subcases' too."""

from __future__ import annotations

import copy
import itertools
import re

# Lists of the values a case's placeholders take: a case is made of each
# choice of one entry from every list.
PARAMETERS = "parameters"
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_-]+)\}")
# What a placeholder set to null leaves of the key it is the value of.
DROPPED = object()


def placeholders(value: object) -> set[str]:
    """The names of the placeholders left in value."""
    if isinstance(value, dict):
        return set().union(*map(placeholders, value.keys()), *map(placeholders, value.values()))
    if isinstance(value, list):
        return set().union(*map(placeholders, value))
    return set(PLACEHOLDER_RE.findall(value)) if isinstance(value, str) else set()


def substituted(value: object, values: dict, where: str) -> object:
    """value with its placeholders that values names replaced, those it does
    not left: a string that is one placeholder by the value as it is, a key or
    an item set to null dropped, and a placeholder in a longer string or in a
    key by its text."""
    out = _substituted(value, values, where)
    return None if out is DROPPED else out


def _substituted(value: object, values: dict, where: str) -> object:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            item = _substituted(item, values, where)
            if item is DROPPED:
                continue
            key = _text(key, values, where) if isinstance(key, str) else key
            if key in out:
                raise ValueError(f"{where}: two keys are {key!r}")
            out[key] = item
        return out
    if isinstance(value, list):
        return [item for item in (_substituted(item, values, where) for item in value) if item is not DROPPED]
    if not isinstance(value, str):
        return value
    whole = PLACEHOLDER_RE.fullmatch(value)
    if whole and whole[1] in values:
        given = values[whole[1]]
        return DROPPED if given is None else copy.deepcopy(given)
    return _text(value, values, where)


def _text(value: str, values: dict, where: str) -> str:
    """value with each placeholder that values names replaced by its text."""

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
    one entry from every list of them, in their order, none with a
    placeholder left."""
    out = {}
    for name, case in cases.items():
        if not isinstance(case, dict):
            raise ValueError(f"{where}: case {name} is not a mapping")
        made = parametrized(where, name, case) if PARAMETERS in case else [(name, case)]
        for renamed, expanded in made:
            if renamed in out:
                raise ValueError(f"{where}: there are two cases {renamed}")
            unset = placeholders(renamed) | placeholders(expanded)
            if unset:
                raise ValueError(f"{where}: case {renamed} has {listed(unset)}, which none of its parameters set")
            out[renamed] = expanded
    return out


def parametrized(where: str, name: str, case: dict) -> list[tuple[str, dict]]:
    """The case made of each choice of its parameters, with the entries'
    values in its placeholders, its subcases' too, null in those an entry
    does not set and another does."""
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
    body = {key: value for key, value in case.items() if key != PARAMETERS}
    given = set().union(*(entry for entries in parameters.values() for entry in entries))
    unused = given - placeholders(name) - placeholders(body)
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
        made.append((renamed, substituted(body, {p: values.get(p) for p in given}, f"case {renamed}")))
    return made
