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

"""The configurations of the infra/clusters/ configs, each as what may be
asked of it: its inputs, which the `infra.cluster` in a case's type gives in
kebab-case. Only these turn them into the config's Terraform variables, with
what the execution decides of every cluster it applies. A configuration's
subclass is the cluster of its parent's and more, so a case of the parent's
runs on it too."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import ClassVar, Self

from .. import SCENARIOS_DIR
from .deployment import Deployment

CLUSTERS_DIR = SCENARIOS_DIR / "infra" / "clusters"
# Set by automation-infra's Cloud Run job: the account its clusters' nodes run
# as, which its deployment created with the project roles nodes need, so the
# job neither creates one per run nor grants it those.
NODE_SERVICE_ACCOUNT = os.environ.get("NODE_SERVICE_ACCOUNT")
# Set by automation-infra's Cloud Run job, which runs inside this network: its
# clusters go into the subnetwork, with an internal endpoint only.
CLUSTER_NETWORK = os.environ.get("CLUSTER_NETWORK")
CLUSTER_SUBNETWORK = os.environ.get("CLUSTER_SUBNETWORK")

# The pool for mock-llm, which its pods select and tolerate
# (workloads/manifests/mock-llm.yaml.tmpl).
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


def execution(named: dict, image_repository: str | None = None, substrate_version: str | None = None) -> Execution:
    """The Execution of named, with what automation-infra's Cloud Run job sets."""
    network = {"network": CLUSTER_NETWORK, "subnetwork": CLUSTER_SUBNETWORK} if CLUSTER_SUBNETWORK else None
    return Execution(named, image_repository, substrate_version, NODE_SERVICE_ACCOUNT, network)


