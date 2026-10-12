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

"""Lists or destroys what the scenario-runner's executions with
--state-backend=gcs left behind: the deployments under <state prefix>/<run id>/
in the state bucket that were created but never destroyed, as when an
execution is killed before its own destroy."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import NoReturn

from ..deployment import DESTROYED_FILE, VARIABLES_FILE, GcsDeployment, storage_client
from ..log import log
from ..process import failure
from .dev_infra import resource_count
from .run import LOCAL_TERRAFORM_DIR

RUNNER_KEY = "scenario-runner"


def fail(message: str) -> NoReturn:
    print(f"runs: {message}", file=sys.stderr)
    sys.exit(1)


def deployment(args: argparse.Namespace, key: tuple[str, ...]) -> GcsDeployment:
    """The deployment as its execution had it, downloaded to the same
    .local-data/runs/terraform/<run id>/<key>/."""
    json_output = getattr(args, "terraform_output", "text") == "json"
    prefix = "/".join([args.state_prefix, *key])
    return GcsDeployment(LOCAL_TERRAFORM_DIR.joinpath(*key), json_output, bucket=args.state_bucket, prefix=prefix)


def leftovers(args: argparse.Namespace) -> dict[str, list[GcsDeployment]]:
    """Each run's deployments not destroyed, by run id, the clusters before the
    scenario-runner as the execution destroys them."""
    created, destroyed = set(), set()
    for blob in storage_client().list_blobs(args.state_bucket, prefix=f"{args.state_prefix}/"):
        parts = tuple(blob.name.removeprefix(f"{args.state_prefix}/").split("/"))
        if "config" in parts:
            created.add(parts[: parts.index("config")])
        if parts[-1] == DESTROYED_FILE:
            destroyed.add(parts[:-1])
    runs: dict[str, list[GcsDeployment]] = {}
    for key in sorted(created - destroyed, key=lambda key: (key[0], key[1] == RUNNER_KEY, key)):
        runs.setdefault(key[0], []).append(deployment(args, key))
    return runs


def run_labels(args: argparse.Namespace, run_id: str) -> dict[str, str]:
    """The labels of the run's scenario-runner, which its clusters' share."""
    name = f"{args.state_prefix}/{run_id}/{RUNNER_KEY}/config/{VARIABLES_FILE}"
    blob = storage_client().bucket(args.state_bucket).blob(name)
    return json.loads(blob.download_as_text()).get("labels", {}) if blob.exists() else {}


def deployment_info(run_id: str, infra: GcsDeployment) -> dict:
    """Its key in the run, e.g. scenarios/glutton-1, and how many resources its
    state holds."""
    name = infra.root.relative_to(LOCAL_TERRAFORM_DIR / run_id).as_posix()
    return {"name": name, "resources": resource_count(infra)}


def print_list(args: argparse.Namespace) -> None:
    runs = [
        {
            "run_id": run_id,
            "labels": run_labels(args, run_id),
            "deployments": [deployment_info(run_id, infra) for infra in infras],
        }
        for run_id, infras in leftovers(args).items()
    ]
    if args.json:
        print(json.dumps(runs, indent=2))
        return
    if not runs:
        print(f"Nothing left in gs://{args.state_bucket}/{args.state_prefix}/")
        return
    rows = [["RUN_ID", "DEPLOYMENT", "RESOURCES", "EXECUTION", "TRIGGER"]]
    for run in runs:
        labels = run["labels"]
        for infra in run["deployments"]:
            rows.append([
                run["run_id"],
                infra["name"],
                str(infra["resources"]),
                labels.get("benchmark-execution", "-"),
                labels.get("benchmark-trigger", "-"),
            ])
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def destroy(args: argparse.Namespace) -> bool:
    """Destroys each run's leftovers, and says whether every destroy succeeded.
    A run's scenario-runner stays if one of its clusters fails to destroy."""
    runs = leftovers(args)
    succeeded = True
    for run_id in args.run_ids:
        if run_id not in runs:
            log(f"runs: {run_id} has nothing left to destroy")
            continue
        try:
            for infra in runs[run_id]:
                log(f"runs: destroying {infra.url}")
                infra.destroy()
        except subprocess.CalledProcessError as exc:
            log(f"runs: {run_id}: {failure(exc)}", "ERROR")
            succeeded = False
    return succeeded


def main() -> None:
    p = argparse.ArgumentParser(prog="python3 -m scenario_runner.cli.runs", description=__doc__)
    p.add_argument("--state-bucket", default=os.environ.get("STATE_BUCKET"), help="default: $STATE_BUCKET")
    p.add_argument("--state-prefix", default=os.environ.get("STATE_PREFIX"), help="default: $STATE_PREFIX")
    commands = p.add_subparsers(dest="command", required=True)
    list_command = commands.add_parser("list", help="list each run's deployments left, with the resources they hold")
    list_command.add_argument("--json", action="store_true", help="print the runs, with their labels, as JSON")
    destroy_command = commands.add_parser("destroy", help="destroy what the runs left, clusters first")
    destroy_command.add_argument("run_ids", nargs="+", metavar="RUN_ID")
    destroy_command.add_argument(
        "--terraform-output", choices=["text", "json"], default="text", help="default: %(default)s"
    )
    args = p.parse_args()
    if not (args.state_bucket and args.state_prefix):
        p.error("--state-bucket and --state-prefix, or STATE_BUCKET and STATE_PREFIX, are required")

    if args.command == "list":
        print_list(args)
        return
    if not shutil.which("terraform"):
        fail("terraform is required to destroy the runs' infra")
    if not destroy(args):
        sys.exit(1)


if __name__ == "__main__":
    main()
