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
case's cluster, which the orchestrator's --dev-infra creates and keeps, each
with every Terraform file of it in .local-data/dev-infra/terraform/<name>/<infra>/."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from . import LOCAL_DATA_DIR, substrate
from .cluster import Cluster
from .deployment import (
    ORCHESTRATOR_DIR,
    Deployment,
    cluster_deployment,
    data_dir_under,
    files_under,
    state_file_under,
    tf_vars,
)

DEV_INFRA_DIR = LOCAL_DATA_DIR / "dev-infra" / "terraform"
# What a dev infra is, written before it is applied so that one whose apply
# failed can be applied again or destroyed.
RECORD_FILE = "dev-infra.json"
# In the order they are applied.
INFRAS = ("orchestrator", "cluster")


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


def state_file(name: str, infra: str) -> Path:
    return state_file_under(DEV_INFRA_DIR / name / infra)


def data_dir(name: str, infra: str) -> Path:
    return data_dir_under(DEV_INFRA_DIR / name / infra)


def read_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def deployments(record: dict, json_output: bool = False) -> dict[str, Deployment]:
    name = record["name"]
    variables = tf_vars(record["project_id"], record["region"], name)

    def files(infra: str) -> dict:
        return files_under(DEV_INFRA_DIR / name / infra, f"dev_infra_{name}", json_output)

    return {
        "orchestrator": Deployment(dir=ORCHESTRATOR_DIR, plan_args=variables, **files("orchestrator")),
        "cluster": cluster_deployment(record["cluster"], tf_vars=variables, **files("cluster")),
    }


def apply(record: dict, json_output: bool = False) -> dict:
    """Applies every infra of the dev infra, which is complete only once all
    of them are, and returns its record."""
    record = {**record, "complete": False}
    write_record(record)
    for deployment in deployments(record, json_output).values():
        deployment.apply()
    record["complete"] = True
    write_record(record)
    return record


def cluster(name: str) -> Cluster:
    """The dev infra's cluster, its outputs read off its state."""
    outputs = read_state(state_file(name, "cluster")).get("outputs", {})
    return Cluster({key: out["value"] for key, out in outputs.items()}, data_dir(name, "cluster") / "kubeconfig")


def install_substrate(record: dict) -> dict:
    """Installs substrate on the dev infra's cluster unless its record says it
    is, and returns its record."""
    if record.get("substrate_installed"):
        return record
    substrate.install(cluster(record["name"]))
    record = {**record, "substrate_installed": True}
    write_record(record)
    return record


def destroy(record: dict, json_output: bool = False) -> None:
    for infra, deployment in reversed(deployments(record, json_output).items()):
        # One never applied has nothing to destroy.
        if state_file(record["name"], infra).exists():
            deployment.destroy()
    shutil.rmtree(DEV_INFRA_DIR / record["name"])
