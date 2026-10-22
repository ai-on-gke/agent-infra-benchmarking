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
from pathlib import Path

import yaml

from . import SCENARIOS_DIR, benchmark
from .control_plane import CONTROL_PLANE, CONTROL_PLANES, control_plane
from .reports import LABEL_RE, VARIANT

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

# A case's variants, each run as a case of its own, <case>-<variant>: the
# case's keys with the variant's on top.
VARIANTS = "variants"
# A case's own keys; the rest of what it states is in the mapping of its type,
# named by the type, which only the type's runner reads, but for the cluster
# the type asks for in its `infra`.
CASE_KEYS = {REPEATS, "sandbox-class", BARE_METAL, CONTROL_PLANE, VARIANTS}


def types() -> list[str]:
    return ["noop", *benchmark.types()]


def load_cases(scenarios_file: Path) -> dict[str, dict]:
    """The cases, each with variants in their place and `type` filled in from
    the mapping that names it."""
    with scenarios_file.open() as f:
        cases = (yaml.safe_load(f) or {}).get("cases")
    if not isinstance(cases, dict) or not cases:
        raise ValueError(f"{scenarios_file} has no `cases` mapping")
    cases = with_variants(str(scenarios_file), cases)
    known = types()
    for name, case in cases.items():
        if "type" in case:
            raise ValueError(
                f"{scenarios_file}: case {name}'s type is the key of its mapping, one of {', '.join(known)}"
            )
        given = [key for key in known if key in case]
        if len(given) != 1:
            raise ValueError(f"{scenarios_file}: case {name} needs a mapping of exactly one type of {', '.join(known)}")
        unknown = case.keys() - CASE_KEYS - set(given) - {VARIANT}
        if unknown:
            raise ValueError(f"{scenarios_file}: case {name} has unknown {', '.join(sorted(unknown))}")
        case["type"] = given[0]
        # noop's holds its `infra` at most, and so may be left null.
        options = case[given[0]] if case[given[0]] is not None or given[0] != "noop" else {}
        if not isinstance(options, dict) or (given[0] == "noop" and options.keys() - {"infra"}):
            raise ValueError(
                f"{scenarios_file}: case {name}'s `{given[0]}` must be a mapping, for noop of its `infra` at most"
            )
        case[given[0]] = options
        if control_plane(case) not in CONTROL_PLANES:
            raise ValueError(
                f"{scenarios_file}: case {name}'s `{CONTROL_PLANE}` must be one of {', '.join(CONTROL_PLANES)}"
            )
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


def merged(base: dict, over: dict) -> dict:
    """base with over's keys on top, a mapping in both merged in turn."""
    out = dict(base)
    for key, value in over.items():
        out[key] = merged(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def with_variants(where: str, cases: dict) -> dict[str, dict]:
    """The cases, one with `variants` replaced by a case per variant, in their
    order, which VARIANT says it is a variant of."""
    out = {}
    for name, case in cases.items():
        if not isinstance(case, dict):
            raise ValueError(f"{where}: case {name} is not a mapping")
        if VARIANT in case:
            raise ValueError(f"{where}: case {name}'s `{VARIANT}` is the scenario-runner's own")
        if VARIANTS not in case:
            out[name] = case
            continue
        variants = case[VARIANTS]
        if not isinstance(variants, dict) or not variants:
            raise ValueError(f"{where}: case {name}'s `{VARIANTS}` must be a mapping of names to the keys they change")
        base = {key: value for key, value in case.items() if key != VARIANTS}
        for variant, keys in variants.items():
            if not isinstance(variant, str) or not LABEL_RE.fullmatch(variant):
                raise ValueError(f"{where}: case {name}'s variant {variant!r} is not a valid label value")
            keys = keys or {}
            if not isinstance(keys, dict):
                raise ValueError(f"{where}: case {name}'s variant {variant} is not a mapping")
            fixed = keys.keys() & {VARIANTS, VARIANT}
            if fixed:
                raise ValueError(f"{where}: case {name}'s variant {variant} cannot change {', '.join(sorted(fixed))}")
            renamed = f"{name}-{variant}"
            if renamed in cases or renamed in out:
                raise ValueError(f"{where}: case {name}'s variant {variant} would be {renamed}, which is another case")
            out[renamed] = {**merged(copy.deepcopy(base), keys), VARIANT: {"of": name, "name": variant}}
    return out


def grouped(cases: dict[str, dict]) -> dict[str, list[str]]:
    """The cases by the one they are variants of, a case of none by its own
    name, in the order the first of each comes."""
    groups = {}
    for name, case in cases.items():
        groups.setdefault(case[VARIANT]["of"] if VARIANT in case else name, []).append(name)
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
    runs nothing there. A variant on bare metal is one of <case>-metal, so
    variants on and off it are compared apart."""
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
        if VARIANT in case:
            group = case[VARIANT]["of"] + BARE_METAL_SUFFIX
            if group in all_names:
                raise ValueError(f"case {name} on bare metal would be a variant of {group}, which is another case")
            result[renamed][VARIANT] = {**case[VARIANT], "of": group}
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
