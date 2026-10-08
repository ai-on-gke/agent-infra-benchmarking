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

"""Applies infra/orchestrator/, runs the cases in tests.yaml one after another
(every case in the order listed, or the ones --test-case-name names in the
order given), each on its own infra/clusters/ cluster applied for it and
destroyed after it, and destroys the infra again. With --dev-infra, uses the
infra/orchestrator and the cluster of that dev infra instead, which outlive the
execution."""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

import yaml

from . import AUTOMATION_DIR, dev_infra, terraform
from .cases import TESTS_FILE, load_cases
from .deployment import ORCHESTRATOR_DIR, Deployment, cluster
from .log import log

# The executions' local Terraform files, as the Cloud Run job's state is under
# terraform/ in its bucket. An execution's are in <name>/: a folder for
# infra/orchestrator, orchestrator/, and one per case, tests/<case>/, each with
# its tfstates/ when the backend is local and its tf-data/ with either.
LOCAL_TERRAFORM_DIR = AUTOMATION_DIR / "local-runs" / "terraform"


def fail(message: str) -> NoReturn:
    log(f"run: {message}", "ERROR")
    sys.exit(1)


def deployment_files(args: argparse.Namespace, *key: str) -> dict:
    """Where an infra of this execution keeps its files: its state under
    <state prefix>/<name>/<key> in GCS, or in
    local-runs/terraform/<name>/<key>/tfstates/ with the local backend, and its
    TF_DATA_DIR in local-runs/terraform/<name>/<key>/tf-data/ with either."""
    if args.state_backend == "gcs":
        backend_config = [f"bucket={args.state_bucket}", f"prefix={'/'.join([args.state_prefix, args.name, *key])}"]
    else:
        backend_config = [f"path={LOCAL_TERRAFORM_DIR.joinpath(args.name, *key, 'tfstates', 'terraform.tfstate')}"]
    data = LOCAL_TERRAFORM_DIR.joinpath(args.name, *key, "tf-data")
    data.mkdir(parents=True, exist_ok=True)
    return {
        "data": data,
        "backend": args.state_backend,
        "backend_config": backend_config,
        "owner": f"orchestrator_{args.name}",
        "json_output": args.terraform_output == "json",
    }


def prepare_dev_infra(args: argparse.Namespace, cases: dict[str, dict], names: list[str]) -> None:
    """Checks every case can run on the dev infra --dev-infra names, and applies
    it unless it is complete. A new one has the first case's cluster."""
    record = dev_infra.read_record(args.dev_infra)
    if record is None:
        record = {
            "name": args.dev_infra,
            "test_case": names[0],
            "project_id": args.project_id,
            "region": args.region,
            "cluster": cases[names[0]]["infra"]["cluster"],
        }
    elif (record["project_id"], record["region"]) != (args.project_id, args.region):
        fail(
            f"dev infra {args.dev_infra} is in project {record['project_id']} and region {record['region']},"
            f" not {args.project_id} and {args.region}"
        )
    for name in names:
        spec = cases[name]["infra"]["cluster"]
        if spec["name"] != record["cluster"]["name"]:
            fail(
                f"case {name} runs on infra/clusters/{spec['name']}, but dev infra {args.dev_infra}"
                f" is infra/clusters/{record['cluster']['name']}"
            )
        if spec != record["cluster"]:
            log(f"run: case {name}'s infra.cluster differs from dev infra {args.dev_infra}'s it runs on", "WARNING")
    if not record.get("complete"):
        log(f"run: applying dev infra {args.dev_infra}, which is not complete")
        dev_infra.apply(record, args.terraform_output == "json")


def run_case(name: str, case: dict) -> None:
    if case["type"] == "noop":
        log(f"{name}: noop, nothing to run")
    else:
        # Running the case is a no-op for now.
        log(f"{name}: {json.dumps(case)}")


def main() -> None:
    p = argparse.ArgumentParser(prog="python3 -m orchestrator", description=__doc__)
    p.add_argument("--tests-file", type=Path, default=TESTS_FILE, help=f"default: {TESTS_FILE}")
    p.add_argument(
        "--test-case-name",
        action="append",
        metavar="NAME",
        help="a case to run; repeat it for more. Default: every case",
    )
    p.add_argument("--project-id", required=True, help="GCP project to create the infra in")
    p.add_argument("--region", default="us-central1", help="default: %(default)s")
    p.add_argument(
        "--state-backend",
        choices=["local", "gcs"],
        required=True,
        help="where the infra's Terraform state goes: a local file or a GCS bucket",
    )
    p.add_argument("--state-bucket", help="GCS bucket holding the state; only, and required, with gcs")
    p.add_argument(
        "--state-prefix",
        help="prefix in the state bucket, each execution's state being under <prefix>/<name>; only, and required,"
        f" with gcs. With local, the state is under {LOCAL_TERRAFORM_DIR}/<name>",
    )
    p.add_argument(
        "--name",
        default=datetime.now(timezone.utc).strftime("run-%Y%m%dt%H%M%Sz"),
        help="this execution's infra and state prefix; unique to it. Default: run-<UTC timestamp>",
    )
    p.add_argument(
        "--terraform-output",
        choices=["text", "json"],
        default="text",
        help="how terraform plan and apply log: their text output line by line, or -json one message per entry."
        " Default: %(default)s",
    )
    p.add_argument(
        "--dev-infra",
        type=dev_infra.check_name,
        metavar="NAME",
        help="run every case on this dev infra, an infra/orchestrator and a cluster kept until"
        " python3 -m orchestrator.dev_infra destroys them, rather than on ones applied for this execution."
        " Applied unless complete, a new one with the first case's cluster",
    )
    args = p.parse_args()
    if args.state_backend == "gcs" and not (args.state_bucket and args.state_prefix):
        p.error("--state-backend=gcs requires --state-bucket and --state-prefix")
    if args.state_backend == "local" and (args.state_bucket or args.state_prefix):
        p.error("--state-bucket and --state-prefix are only for --state-backend=gcs")

    try:
        cases = load_cases(args.tests_file)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        fail(str(exc))

    # dict.fromkeys drops repeated names, keeping the first.
    names = list(dict.fromkeys(args.test_case_name or cases))
    unknown = [name for name in names if name not in cases]
    if unknown:
        fail(f"no case {', '.join(unknown)} in {args.tests_file}; it has {', '.join(cases)}")

    if not shutil.which("terraform"):
        fail("terraform is required to apply the infra")

    # Cloud Run stops a task that outlives its timeout with SIGTERM; exiting
    # on it, rather than dying, still destroys the infra.
    signal.signal(signal.SIGTERM, lambda *_: fail("stopped by SIGTERM"))

    tf_vars = [
        f"-var=project_id={args.project_id}",
        f"-var=region={args.region}",
        f"-var=default_resource_name={args.name}",
    ]
    try:
        if args.dev_infra:
            prepare_dev_infra(args, cases, names)
            for name in names:
                run_case(name, cases[name])
            return
        orchestrator = Deployment(dir=ORCHESTRATOR_DIR, plan_args=tf_vars, **deployment_files(args, "orchestrator"))
        with orchestrator.applied():
            for name in names:
                files = deployment_files(args, "tests", name)
                with cluster(cases[name]["infra"]["cluster"], tf_vars=tf_vars, **files).applied():
                    run_case(name, cases[name])
    except subprocess.CalledProcessError as exc:
        # cmd[1] is -chdir=<the config's directory>.
        fail(f"terraform {exc.cmd[2]} failed in {exc.cmd[1].removeprefix('-chdir=')}")


if __name__ == "__main__":
    main()