@dataclass(frozen=True)
class ClusterConfig:
    """A cluster of one of the configs, with its sandboxed pool: pinned at
    sandboxed_nodes, or autoscaling up to sandboxed_max_nodes."""

    # What infra.cluster.configuration names it by.
    CONFIGURATION: ClassVar[str]
    CONFIG: ClassVar[Path]
    # The fields a case's infra.cluster may give.
    INPUTS: ClassVar[tuple[str, ...]] = (
        "sandboxed_nodes", "sandboxed_max_nodes", "sandboxed_machine_type", "zone", "nested_virtualization"
    )

    sandboxed_nodes: int = 1
    sandboxed_max_nodes: int | None = None
    sandboxed_machine_type: str = "c2-standard-16"
    zone: str | None = None
    # /dev/kvm off bare metal, which microvm's guests boot on.
    nested_virtualization: bool = False

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
        if not isinstance(self.nested_virtualization, bool):
            raise ValueError(f"`nested-virtualization` must be true or false, got {self.nested_virtualization!r}")

    @classmethod
    def from_yaml(cls, given: dict | None) -> Self:
        """The cluster a case's infra.cluster gives the inputs of; raises
        ValueError, its message what is wrong."""
        given = {} if given is None else given
        if not isinstance(given, dict):
            raise ValueError(f"must be a mapping of {', '.join(map(kebab, cls.INPUTS))}, got {given!r}")
        unknown = given.keys() - set(map(kebab, cls.INPUTS))
        if unknown:
            raise ValueError(
                f"has unknown {', '.join(sorted(map(str, unknown)))}; it takes {', '.join(map(kebab, cls.INPUTS))}"
            )
        return cls(**{key.replace("-", "_"): value for key, value in given.items()})

    @property
    def sandboxed_kvm(self) -> bool:
        """Whether the sandboxed pool has /dev/kvm, as the config's
        sandboxed_kvm output says: nested or on bare metal."""
        return self.nested_virtualization or self.sandboxed_machine_type.endswith("-metal")

    @property
    def name(self) -> str:
        """The config's directory in infra/clusters/."""
        return self.CONFIG.name

    def placed(self, placement: dict) -> Self:
        """The cluster with its sandboxed pool at a bare-metal placement."""
        return replace(self, zone=placement["zone"], sandboxed_machine_type=placement["machine-type"])

    def sandboxed_pool(self) -> dict:
        variables = {
            "nested_virtualization": self.nested_virtualization,
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
    """infra/clusters/common: a GKE cluster and its sandboxed pool for one
    sandbox class."""

    CONFIGURATION: ClassVar[str] = "common"
    CONFIG: ClassVar[Path] = CLUSTERS_DIR / "common"
    INPUTS: ClassVar[tuple[str, ...]] = (*ClusterConfig.INPUTS, "default_machine_type")

    # The config's own default, c2-standard-16, unless set.
    default_machine_type: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.default_machine_type is not None and not (
            isinstance(self.default_machine_type, str) and self.default_machine_type
        ):
            raise ValueError(f"`default-machine-type` must be a non-empty string, got {self.default_machine_type!r}")

    def variables(self, execution: Execution) -> dict:
        variables = {**self.sandboxed_pool(), "bucket_force_destroy": True}
        if self.default_machine_type:
            variables["default_machine_type"] = self.default_machine_type
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
class OpenclawCluster(CommonCluster):
    """common's cluster with a pool for mock-llm, which the openclaw actors
    call, so it shares no node with the control plane."""

    CONFIGURATION: ClassVar[str] = "openclaw"

    def variables(self, execution: Execution) -> dict:
        return {**super().variables(execution), "additional_node_pools": [MOCK_LLM_POOL]}


@dataclass(frozen=True)
class DummyCluster(ClusterConfig):
    """infra/clusters/dummy: the scenario-runner's Terraform path, which
    creates nothing in GCP."""

    CONFIGURATION: ClassVar[str] = "dummy"
    CONFIG: ClassVar[Path] = CLUSTERS_DIR / "dummy"


# What infra.cluster.configuration picks, common by default.
CLUSTER_CONFIGS: dict[str, type[ClusterConfig]] = {
    config.CONFIGURATION: config for config in (CommonCluster, OpenclawCluster, DummyCluster)
}


def kebab(name: str) -> str:
    return name.replace("_", "-")


def from_infra(
    case: str,
    path: str,
    infra: object,
    configs: tuple[type[ClusterConfig], ...] = (CommonCluster,),
) -> ClusterConfig:
    """The cluster the `infra` at path in the case asks for: its `cluster`'s
    inputs of the one of configs its `configuration` picks, common unless it
    says. Raises ValueError, its message what is wrong."""
    where = f"case {case}'s `{path}"
    infra = {} if infra is None else infra
    if not isinstance(infra, dict) or infra.keys() - {"cluster"}:
        raise ValueError(f"{where}` must be a mapping of `cluster`, the inputs of the cluster it runs on")
    given = infra.get("cluster")
    allowed = {config.CONFIGURATION: config for config in configs}
    name = given.get("configuration", "common") if isinstance(given, dict) else "common"
    config = allowed.get(name) if isinstance(name, str) else None
    if config is None:
        raise ValueError(f"{where}.cluster.configuration` must be one of {', '.join(allowed)}, got {name!r}")
    if isinstance(given, dict):
        given = {key: value for key, value in given.items() if key != "configuration"}
    try:
        return config.from_yaml(given)
    except ValueError as exc:
        raise ValueError(f"{where}.cluster`: {exc}") from None


def to_record(config: ClusterConfig) -> dict:
    """The cluster as a dev infra's record keeps it: the name of its config,
    its configuration and its fields."""
    return {
        "name": config.name,
        "configuration": config.CONFIGURATION,
        **{f.name: getattr(config, f.name) for f in fields(config)},
    }


def from_record(record: dict) -> ClusterConfig:
    """The cluster of to_record's record; raises ValueError for one it did not
    write, such as a dev infra's from before the clusters were these."""
    values = dict(record)
    values.pop("name", None)
    # Recorded before the case's class became nested-virtualization's input.
    if "sandbox_class" in values:
        values["nested_virtualization"] = values.pop("sandbox_class") == "microvm"
    config = CLUSTER_CONFIGS.get(values.pop("configuration", None))
    try:
        if config is None:
            raise TypeError
        return config(**values)
    except TypeError:
        raise ValueError(f"not a cluster of {', '.join(CLUSTER_CONFIGS)}: {record}") from None
