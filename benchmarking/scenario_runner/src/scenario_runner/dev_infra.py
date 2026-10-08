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

"""Applies, reads and destroys the dev infras: scenario_runner/infra and a
case's cluster, which the scenario-runner's --dev-infra creates and keeps, each
a Deployment in .local-data/dev-infra/terraform/<name>/<infra>/."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from . import LOCAL_DATA_DIR, substrate
from .cases import SKIP_SUBSTRATE_INSTALL
from .cluster import Cluster
from .deployment import RUNNER_INFRA_DIR, Deployment, cluster_deployment, tf_vars

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
    root = DEV_INFRA_DIR / record["name"] / infra
    variables = tf_vars(record["project_id"], record["region"], record["name"])
    if infra == "scenario-runner":
        return Deployment.create(root, RUNNER_INFRA_DIR, variables, json_output=json_output)
    version = None if record.get(SKIP_SUBSTRATE_INSTALL) else substrate.build_version()
    return cluster_deployment(record["cluster"], root, variables, version, json_output=json_output)


def apply(record: dict, json_output: bool = False) -> dict:
    """Applies every infra of the dev infra, which is complete only once all
    of them are, and returns its record. An infra created by an earlier apply
    keeps its config."""
    record = {**record, "complete": False}
    write_record(record)
    for infra in INFRAS:
        infra_deployment = deployment(record["name"], infra, json_output)
        if not infra_deployment.created:
            infra_deployment = create_deployment(record, infra, json_output)
        infra_deployment.apply()
    record["complete"] = True
    write_record(record)
    return record


def cluster(name: str) -> Cluster:
    """The dev infra's cluster, its outputs read off its state."""
    infra = deployment(name, "cluster")
    return Cluster(infra.state_outputs(), infra.data / "kubeconfig")


def install_substrate(record: dict) -> dict:
    """Installs substrate on the dev infra's cluster unless its record says it
    is, and returns its record."""
    if record.get("substrate_installed"):
        return record
    substrate.install(cluster(record["name"]))
    record = {**record, "substrate_installed": True}
    write_record(record)
    return record


def destroy(name: str, json_output: bool = False) -> None:
    for infra in reversed(INFRAS):
        infra_deployment = deployment(name, infra, json_output)
        # One never applied has nothing to destroy.
        if infra_deployment.state_file.exists():
            infra_deployment.destroy()
    shutil.rmtree(DEV_INFRA_DIR / name)
