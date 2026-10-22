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

"""Reads and checks the cases in scenarios.yaml."""

from __future__ import annotations

import copy
import itertools
import re
from pathlib import Path

import yaml

from . import SCENARIOS_DIR, benchmark
from .install import INSTALL, check_install
from .reports import LABEL_RE, SUBCASE

SCENARIOS_FILE = SCENARIOS_DIR / "scenarios.yaml"
BARE_METAL = "bare-metal"
# What a case on bare metal is recorded under: its name with this added.
BARE_METAL_SUFFIX = "-metal"
PLACEMENT_KEYS = {"zone", "machine-type"}

# How a case is sampled: `count` runs that are measured, after `warmups` that
# are not, all on one cluster applied for the case, or with `cluster:
# per-repeat` each measured run on one of its own and no warm-ups.
REPEATS = "repeats"
REPEAT_KEYS = {"count", "warmups", "cluster"}
SHARED = "shared"
PER_REPEAT = "per-repeat"
REPEAT_CLUSTERS = [SHARED, PER_REPEAT]

# A case's parameters, lists of the values its placeholders take, `{name}` in
# its name or any of its values: a case is made of each choice of one entry
# from every list.
PARAMETERS = "parameters"
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_-]+)\}")
# What a placeholder set to null leaves of the key it is the value of.
DROPPED = object()

# A case's subcases, each run as a case of its own, <case>-<subcase>: the
# case with the subcase's values in its placeholders.
SUBCASES = "subcases"
# A case's own keys; the rest of what it states is in the mapping of its type,
# named by the type, which only the type's runner reads, but for the cluster
# the type asks for in its `infra`.
CASE_KEYS = {REPEATS, BARE_METAL, SUBCASES}


def types() -> list[str]:
    return ["noop", *benchmark.types()]


def resolved(scenarios_file: Path) -> dict:
    """The scenarios file with a case made of each choice of its cases'
    parameters, which the scenario-runner runs the same."""
    with scenarios_file.open() as f:
        loaded = yaml.safe_load(f) or {}
    cases = loaded.get("cases")
    if not isinstance(cases, dict) or not cases:
        raise ValueError(f"{scenarios_file} has no `cases` mapping")
    return {**loaded, "cases": with_parameters(str(scenarios_file), cases)}


def load_cases(scenarios_file: Path) -> dict[str, dict]:
    """The cases, one made of each choice of their parameters, subcases in
    their place and `type` filled in from the mapping that names it."""
    cases = with_subcases(str(scenarios_file), resolved(scenarios_file)["cases"])
    known = types()
    for name, case in cases.items():
        unset = placeholders(name) | placeholders(case)
        if unset:
            raise ValueError(
                f"{scenarios_file}: case {name} has {', '.join('{' + p + '}' for p in sorted(unset))},"
                " which none of its parameters or subcases set"
            )
        if "type" in case:
            raise ValueError(
                f"{scenarios_file}: case {name}'s type is the key of its mapping, one of {', '.join(known)}"
            )
        given = [key for key in known if key in case]
        if len(given) != 1:
            raise ValueError(f"{scenarios_file}: case {name} needs a mapping of exactly one type of {', '.join(known)}")
        unknown = case.keys() - CASE_KEYS - set(given) - {SUBCASE}
        if unknown:
            raise ValueError(f"{scenarios_file}: case {name} has unknown {', '.join(sorted(unknown))}")
        case["type"] = given[0]
        # noop's holds its `infra` and `install` at most, and so may be left null.
        options = case[given[0]] if case[given[0]] is not None or given[0] != "noop" else {}
        if not isinstance(options, dict) or (given[0] == "noop" and options.keys() - {"infra", INSTALL}):
            raise ValueError(
                f"{scenarios_file}: case {name}'s `{given[0]}` must be a mapping,"
                f" for noop of its `infra` and `{INSTALL}` at most"
            )
        case[given[0]] = options
        try:
            check_install(options.get(INSTALL, {}))
        except ValueError as exc:
            raise ValueError(f"{scenarios_file}: case {name}'s `{given[0]}.{INSTALL}` {exc}") from None
        if not isinstance(case.get(BARE_METAL, False), bool):
            raise ValueError(f"{scenarios_file}: case {name}'s `{BARE_METAL}` must be true or false")
        try:
            # Filled in, so run.json records how the case was sampled.
            case[REPEATS] = check_repeats(case.get(REPEATS, {}))
        except ValueError as exc:
            raise ValueError(f"{scenarios_file}: case {name}'s `{REPEATS}` {exc}") from None
        try:
            if case["type"] == "noop":
                benchmark.cluster_config(name, case)
            else:
                # As each repeat runs under its own name.
                for record in warmup_names(name, case) + record_names(name, case):
                    benchmark.check(record, case)
        except ValueError as exc:
            raise ValueError(f"{scenarios_file}: {exc}") from None
    return cases


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


def placeholders(value: object) -> set[str]:
    """The names of the placeholders left in value."""
    if isinstance(value, dict):
        return set().union(*map(placeholders, value.values()))
    if isinstance(value, list):
        return set().union(*map(placeholders, value))
    return set(PLACEHOLDER_RE.findall(value)) if isinstance(value, str) else set()


