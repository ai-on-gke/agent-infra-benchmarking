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

from pathlib import Path

import yaml

from . import SCENARIOS_DIR, benchmark

SCENARIOS_FILE = SCENARIOS_DIR / "scenarios.yaml"
# One Terraform config per directory, which a case's infra.cluster.name picks.
CLUSTERS_DIR = SCENARIOS_DIR / "infra" / "clusters"
CLUSTER_KEYS = {"name", "tfvars", "tfvars_inline", "variables"}
SKIP_SUBSTRATE_INSTALL = "skip-substrate-install"
PROVIDER = "provider"
PROVIDER_SUBSTRATE = "agent-substrate"
PROVIDER_AGENT_SANDBOX = "agent-sandbox"
PROVIDERS = [PROVIDER_SUBSTRATE, PROVIDER_AGENT_SANDBOX]
DEFAULT_PROVIDER = PROVIDER_SUBSTRATE
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

def load_cases(scenarios_file: Path) -> dict[str, dict]:
    with scenarios_file.open() as f:
        cases = (yaml.safe_load(f) or {}).get("cases")
    if not isinstance(cases, dict) or not cases:
        raise ValueError(f"{scenarios_file} has no `cases` mapping")
    for name, case in cases.items():
        if not isinstance(case, dict):
            raise ValueError(f"{scenarios_file}: case {name} is not a mapping")
        if not isinstance(case.get("type"), str):
            raise ValueError(f"{scenarios_file}: case {name} has no `type`")
        known = ["noop", *benchmark.types()]
        if case["type"] not in known:
            raise ValueError(f"{scenarios_file}: case {name}'s `type` must be one of {', '.join(known)}")
        if not isinstance(case.get(SKIP_SUBSTRATE_INSTALL, False), bool):
            raise ValueError(f"{scenarios_file}: case {name}'s `{SKIP_SUBSTRATE_INSTALL}` must be true or false")
        if case["type"] != "locust" and PROVIDER in case:
            raise ValueError(f"{scenarios_file}: case {name}'s `{PROVIDER}` is only for locust cases")
        if case["type"] == "locust":
            provider = case.get(PROVIDER, DEFAULT_PROVIDER)
            if provider not in PROVIDERS:
                raise ValueError(f"{scenarios_file}: case {name}'s `{PROVIDER}` must be one of {', '.join(PROVIDERS)}")
            case[PROVIDER] = provider
            if provider == PROVIDER_AGENT_SANDBOX:
                # Nothing installs a microvm runtime without substrate.
                if case.get("sandbox-class", "gvisor") != "gvisor":
                    raise ValueError(
                        f"{scenarios_file}: case {name}'s `sandbox-class` must be gvisor for `{PROVIDER}`"
                        f" {PROVIDER_AGENT_SANDBOX}"
                    )
                case[SKIP_SUBSTRATE_INSTALL] = True
        if not isinstance(case.get(BARE_METAL, False), bool):
            raise ValueError(f"{scenarios_file}: case {name}'s `{BARE_METAL}` must be true or false")
        try:
            # Filled in, so run.json records how the case was sampled.
            case[REPEATS] = check_repeats(case.get(REPEATS, {}))
        except ValueError as exc:
            raise ValueError(f"{scenarios_file}: case {name}'s `{REPEATS}` {exc}") from None
        check_cluster(f"{scenarios_file}: case {name}", case)
        if case["type"] != "noop":
            try:
                # As each repeat runs under its own name.
                for record in warmup_names(name, case) + record_names(name, case):
                    benchmark.check(record, case)
            except ValueError as exc:
                raise ValueError(f"{scenarios_file}: {exc}") from None
    return cases


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


def placement_variables(placement: dict) -> dict:
    """The cluster config's variables that put its sandboxed pool there."""
    return {"zone": placement["zone"], "sandboxed_machine_type": placement["machine-type"]}


def placed(cluster: dict, placement: dict) -> dict:
    """The infra.cluster spec with its sandboxed pool at placement."""
    return {**cluster, "variables": {**cluster.get("variables", {}), **placement_variables(placement)}}


def with_bare_metal(
    cases: dict[str, dict], all_names: set[str], default: bool, override: bool | None, placements: list[dict]
) -> dict[str, dict]:
    """The cases, each one that runs on bare metal renamed <name>-metal, as
    its records and reports are, with its cluster at the first of placements.
    A case's own `bare-metal` wins over default, and override, unless None,
    over both. Neither default nor override puts a noop on bare metal, as it
    runs nothing there."""
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
        infra = {**case["infra"], "cluster": placed(case["infra"]["cluster"], placements[0])}
        result[renamed] = {**case, BARE_METAL: True, "infra": infra}
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


def check_cluster(where: str, case: dict) -> None:
    infra = case.get("infra")
    cluster = infra.get("cluster") if isinstance(infra, dict) else None
    if not isinstance(cluster, dict):
        raise ValueError(f"{where} has no `infra.cluster` mapping")
    unknown = cluster.keys() - CLUSTER_KEYS
    if unknown:
        raise ValueError(f"{where}: `infra.cluster` has unknown {', '.join(sorted(unknown))}")

    clusters = sorted(p.name for p in CLUSTERS_DIR.iterdir() if p.is_dir())
    if cluster.get("name") not in clusters:
        raise ValueError(f"{where}: `infra.cluster.name` must be one of {', '.join(clusters)}")
    tfvars = cluster.get("tfvars")
    # An absolute path stays as it is; a relative one is in the cluster's config.
    if tfvars is not None and not (isinstance(tfvars, str) and (CLUSTERS_DIR / cluster["name"] / tfvars).is_file()):
        raise ValueError(f"{where}: `infra.cluster.tfvars` {tfvars!r} is not a file")
    if not isinstance(cluster.get("tfvars_inline", ""), str):
        raise ValueError(f"{where}: `infra.cluster.tfvars_inline` must be a string")
    if not isinstance(cluster.get("variables", {}), dict):
        raise ValueError(f"{where}: `infra.cluster.variables` must be a mapping")
