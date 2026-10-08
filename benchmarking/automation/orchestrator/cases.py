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

"""Reads and checks the cases in tests.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

from . import TESTS_DIR, benchmark

TESTS_FILE = TESTS_DIR / "tests.yaml"
# One Terraform config per directory, which a case's infra.cluster.name picks.
CLUSTERS_DIR = TESTS_DIR / "infra" / "clusters"
CLUSTER_KEYS = {"name", "tfvars", "tfvars_inline", "variables"}
SKIP_SUBSTRATE_INSTALL = "skip-substrate-install"


def load_cases(tests_file: Path) -> dict[str, dict]:
    with tests_file.open() as f:
        cases = (yaml.safe_load(f) or {}).get("cases")
    if not isinstance(cases, dict) or not cases:
        raise ValueError(f"{tests_file} has no `cases` mapping")
    for name, case in cases.items():
        if not isinstance(case, dict):
            raise ValueError(f"{tests_file}: case {name} is not a mapping")
        if not isinstance(case.get("type"), str):
            raise ValueError(f"{tests_file}: case {name} has no `type`")
        known = ["noop", *benchmark.types()]
        if case["type"] not in known:
            raise ValueError(f"{tests_file}: case {name}'s `type` must be one of {', '.join(known)}")
        if not isinstance(case.get(SKIP_SUBSTRATE_INSTALL, False), bool):
            raise ValueError(f"{tests_file}: case {name}'s `{SKIP_SUBSTRATE_INSTALL}` must be true or false")
        check_cluster(f"{tests_file}: case {name}", case)
        if case["type"] != "noop":
            try:
                benchmark.check(name, case)
            except ValueError as exc:
                raise ValueError(f"{tests_file}: {exc}") from None
    return cases


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
