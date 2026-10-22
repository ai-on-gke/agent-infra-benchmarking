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

"""The infra/clusters/ configs, each as what may be asked of it: its inputs,
which a case's infra.cluster gives in kebab-case, and what the case's type
sets itself. Only these turn them into the config's Terraform variables, with
what the execution decides of every cluster it applies."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, Self

from .cases import CLUSTERS_DIR
from .deployment import Deployment

# What mock-llm's pods select and tolerate (workloads/manifests/mock-llm.yaml.tmpl).
MOCK_LLM_POOL = {
    "name": "mock-llm",
    "machine_type": "e2-standard-8",
    "labels": {"benchmarking-mock-llm": "true"},
    "taints": [{"key": "benchmarking-mock-llm", "value": "true", "effect": "NO_SCHEDULE"}],
}


@dataclass(frozen=True)
class Execution:
    """What the execution, or a dev infra, decides of every cluster it
    applies, whichever case it is for."""

    # names.infra_vars' or names.dev_infra_vars': the project, region, names and labels.
    named: dict
    # The repository the cluster pulls its images from; None for none.
    image_repository: str | None = None
    # The substrate build its nodes are labeled with; None without substrate.
    substrate_version: str | None = None
    # Set on automation-infra's Cloud Run job: the account its nodes run as,
    # and the network, {"network", "subnetwork"}, its clusters go into with an
    # internal endpoint only.
    node_service_account: str | None = None
    network: dict | None = None


@dataclass(frozen=True)
class ClusterConfig:
    """A cluster of one of the configs, with its sandboxed pool: pinned at
    sandboxed_nodes, or autoscaling up to sandboxed_max_nodes."""

    CONFIG: ClassVar[Path]
    # The fields a case's infra.cluster may give; the rest its type sets.
    INPUTS: ClassVar[tuple[str, ...]] = ("sandboxed_nodes", "sandboxed_max_nodes", "sandboxed_machine_type", "zone")

    sandboxed_nodes: int = 1
    sandboxed_max_nodes: int | None = None
    sandboxed_machine_type: str = "c2-standard-16"
    zone: str | None = None
    sandbox_class: str = "gvisor"

    def __post_init__(self) -> None:
        for name in ("sandboxed_nodes", "sandboxed_max_nodes"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
                raise ValueError(f"`{kebab(name)}` must be a whole number from 1, got {value!r}")
        if self.sandboxed_max_nodes is not None and self.sandboxed_max_nodes < self.sandboxed_nodes:
            raise ValueError(
                f"`sandboxed-max-nodes` {self.sandboxed_max_nodes} is less than `sandboxed-nodes` {self.sandboxed_nodes}"
            )
        for name in ("sandboxed_machine_type", "zone"):
            value = getattr(self, name)
            if value is not None and not (isinstance(value, str) and value):
                raise ValueError(f"`{kebab(name)}` must be a non-empty string, got {value!r}")

    @classmethod
    def from_yaml(cls, given: dict | None, **from_type) -> Self:
        """The cluster a case's infra.cluster gives the inputs of, with what
        its type sets; raises ValueError, its message what is wrong."""
        given = {} if given is None else given
        if not isinstance(given, dict):
            raise ValueError(f"must be a mapping of {', '.join(map(kebab, cls.INPUTS))}, got {given!r}")
        unknown = given.keys() - set(map(kebab, cls.INPUTS))
        if unknown:
            raise ValueError(
                f"has unknown {', '.join(sorted(map(str, unknown)))}; it takes {', '.join(map(kebab, cls.INPUTS))}"
            )
        return cls(**{key.replace("-", "_"): value for key, value in given.items()}, **from_type)

    def placed(self, placement: dict) -> Self:
        """The cluster with its sandboxed pool at a bare-metal placement."""
        return replace(self, zone=placement["zone"], sandboxed_machine_type=placement["machine-type"])

    def sandboxed_pool(self) -> dict:
        variables = {
            "sandbox_class": self.sandbox_class,
            "sandboxed_machine_type": self.sandboxed_machine_type,
            "sandboxed_node_count": self.sandboxed_nodes,
            "sandboxed_min_node_count": self.sandboxed_nodes,
            "sandboxed_max_node_count": self.sandboxed_max_nodes or self.sandboxed_nodes,
        }
        return {**variables, "zone": self.zone} if self.zone else variables

    def variables(self, execution: Execution) -> dict:
        """The config's Terraform variables."""
        return {**self.sandboxed_pool(), **execution.named}

    def deployment(self, uncreated: Deployment, execution: Execution) -> Deployment:
        """Creates uncreated as a deployment of the config."""
        return uncreated.create(self.CONFIG, self.variables(execution))


@dataclass(frozen=True)
class CommonCluster(ClusterConfig):
    """infra/clusters/common: a GKE cluster, its sandboxed pool for one
    sandbox class, and with mock_llm_pool a pool for mock-llm."""

    CONFIG: ClassVar[Path] = CLUSTERS_DIR / "common"

    mock_llm_pool: bool = False

    def variables(self, execution: Execution) -> dict:
        variables = {**self.sandboxed_pool(), "bucket_force_destroy": True}
        if self.mock_llm_pool:
            variables["additional_node_pools"] = [MOCK_LLM_POOL]
        if execution.image_repository is not None:
            variables["image_repository"] = execution.image_repository
        if execution.substrate_version is not None:
            variables["substrate_version"] = execution.substrate_version
        if execution.node_service_account:
            variables["node_service_account"] = execution.node_service_account
        if execution.network:
            variables["existing_network"] = execution.network
            variables["enable_private_endpoint"] = True
        return {**variables, **execution.named}


@dataclass(frozen=True)
class DummyCluster(ClusterConfig):
    """infra/clusters/dummy: the scenario-runner's Terraform path, which
    creates nothing in GCP."""

    CONFIG: ClassVar[Path] = CLUSTERS_DIR / "dummy"


# What a noop's infra.cluster.name picks.
CLUSTER_CONFIGS: dict[str, type[ClusterConfig]] = {"common": CommonCluster, "dummy": DummyCluster}


def kebab(name: str) -> str:
    return name.replace("_", "-")
