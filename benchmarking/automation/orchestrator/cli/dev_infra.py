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

"""Lists the dev infras the orchestrator's --dev-infra creates and keeps, shows
their outputs or destroys them."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

from .. import terraform
from ..dev_infra import (
    DEV_INFRA_DIR,
    INFRAS,
    RECORD_FILE,
    check_name,
    destroy,
    read_record,
    read_state,
    record_file,
    state_file,
)
from ..process import failure


def fail(message: str) -> NoReturn:
    print(f"dev-infra: {message}", file=sys.stderr)
    sys.exit(1)


def resource_count(path: Path) -> int:
    return sum(len(r.get("instances", [])) for r in read_state(path).get("resources", []))


def print_outputs(name: str) -> None:
    """Each infra's outputs, read off its state, sensitive ones hidden as
    terraform output hides them."""
    for infra in INFRAS:
        path = state_file(name, infra)
        if not path.exists():
            print(f"{infra}: not applied")
            continue
        print(f"{infra}:")
        for key, out in read_state(path).get("outputs", {}).items():
            print(f"  {terraform.format_output(key, out)}")


def print_list() -> None:
    rows = []
    for path in sorted(DEV_INFRA_DIR.glob(f"*/{RECORD_FILE}")):
        r = json.loads(path.read_text())
        fields = [r["name"], r["test_case"], r["cluster"]["name"], r["project_id"], r["region"]]
        status = "complete" if r.get("complete") else "incomplete"
        installed = "installed" if r.get("substrate_installed") else "-"
        counts = [str(resource_count(state_file(r["name"], infra))) for infra in INFRAS]
        rows.append([*fields, status, installed, *counts])
    if not rows:
        print(f"No dev infra in {DEV_INFRA_DIR}")
        return
    header = ["NAME", "TEST_CASE", "CLUSTER_CONFIG", "PROJECT", "REGION", "STATUS", "SUBSTRATE"]
    rows.insert(0, [*header, *map(str.upper, INFRAS)])
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def main() -> None:
    p = argparse.ArgumentParser(prog="python3 -m orchestrator.cli.dev_infra", description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list the dev infras, with how many resources each of their infras holds")
    outputs_command = commands.add_parser("outputs", help="show the outputs of a dev infra's infras")
    outputs_command.add_argument("name", type=check_name, metavar="NAME")
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
    if args.command == "outputs":
        print_outputs(args.name)
        return
    if not shutil.which("terraform"):
        fail("terraform is required to destroy the dev infra")
    try:
        destroy(record, args.terraform_output == "json")
    except subprocess.CalledProcessError as exc:
        fail(failure(exc))


if __name__ == "__main__":
    main()
