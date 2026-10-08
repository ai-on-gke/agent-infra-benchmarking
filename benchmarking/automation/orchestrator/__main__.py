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
destroyed after it, or all on the one --dev-infra names, and destroys the infra
again."""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

import yaml

from . import AUTOMATION_DIR, terraform
from .cases import TESTS_FILE, load_cases
from .cluster import DEV_INFRA_DIR, RECORD_FILE, Cluster, local_cluster
from .log import log

INFRA_DIR = AUTOMATION_DIR / "infra" / "orchestrator"
# The infra's backend, which each execution writes; the infra's .gitignore
# leaves out *_override.tf.
BACKEND_FILE = INFRA_DIR / "backend_override.tf"
# The executions' local Terraform files, as the Cloud Run job's state is under
# terraform/ in its bucket. An execution's are in <name>/: a folder for
# infra/orchestrator, orchestrator/, and one per case, tests/<case>/, each with
# its tfstates/ when the backend is local and its tf-data/ with either.
LOCAL_TERRAFORM_DIR = AUTOMATION_DIR / "local-runs" / "terraform"


def fail(message: str) -> NoReturn:
    log(f"run: {message}", "ERROR")
    sys.exit(1)


def backend_config(args: argparse.Namespace, *key: str) -> list[str]:
    """The -backend-config of the state under <state prefix>/<name>/<key> in
    GCS, or in local-runs/terraform/<name>/<key>/tfstates/ with the local
    backend."""
    if args.state_backend == "gcs":
        return [f"bucket={args.state_bucket}", f"prefix={'/'.join([args.state_prefix, args.name, *key])}"]
    return [f"path={LOCAL_TERRAFORM_DIR.joinpath(args.name, *key, 'tfstates', 'terraform.tfstate')}"]


def data_dir(args: argparse.Namespace, *key: str) -> Path:
    """local-runs/terraform/<name>/<key>/tf-data/, with either backend: a
    TF_DATA_DIR, which also holds its config's plan."""
    path = LOCAL_TERRAFORM_DIR.joinpath(args.name, *key, "tf-data")
    path.mkdir(parents=True, exist_ok=True)
    return path


def dev_cluster(args: argparse.Namespace, cases: dict[str, dict], names: list[str]) -> Cluster:
    """The dev cluster --dev-infra names, once it is one every case can run on."""
    terraform_dir = DEV_INFRA_DIR / args.dev_infra
    record_file = terraform_dir / RECORD_FILE
    if not record_file.exists():
        fail(f"no dev infra {args.dev_infra}: no {record_file}")
    record = json.loads(record_file.read_text())
    if record["project_id"] != args.project_id:
        fail(f"dev infra {args.dev_infra} is in project {record['project_id']}, not {args.project_id}")
    for name in names:
        spec = cases[name]["infra"]["cluster"]
        if spec["name"] != record["cluster"]["name"]:
            fail(
                f"case {name} runs on infra/clusters/{spec['name']}, but dev infra {args.dev_infra}"
                f" is infra/clusters/{record['cluster']['name']}"
            )
        if spec != record["cluster"]:
            log(f"run: case {name}'s infra.cluster differs from dev infra {args.dev_infra}'s it runs on", "WARNING")
    return local_cluster(terraform_dir, record, args.terraform_output == "json")


def run_case(name: str, case: dict, args: argparse.Namespace, tf_vars: list[str], dev: Cluster | None) -> None:
    cluster = dev or Cluster(
        spec=case["infra"]["cluster"],
        data=data_dir(args, "tests", name),
        backend=args.state_backend,
        backend_config=backend_config(args, "tests", name),
        owner=f"orchestrator_{args.name}",
        tf_vars=tf_vars,
        json_output=args.terraform_output == "json",
    )
    # A dev cluster outlives the execution, so is neither applied nor destroyed.
    with nullcontext() if dev else cluster.applied():
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
        metavar="NAME",
        help="run every case on this dev cluster, which tools/dev-infra created, rather than on one applied for it",
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
    dev = dev_cluster(args, cases, names) if args.dev_infra else None

    # Cloud Run stops a task that outlives its timeout with SIGTERM; exiting
    # on it, rather than dying, still destroys the infra.
    signal.signal(signal.SIGTERM, lambda *_: fail("stopped by SIGTERM"))

    json_output = args.terraform_output == "json"
    tf_vars = [
        f"-var=project_id={args.project_id}",
        f"-var=region={args.region}",
        f"-var=default_resource_name={args.name}",
    ]
    orchestrator_data = data_dir(args, "orchestrator")
    env = terraform.data_env(orchestrator_data)
    plan_file = str(orchestrator_data / "run.tfplan")
    BACKEND_FILE.write_text(terraform.backend_block(args.state_backend))
    try:
        terraform.run(
            INFRA_DIR,
            "init",
            "-input=false",
            *(f"-backend-config={c}" for c in backend_config(args, "orchestrator")),
            env=env,
        )
        try:
            terraform.deploy(INFRA_DIR, *tf_vars, json_output=json_output, env=env, plan_file=plan_file)
            for name in names:
                run_case(name, cases[name], args, tf_vars, dev)
        finally:
            # Also after a failed apply, which may have created part of the infra.
            terraform.deploy(INFRA_DIR, "-destroy", *tf_vars, json_output=json_output, env=env, plan_file=plan_file)
    except subprocess.CalledProcessError as exc:
        # cmd[1] is -chdir=<the config's directory>.
        fail(f"terraform {exc.cmd[2]} failed in {exc.cmd[1].removeprefix('-chdir=')}")


if __name__ == "__main__":
    main()
