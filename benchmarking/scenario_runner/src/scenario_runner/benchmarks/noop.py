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

"""The scenario-runner's own type: a case that applies and destroys its
cluster and runs nothing on it. Its `noop` holds its `infra` and `install` at
most."""

from __future__ import annotations

from ..infra.cluster_configs import CLUSTER_CONFIGS, ClusterConfig, from_infra
from ..scenarios.install import INSTALL

TYPE = "noop"
KEYS = {"infra", INSTALL}
# It pulls none of the benchmark's images.
IMAGE_FILES = []


def missing_requirements() -> list[str]:
    return []


def check_case(name: str, case: dict, deploy_workloads: bool) -> dict:
    """The case, as it has no defaults."""
    options = case.get(TYPE)
    if not isinstance(options, dict):
        raise ValueError(f"case {name}'s `{TYPE}` must be a mapping of its `infra` and `{INSTALL}` at most")
    unknown = options.keys() - KEYS
    if unknown:
        raise ValueError(f"case {name}'s `{TYPE}` has unknown {', '.join(sorted(unknown))}; it holds its `infra` and"
                         f" `{INSTALL}` at most")  # fmt: skip
    cluster_config(name, case)
    return case


def cluster_config(name: str, case: dict) -> ClusterConfig:
    """Any configuration its `noop.infra.cluster.configuration` picks."""
    return from_infra(name, f"{TYPE}.infra", case[TYPE].get("infra"), tuple(CLUSTER_CONFIGS.values()))
