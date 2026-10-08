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

"""Lists or destroys the dev infras: the clusters the orchestrator's
--dev-infra creates and keeps, each with every Terraform file of it in
.dev-infra/terraform/<name>/."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

from . import AUTOMATION_DIR
from .cluster import Cluster
from .log import log

DEV_INFRA_DIR = AUTOMATION_DIR / ".dev-infra" / "terraform"
# What a dev infra is, written before it is applied so that one whose apply
# failed can still be destroyed.
RECORD_FILE = "cluster.json"


def check_name(name: str) -> str:
    # Also the cluster's resource name, so what GCP takes as one.
    if not re.fullmatch(r"[a-z][-a-z0-9]*", name):
        raise argparse.ArgumentTypeError(f"{name!r}: use lowercase letters, digits and hyphens, starting with a letter")
    return name


def record_file(name: str) -> Path:
    return DEV_INFRA_DIR / name / RECORD_FILE


def read_record(name: str) -> dict | None:
    path = record_file(name)
    return json.loads(path.read_text()) if path.exists() else None


def cluster(record: dict, json_output: bool = False) -> Cluster:
    terraform_dir = DEV_INFRA_DIR / record["name"]
    data = terraform_dir / "tf-data"
    data.mkdir(parents=True, exist_ok=True)
    return Cluster(
        spec=record["cluster"],
        data=data,
        backend="local",
        backend_config=[f"path={terraform_dir / 'tfstates' / 'terraform.tfstate'}"],
        owner=f"dev_infra_{record['name']}",
        tf_vars=[
            f"-var=project_id={record['project_id']}",
            f"-var=region={record['region']}",
            f"-var=default_resource_name={record['name']}",
        ],
        json_output=json_output,
    )


def create(record: dict, json_output: bool = False) -> Cluster:
    path = record_file(record["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n")
    created = cluster(record, json_output)
    created.apply()
    return created


def fail(message: str) -> NoReturn:
    log(f"dev-infra: {message}", "ERROR")
    sys.exit(1)


def print_list() -> None:
    rows = []
    for path in sorted(DEV_INFRA_DIR.glob(f"*/{RECORD_FILE}")):
        record = json.loads(path.read_text())
        # How much of it there is, e.g. 0 after an apply that failed early.
        state_file = path.parent / "tfstates" / "terraform.tfstate"
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        resources = sum(len(r.get("instances", [])) for r in state.get("resources", []))
        fields = [record["name"], record["test_case"], record["cluster"]["name"], record["project_id"], record["region"]]
        rows.append([*fields, str(resources)])
    if not rows:
        print(f"No dev infra in {DEV_INFRA_DIR}")
        return
    rows.insert(0, ["NAME", "TEST_CASE", "CLUSTER", "PROJECT", "REGION", "RESOURCES"])
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def main() -> None:
    p = argparse.ArgumentParser(prog="python3 -m orchestrator.dev_infra", description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list the dev infras")
    destroy = commands.add_parser("destroy", help="destroy a dev infra and remove its Terraform files")
    destroy.add_argument("name", type=check_name, metavar="NAME")
    destroy.add_argument("--terraform-output", choices=["text", "json"], default="text", help="default: %(default)s")
    args = p.parse_args()

    if args.command == "list":
        print_list()
        return

    record = read_record(args.name)
    if record is None:
        fail(f"no dev infra {args.name}: no {record_file(args.name)}")
    if not shutil.which("terraform"):
        fail("terraform is required to destroy the dev infra")
    try:
        cluster(record, args.terraform_output == "json").destroy()
    except subprocess.CalledProcessError as exc:
        # cmd[1] is -chdir=<the config's directory>.
        fail(f"terraform {exc.cmd[2]} failed in {exc.cmd[1].removeprefix('-chdir=')}")
    shutil.rmtree(DEV_INFRA_DIR / args.name)


if __name__ == "__main__":
    main()
