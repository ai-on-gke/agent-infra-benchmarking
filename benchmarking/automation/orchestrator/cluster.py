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

"""Creates or destroys the cluster a tests.yaml case runs on, the
infra/clusters/ config its infra.cluster names, and keeps it until destroyed.
Every Terraform file of it is in --terraform-dir, its state a local one; create
records the cluster there, so destroy needs nothing else."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, NoReturn

import yaml

from . import AUTOMATION_DIR, terraform
from .cases import CLUSTERS_DIR, TESTS_FILE, load_cases
from .log import log

RECORD_FILE = "cluster.json"
# Where tools/dev-infra keeps each dev cluster's --terraform-dir, by name.
DEV_INFRA_DIR = AUTOMATION_DIR / ".dev-infra" / "terraform"


@dataclass
class Cluster:
    """A case's infra.cluster, applied with the state backend_config points
    to."""

    spec: dict
    # The TF_DATA_DIR, which also holds the plan and the inline tfvars.
    data: Path
    backend: str
    backend_config: list[str]
    # Names the backend override file, so unique to whoever applies it.
    owner: str
    # -var arguments, which win over the spec's.
    tf_vars: list[str]
    json_output: bool = False

    @property
    def dir(self) -> Path:
        return CLUSTERS_DIR / self.spec["name"]

    def plan_args(self) -> list[str]:
        # Later ones win: tfvars, tfvars_inline, variables, and last tf_vars.
        args = []
        if "tfvars" in self.spec:
            args.append(f"-var-file={self.dir / self.spec['tfvars']}")
        if "tfvars_inline" in self.spec:
            inline = self.data / "inline.tfvars"
            inline.write_text(self.spec["tfvars_inline"])
            args.append(f"-var-file={inline}")
        # A string as it is; anything else as JSON, which terraform reads as HCL.
        args += [
            f"-var={key}={value if isinstance(value, str) else json.dumps(value)}"
            for key, value in self.spec.get("variables", {}).items()
        ]
        return args + self.tf_vars

    @contextmanager
    def initialized(self) -> Iterator[None]:
        # The config is also applied by hand, so its .terraform/ and
        # backend_override.tf stay that deployment's: this has its own data
        # dir, and an override file sorting after backend_override.tf replaces it.
        override = self.dir / f"{self.owner}_override.tf"
        override.write_text(terraform.backend_block(self.backend))
        try:
            terraform.run(
                self.dir,
                "init",
                "-input=false",
                *(f"-backend-config={c}" for c in self.backend_config),
                env=terraform.data_env(self.data),
            )
            yield
        finally:
            override.unlink()

    def deploy(self, *args: str) -> None:
        terraform.deploy(
            self.dir,
            *args,
            *self.plan_args(),
            json_output=self.json_output,
            env=terraform.data_env(self.data),
            plan_file=str(self.data / "run.tfplan"),
        )

    def apply(self) -> None:
        with self.initialized():
            self.deploy()

    def destroy(self) -> None:
        with self.initialized():
            self.deploy("-destroy")

    @contextmanager
    def applied(self) -> Iterator[None]:
        """Applies the cluster for the with block, and destroys it after, also
        after a failed apply, which may have created part of it."""
        with self.initialized():
            try:
                self.deploy()
                yield
            finally:
                self.deploy("-destroy")


def fail(message: str) -> NoReturn:
    log(f"cluster: {message}", "ERROR")
    sys.exit(1)


def local_cluster(terraform_dir: Path, record: dict, json_output: bool) -> Cluster:
    data = terraform_dir / "tf-data"
    data.mkdir(parents=True, exist_ok=True)
    return Cluster(
        spec=record["cluster"],
        data=data,
        backend="local",
        backend_config=[f"path={terraform_dir / 'tfstates' / 'terraform.tfstate'}"],
        owner=f"cluster_{record['name']}",
        tf_vars=[
            f"-var=project_id={record['project_id']}",
            f"-var=region={record['region']}",
            f"-var=default_resource_name={record['name']}",
        ],
        json_output=json_output,
    )


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--terraform-dir", type=Path, required=True, help="where the cluster's state and Terraform data are"
    )
    common.add_argument("--terraform-output", choices=["text", "json"], default="text", help="default: %(default)s")

    p = argparse.ArgumentParser(prog="python3 -m orchestrator.cluster", description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", parents=[common], help="apply a case's cluster")
    create.add_argument("--test-case-name", required=True, metavar="NAME", help="the case whose cluster to apply")
    create.add_argument("--tests-file", type=Path, default=TESTS_FILE, help=f"default: {TESTS_FILE}")
    create.add_argument("--project-id", required=True, help="GCP project to create the cluster in")
    create.add_argument("--region", default="us-central1", help="default: %(default)s")
    create.add_argument("--name", required=True, help="the cluster's resource name")
    commands.add_parser("destroy", parents=[common], help="destroy the cluster create applied")
    args = p.parse_args()

    if not shutil.which("terraform"):
        fail("terraform is required to apply the cluster")

    record_file = args.terraform_dir / RECORD_FILE
    if args.command == "create":
        try:
            cases = load_cases(args.tests_file)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            fail(str(exc))
        if args.test_case_name not in cases:
            fail(f"no case {args.test_case_name} in {args.tests_file}; it has {', '.join(cases)}")
        record = {
            "name": args.name,
            "test_case": args.test_case_name,
            "project_id": args.project_id,
            "region": args.region,
            "cluster": cases[args.test_case_name]["infra"]["cluster"],
        }
        # The same one again re-applies it, e.g. after a failed create.
        if record_file.exists() and json.loads(record_file.read_text()) != record:
            fail(f"{args.terraform_dir} already holds another cluster, recorded in {record_file}; destroy it first")
        args.terraform_dir.mkdir(parents=True, exist_ok=True)
        # Before applying, so a failed create can be destroyed.
        record_file.write_text(json.dumps(record, indent=2) + "\n")
    elif record_file.exists():
        record = json.loads(record_file.read_text())
    else:
        fail(f"no cluster in {args.terraform_dir}: it has no {RECORD_FILE}")

    cluster = local_cluster(args.terraform_dir, record, args.terraform_output == "json")
    try:
        if args.command == "create":
            cluster.apply()
        else:
            cluster.destroy()
    except subprocess.CalledProcessError as exc:
        # cmd[1] is -chdir=<the config's directory>.
        fail(f"terraform {exc.cmd[2]} failed in {exc.cmd[1].removeprefix('-chdir=')}")


if __name__ == "__main__":
    main()
