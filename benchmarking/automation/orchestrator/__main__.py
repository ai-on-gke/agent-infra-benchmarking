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
destroyed after it, and destroys the infra again."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

import yaml

from . import terraform
from .log import log

AUTOMATION_DIR = Path(__file__).resolve().parent.parent
TESTS_FILE = AUTOMATION_DIR / "tests.yaml"
INFRA_DIR = AUTOMATION_DIR / "infra" / "orchestrator"
# The infra's backend, which each execution writes; the infra's .gitignore
# leaves out *_override.tf.
BACKEND_FILE = INFRA_DIR / "backend_override.tf"
# One Terraform config per directory, which a case's infra.cluster.name picks.
CLUSTERS_DIR = AUTOMATION_DIR / "infra" / "clusters"
CLUSTER_KEYS = {"name", "tfvars", "tfvars_inline", "variables"}
# Every case's .terraform/ starts empty, so its providers come from here rather
# than from a download each.
PLUGIN_CACHE_DIR = Path(os.environ.get("TF_PLUGIN_CACHE_DIR") or AUTOMATION_DIR / ".terraform-plugin-cache")
# An execution's local files, in <name>/: its own tfstates/ with the local
# backend, and a folder per case, <name>/tests/<case>/, with that case's
# tfstates/ and, with either backend, its tf-data/.
LOCAL_RUNS_DIR = AUTOMATION_DIR / "local-runs"


def fail(message: str) -> NoReturn:
    log(f"run: {message}", "ERROR")
    sys.exit(1)


def load_cases(tests_file: Path) -> dict[str, dict]:
    with tests_file.open() as f:
        cases = (yaml.safe_load(f) or {}).get("cases")
    if not isinstance(cases, dict) or not cases:
        raise ValueError(f"{tests_file} has no `cases` mapping")
    for name, case in cases.items():
        if not isinstance(case, dict):
            raise ValueError(f"{tests_file}: case {name} is not a mapping")
        check_cluster(f"{tests_file}: case {name}", case)
    return cases


def check_cluster(where: str, case: dict) -> None:
    infra = case.get("infra")
    cluster = infra.get("cluster") if isinstance(infra, dict) else None
    if not isinstance(cluster, dict):
        raise ValueError(f"{where} has no `infra.cluster` mapping")
    unknown = cluster.keys() - CLUSTER_KEYS
    if unknown:
        raise ValueError(f"{where}: `infra.cluster` has unknown {', '.join(sorted(unknown))}")

    clusters = sorted(p.name for p in CLUSTERS_DIR.iterdir() if p.is_dir())
    if cluster.get("name") not in clusters:
        raise ValueError(f"{where}: `infra.cluster.name` must be one of {', '.join(clusters)}")
    tfvars = cluster.get("tfvars")
    # An absolute path stays as it is; a relative one is in the cluster's config.
    if tfvars is not None and not (isinstance(tfvars, str) and (CLUSTERS_DIR / cluster["name"] / tfvars).is_file()):
        raise ValueError(f"{where}: `infra.cluster.tfvars` {tfvars!r} is not a file")
    if not isinstance(cluster.get("tfvars_inline", ""), str):
        raise ValueError(f"{where}: `infra.cluster.tfvars_inline` must be a string")
    if not isinstance(cluster.get("variables", {}), dict):
        raise ValueError(f"{where}: `infra.cluster.variables` must be a mapping")


def backend_config(args: argparse.Namespace, case: str | None = None) -> list[str]:
    """The -backend-config of the execution's state, or given a case that
    case's: under <state prefix>/<name>[/tests/<case>] in GCS, and in
    local-runs/<name>[/tests/<case>]/tfstates/ with the local backend."""
    key = ["tests", case] if case else []
    if args.state_backend == "gcs":
        return [f"bucket={args.state_bucket}", f"prefix={'/'.join([args.state_prefix, args.name, *key])}"]
    return [f"path={LOCAL_RUNS_DIR.joinpath(args.name, *key, 'tfstates', 'terraform.tfstate')}"]


def backend_block(args: argparse.Namespace) -> str:
    return f'terraform {{\n  backend "{args.state_backend}" {{}}\n}}\n'


def run_case(name: str, case: dict, args: argparse.Namespace, tf_vars: list[str]) -> None:
    cluster = case["infra"]["cluster"]
    cluster_dir = CLUSTERS_DIR / cluster["name"]
    json_output = args.terraform_output == "json"
    # The config is also applied by hand, so its .terraform/ and
    # backend_override.tf stay that deployment's: the case has its own data
    # dir, and an override file sorting after backend_override.tf replaces it.
    override = cluster_dir / f"orchestrator_{args.name}_override.tf"
    # The case's TF_DATA_DIR, which also holds its plan and inline tfvars.
    data_dir = LOCAL_RUNS_DIR / args.name / "tests" / name / "tf-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    PLUGIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    env = {"TF_DATA_DIR": str(data_dir), "TF_PLUGIN_CACHE_DIR": str(PLUGIN_CACHE_DIR)}
    plan_file = str(data_dir / "run.tfplan")

    # Later ones win: tfvars, tfvars_inline, variables, and last the
    # execution's own project, region and name.
    plan_args = []
    if "tfvars" in cluster:
        plan_args.append(f"-var-file={cluster_dir / cluster['tfvars']}")
    if "tfvars_inline" in cluster:
        inline = data_dir / "inline.tfvars"
        inline.write_text(cluster["tfvars_inline"])
        plan_args.append(f"-var-file={inline}")
    # A string as it is; anything else as JSON, which terraform reads as HCL.
    plan_args += [
        f"-var={key}={value if isinstance(value, str) else json.dumps(value)}"
        for key, value in cluster.get("variables", {}).items()
    ]
    plan_args += tf_vars

    override.write_text(backend_block(args))
    try:
        terraform.run(
            cluster_dir,
            "init",
            "-input=false",
            *(f"-backend-config={c}" for c in backend_config(args, name)),
            env=env,
        )
        try:
            terraform.deploy(cluster_dir, *plan_args, json_output=json_output, env=env, plan_file=plan_file)
            # The case itself is a no-op for now.
            log(f"{name}: {json.dumps(case)}")
        finally:
            terraform.deploy(cluster_dir, "-destroy", *plan_args, json_output=json_output, env=env, plan_file=plan_file)
    finally:
        override.unlink()


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
        f" with gcs. With local, the state is under {LOCAL_RUNS_DIR}/<name>",
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

    json_output = args.terraform_output == "json"
    tf_vars = [
        f"-var=project_id={args.project_id}",
        f"-var=region={args.region}",
        f"-var=default_resource_name={args.name}",
    ]
    BACKEND_FILE.write_text(backend_block(args))
    try:
        # -reconfigure, as the .terraform/ left by the last execution names its backend.
        # terraform.run(
        #     INFRA_DIR,
        #     "init",
        #     "-input=false",
        #     "-reconfigure",
        #     *(f"-backend-config={c}" for c in backend_config(args)),
        # )
        try:
            # terraform.deploy(INFRA_DIR, *tf_vars, json_output=json_output)
            for name in names:
                run_case(name, cases[name], args, tf_vars)
        finally:
            # Also after a failed apply, which may have created part of the infra.
            # terraform.deploy(INFRA_DIR, "-destroy", *tf_vars, json_output=json_output)
            pass
    except subprocess.CalledProcessError as exc:
        # cmd[1] is -chdir=<the config's directory>.
        fail(f"terraform {exc.cmd[2]} failed in {exc.cmd[1].removeprefix('-chdir=')}")


if __name__ == "__main__":
    main()
