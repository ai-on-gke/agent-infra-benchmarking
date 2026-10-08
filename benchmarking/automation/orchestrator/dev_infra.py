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

"""Lists or destroys the dev infras: infra/orchestrator and a case's cluster,
which the orchestrator's --dev-infra creates and keeps, each with every
Terraform file of it in .dev-infra/terraform/<name>/<infra>/."""

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
from .deployment import ORCHESTRATOR_DIR, Deployment, cluster
from .log import log

DEV_INFRA_DIR = AUTOMATION_DIR / ".dev-infra" / "terraform"
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
    return DEV_INFRA_DIR / name / infra / "tfstates" / "terraform.tfstate"


def deployments(record: dict, json_output: bool = False) -> dict[str, Deployment]:
    name = record["name"]
    tf_vars = [
        f"-var=project_id={record['project_id']}",
        f"-var=region={record['region']}",
        f"-var=default_resource_name={name}",
    ]

    def files(infra: str) -> dict:
        data = DEV_INFRA_DIR / name / infra / "tf-data"
        data.mkdir(parents=True, exist_ok=True)
        return {
            "data": data,
            "backend": "local",
            "backend_config": [f"path={state_file(name, infra)}"],
            "owner": f"dev_infra_{name}",
            "json_output": json_output,
        }

    return {
        "orchestrator": Deployment(dir=ORCHESTRATOR_DIR, plan_args=tf_vars, **files("orchestrator")),
        "cluster": cluster(record["cluster"], tf_vars=tf_vars, **files("cluster")),
    }


def apply(record: dict, json_output: bool = False) -> None:
    """Applies every infra of the dev infra, which is complete only once all
    of them are."""
    record = {**record, "complete": False}
    write_record(record)
    for deployment in deployments(record, json_output).values():
        deployment.apply()
    write_record({**record, "complete": True})


def destroy(record: dict, json_output: bool = False) -> None:
    for infra, deployment in reversed(deployments(record, json_output).items()):
        # One never applied has nothing to destroy.
        if state_file(record["name"], infra).exists():
            deployment.destroy()
    shutil.rmtree(DEV_INFRA_DIR / record["name"])


def fail(message: str) -> NoReturn:
    log(f"dev-infra: {message}", "ERROR")
    sys.exit(1)


def resource_count(path: Path) -> int:
    state = json.loads(path.read_text()) if path.exists() else {}
    return sum(len(r.get("instances", [])) for r in state.get("resources", []))


def print_list() -> None:
    rows = []
    for path in sorted(DEV_INFRA_DIR.glob(f"*/{RECORD_FILE}")):
        r = json.loads(path.read_text())
        fields = [r["name"], r["test_case"], r["cluster"]["name"], r["project_id"], r["region"]]
        status = "complete" if r.get("complete") else "incomplete"
        counts = [str(resource_count(state_file(r["name"], infra))) for infra in INFRAS]
        rows.append([*fields, status, *counts])
    if not rows:
        print(f"No dev infra in {DEV_INFRA_DIR}")
        return
    rows.insert(0, ["NAME", "TEST_CASE", "CLUSTER_CONFIG", "PROJECT", "REGION", "STATUS", *map(str.upper, INFRAS)])
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def main() -> None:
    p = argparse.ArgumentParser(prog="python3 -m orchestrator.dev_infra", description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list the dev infras, with how many resources each of their infras holds")
    destroy_command = commands.add_parser("destroy", help="destroy a dev infra and remove its Terraform files")
    destroy_command.add_argument("name", type=check_name, metavar="NAME")
    destroy_command.add_argument(
        "--terraform-output", choices=["text", "json"], default="text", help="default: %(default)s"
    )
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
        destroy(record, args.terraform_output == "json")
    except subprocess.CalledProcessError as exc:
        # cmd[1] is -chdir=<the config's directory>.
        fail(f"terraform {exc.cmd[2]} failed in {exc.cmd[1].removeprefix('-chdir=')}")


if __name__ == "__main__":
    main()
