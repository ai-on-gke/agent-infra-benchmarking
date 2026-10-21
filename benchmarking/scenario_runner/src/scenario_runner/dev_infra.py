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

"""Applies, reads and destroys the dev infras: scenario_runner/infra, the
repository the dev infra's images are built into, and a case's cluster, which
the scenario-runner's --dev-infra creates and keeps, each a Deployment in
.local-data/dev-infra/terraform/<name>/<infra>/."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from . import LOCAL_DATA_DIR, agent_sandbox, names, substrate
from .cases import SKIP_SUBSTRATE_INSTALL
from .cluster import Cluster
from .deployment import RUNNER_INFRA_DIR, VARIABLES_FILE, Deployment, cluster_deployment

DEV_INFRA_DIR = LOCAL_DATA_DIR / "dev-infra" / "terraform"
# What a dev infra is, written before it is applied so that one whose apply
# failed can be applied again or destroyed.
RECORD_FILE = "dev-infra.json"
# In the order they are applied.
INFRAS = ("scenario-runner", "cluster")


def check_name(name: str) -> str:
    # Also its resources' name, so what GCP takes as one.
    if not re.fullmatch(r"[a-z][-a-z0-9]*", name):
        raise argparse.ArgumentTypeError(f"{name!r}: use lowercase letters, digits and hyphens, starting with a letter")
    return name


def record_file(name: str) -> Path:
    return DEV_INFRA_DIR / name / RECORD_FILE


def read_record(name: str) -> dict | None:
    path = record_file(name)
    return json.loads(path.read_text()) if path.exists() else None


def write_record(record: dict) -> None:
    path = record_file(record["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n")


def deployment(name: str, infra: str, json_output: bool = False) -> Deployment:
    """The deployment of the dev infra's infra, created or not."""
    return Deployment(DEV_INFRA_DIR / name / infra, json_output)


def create_deployment(record: dict, infra: str, json_output: bool = False) -> Deployment:
    """Creates the deployment of the record's infra, its config copied from
    the source as it is now."""
    uncreated = deployment(record["name"], infra, json_output)
    variables = names.dev_infra_vars(record["project_id"], record["region"], record["name"], infra == "cluster")
    if infra == "scenario-runner":
        return uncreated.create(RUNNER_INFRA_DIR, variables)
    version = None if record.get(SKIP_SUBSTRATE_INSTALL) else substrate.build_version()
    # Applied before the cluster is created.
    repository = runner_outputs(record["name"])["artifact_registry_repository"]
    return cluster_deployment(record["cluster"], uncreated, variables, version, repository, record.get("sandbox_class"))


def runner_outputs(name: str) -> dict:
    """The outputs of the dev infra's scenario_runner/infra, read off its state."""
    return deployment(name, "scenario-runner").state_outputs()


def substrate_version(name: str) -> str | None:
    """The substrate version the dev infra's cluster config labels its nodes
    with, None before it is created or when it skips substrate."""
    variables = deployment(name, "cluster").config / VARIABLES_FILE
    return json.loads(variables.read_text()).get("substrate_version") if variables.exists() else None


def apply(record: dict, json_output: bool = False, update: bool = False) -> dict:
    """Applies every infra of the dev infra, which is complete only once all
    of them are, and returns its record. An infra created by an earlier apply
    keeps its config unless update, which copies it from the source again."""
    record = {**record, "complete": False}
    installed_version = substrate_version(record["name"])
    write_record(record)
    for infra in INFRAS:
        infra_deployment = deployment(record["name"], infra, json_output)
        if update or not infra_deployment.created:
            infra_deployment = create_deployment(record, infra, json_output)
        infra_deployment.apply()
    # The nodes now carry a version the installed dataplane is not, so no
    # worker would schedule until substrate is installed again.
    if substrate_version(record["name"]) != installed_version:
        record.pop("substrate_installed", None)
    record["complete"] = True
    write_record(record)
    return record


def cluster(name: str) -> Cluster:
    """The dev infra's cluster, its outputs read off its state."""
    infra = deployment(name, "cluster")
    return Cluster(infra.state_outputs(), infra.data / "kubeconfig")


def install_substrate(record: dict) -> dict:
    """Installs substrate on the dev infra's cluster from the images in its
    repository unless its record says it is installed, and returns its
    record."""
    if record.get("substrate_installed"):
        return record
    substrate.install(cluster(record["name"]))
    record = {**record, "substrate_installed": True}
    write_record(record)
    return record


def build_agent_sandbox_images(record: dict, cloud_build: bool = False, login: bool = True) -> dict:
    version = agent_sandbox.build_version()
    if record.get("agent_sandbox_images") == version:
        return record
    agent_sandbox.build_images(runner_outputs(record["name"])["docker_repo"], record["project_id"], cloud_build, login)
    record = {**record, "agent_sandbox_images": version}
    write_record(record)
    return record


def install_agent_sandbox(record: dict, cloud_build: bool = False, login: bool = True) -> dict:
    if record.get("agent_sandbox_installed"):
        return record
    record = build_agent_sandbox_images(record, cloud_build, login)
    agent_sandbox.install(cluster(record["name"]))
    record = {**record, "agent_sandbox_installed": True}
    write_record(record)
    return record


def destroy(name: str, json_output: bool = False) -> None:
    for infra in reversed(INFRAS):
        infra_deployment = deployment(name, infra, json_output)
        # One never applied has nothing to destroy.
        if infra_deployment.state_file.exists():
            if infra == "cluster" and infra_deployment.state_outputs().get("cluster_name"):
                cluster(name).delete_volumes()
            infra_deployment.destroy()
    shutil.rmtree(DEV_INFRA_DIR / name)