def with_parameters(where: str, cases: dict) -> dict:
    """The cases, one with `parameters` replaced by a case of each choice of
    one entry from every list of them, in their order, with the entries'
    values in its placeholders. Those its subcases set are left to them, each
    subcase that does not set one given the parameter's value."""
    out = {}

    def add(name: object, case: object) -> None:
        if name in out:
            raise ValueError(f"{where}: there are two cases {name}")
        out[name] = case

    for name, case in cases.items():
        if not isinstance(case, dict) or PARAMETERS not in case:
            add(name, case)
            continue
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
        theirs = set().union(*(keys for keys in subcases.values() if isinstance(keys, dict)))
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
            expanded = {
                key: substituted(keys, {k: v for k, v in values.items() if k not in theirs}, what)
                for key, keys in case.items()
                if key != PARAMETERS
            }
            if subcases:
                expanded[SUBCASES] = {
                    subcase: (
                        {
                            **{k: values[k] for k in theirs & values.keys()},
                            **{k: substituted(v, values, f"{what}'s subcase {subcase}") for k, v in keys.items()},
                        }
                        if isinstance(keys, dict)
                        else keys
                    )
                    for subcase, keys in subcases.items()
                }
            add(renamed, expanded)
    return out


def with_subcases(where: str, cases: dict) -> dict[str, dict]:
    """The cases, one with `subcases` replaced by a case per subcase, in their
    order, with the subcase's values in its placeholders, which SUBCASE says
    it is a subcase of."""
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
        for subcase, values in subcases.items():
            if not isinstance(subcase, str) or not LABEL_RE.fullmatch(subcase):
                raise ValueError(f"{where}: case {name}'s subcase {subcase!r} is not a valid label value")
            values = values or {}
            if not isinstance(values, dict):
                raise ValueError(f"{where}: case {name}'s subcase {subcase} is not a mapping")
            renamed = f"{name}-{subcase}"
            if renamed in cases or renamed in out:
                raise ValueError(f"{where}: case {name}'s subcase {subcase} would be {renamed}, which is another case")
            what = f"case {renamed}"
            out[renamed] = {**substituted(base, values, what), SUBCASE: {"of": name, "name": subcase}}
    return out


def grouped(cases: dict[str, dict]) -> dict[str, list[str]]:
    """The cases by the one they are subcases of, a case of none by its own
    name, in the order the first of each comes."""
    groups = {}
    for name, case in cases.items():
        groups.setdefault(case[SUBCASE]["of"] if SUBCASE in case else name, []).append(name)
    return groups


def load_bare_metal(scenarios_file: Path) -> dict:
    """The scenarios file's `bare-metal`: `default`, whether the cases that do
    not say run on bare metal, false unless set, and `placements`, the zones
    and -metal machine types their sandboxed pool is tried in, in order."""
    with scenarios_file.open() as f:
        settings = (yaml.safe_load(f) or {}).get(BARE_METAL, {})
    where = f"{scenarios_file}: `{BARE_METAL}`"
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
    return {"default": settings.get("default", False), "placements": placements}


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
        metal = case.get(BARE_METAL, default if case["type"] != "noop" else False)
        if override is not None and case["type"] != "noop":
            metal = override
        if not metal:
            result[name] = {**case, BARE_METAL: False}
            continue
        renamed = name + BARE_METAL_SUFFIX
        if renamed in all_names:
            raise ValueError(f"case {name} on bare metal would be recorded as {renamed}, which is another case")
        if not placements:
            raise ValueError(f"case {name} runs on bare metal, but `{BARE_METAL}.placements` has none in its region")
        result[renamed] = {**case, BARE_METAL: True}
        if SUBCASE in case:
            group = case[SUBCASE]["of"] + BARE_METAL_SUFFIX
            if group in all_names:
                raise ValueError(f"case {name} on bare metal would be a subcase of {group}, which is another case")
            result[renamed][SUBCASE] = {**case[SUBCASE], "of": group}
    return result


def check_repeats(given: object) -> dict:
    """`repeats` with its defaults filled in; raises ValueError, its message
    what is wrong with it."""
    if not isinstance(given, dict):
        raise ValueError(f"must be a mapping of {', '.join(sorted(REPEAT_KEYS))}, got {given!r}")
    unknown = given.keys() - REPEAT_KEYS
    if unknown:
        raise ValueError(f"has unknown {', '.join(sorted(unknown))}")
    repeats = repeats_of({REPEATS: given})
    if repeats["cluster"] not in REPEAT_CLUSTERS:
        raise ValueError(f"`cluster` must be one of {', '.join(REPEAT_CLUSTERS)}, got {repeats['cluster']!r}")
    for key, least in (("count", 1), ("warmups", 0)):
        value = repeats[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < least:
            raise ValueError(f"`{key}` must be a whole number from {least}, got {value!r}")
    # A warm-up on a cluster destroyed after it warms nothing.
    if repeats["cluster"] == PER_REPEAT and repeats["warmups"]:
        raise ValueError(f"`warmups` is only for `cluster: {SHARED}`")
    return repeats


def repeats_of(case: dict) -> dict:
    """The case's `repeats`, with the defaults of what it leaves out: one
    measured run after one warm-up, on a shared cluster."""
    given = case.get(REPEATS) or {}
    cluster = given.get("cluster", SHARED)
    return {"count": given.get("count", 1), "warmups": given.get("warmups", int(cluster == SHARED)), "cluster": cluster}


def record_names(name: str, case: dict) -> list[str]:
    """What each of the case's measured runs is recorded under: <name>-1 to
    <name>-N above 1."""
    count = repeats_of(case)["count"]
    return [f"{name}-{n}" for n in range(1, count + 1)] if count > 1 else [name]


def warmup_names(name: str, case: dict) -> list[str]:
    """What each of the case's warm-ups is recorded under: <name>-warmup-1 to
    <name>-warmup-N."""
    return [f"{name}-warmup-{n}" for n in range(1, repeats_of(case)["warmups"] + 1)]


def shares_cluster(case: dict) -> bool:
    return repeats_of(case)["cluster"] == SHARED
