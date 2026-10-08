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

"""Applies and destroys a Terraform config: scenario_runner/infra, or the
infra/clusters/ one a case's infra.cluster names."""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import LOCAL_DATA_DIR, RUNNER_DIR, terraform
from .cases import CLUSTERS_DIR

ORCHESTRATOR_DIR = RUNNER_DIR / "infra"
# The copies of the configs terraform runs on, each at its deployment's path
# under LOCAL_DATA_DIR, and kept only after a failure.
ACTIVE_DIR = LOCAL_DATA_DIR / "active"


def state_file_under(root: Path) -> Path:
    return root / "tfstates" / "terraform.tfstate"


def data_dir_under(root: Path) -> Path:
    return root / "tf-data"


def files_under(
    root: Path, json_output: bool, backend: str = "local", backend_config: list[str] | None = None
) -> dict:
    """A Deployment's arguments for one keeping its files under root, in
    LOCAL_DATA_DIR: its TF_DATA_DIR in tf-data/ and, by default, its state in
    tfstates/."""
    data = data_dir_under(root)
    data.mkdir(parents=True, exist_ok=True)
    return {
        "copy": ACTIVE_DIR / root.relative_to(LOCAL_DATA_DIR),
        "data": data,
        "backend": backend,
        "backend_config": backend_config or [f"path={state_file_under(root)}"],
        "json_output": json_output,
    }


def is_override(name: str) -> bool:
    return name in ("override.tf", "override.tf.json") or name.endswith(("_override.tf", "_override.tf.json"))


def tf_vars(project_id: str, region: str, name: str) -> list[str]:
    """The variables every config takes, name naming its resources."""
    return [f"-var=project_id={project_id}", f"-var=region={region}", f"-var=default_resource_name={name}"]


@dataclass
class Deployment:
    """A Terraform config, applied with the state backend_config points to."""

    dir: Path
    # The copy of the config terraform runs on, made by init.
    copy: Path
    # The TF_DATA_DIR, which also holds the plan.
    data: Path
    backend: str
    backend_config: list[str]
    plan_args: list[str]
    json_output: bool = False

    def init(self) -> None:
        # The config is also applied by hand, so terraform runs on a copy of
        # it without its .terraform/ and override files, which are that
        # deployment's, and with this one's backend.
        shutil.rmtree(self.copy, ignore_errors=True)
        shutil.copytree(
            self.dir, self.copy, ignore=lambda _, names: [n for n in names if n == ".terraform" or is_override(n)]
        )
        (self.copy / "backend_override.tf").write_text(terraform.backend_block(self.backend))
        terraform.run(
            self.copy,
            "init",
            "-input=false",
            # The state is where backend_config says, even if the data dir
            # was initialized with another path, e.g. before a move.
            "-reconfigure",
            *(f"-backend-config={c}" for c in self.backend_config),
            env=terraform.data_env(self.data),
        )

    def deploy(self, *args: str) -> None:
        terraform.deploy(
            self.copy,
            *args,
            *self.plan_args,
            json_output=self.json_output,
            env=terraform.data_env(self.data),
            plan_file=str(self.data / "run.tfplan"),
        )

    def outputs(self) -> dict:
        """Its outputs, by name; only after init."""
        return terraform.output(self.copy, env=terraform.data_env(self.data))

    def remove_copy(self) -> None:
        """Removes the copy, and its parents up to ACTIVE_DIR once empty."""
        shutil.rmtree(self.copy)
        for parent in self.copy.parents:
            if parent == ACTIVE_DIR:
                break
            try:
                parent.rmdir()
            except OSError:
                # Not empty: another deployment's copy is in it.
                break

    def apply(self) -> None:
        self.init()
        self.deploy()
        self.remove_copy()

    def destroy(self) -> None:
        self.init()
        self.deploy("-destroy")
        self.remove_copy()

    @contextmanager
    def applied(self) -> Iterator[None]:
        """Applies the config for the with block, and destroys it after, also
        after a failed apply, which may have created part of it."""
        self.init()
        try:
            self.deploy()
            yield
        finally:
            self.deploy("-destroy")
            self.remove_copy()


def cluster_deployment(spec: dict, data: Path, tf_vars: list[str], **deployment) -> Deployment:
    """The config a case's infra.cluster names, planned with its tfvars,
    tfvars_inline and variables, then tf_vars, later ones winning. The inline
    tfvars go in data."""
    cluster_dir = CLUSTERS_DIR / spec["name"]
    args = []
    if "tfvars" in spec:
        args.append(f"-var-file={cluster_dir / spec['tfvars']}")
    if "tfvars_inline" in spec:
        inline = data / "inline.tfvars"
        inline.write_text(spec["tfvars_inline"])
        args.append(f"-var-file={inline}")
    # A string as it is; anything else as JSON, which terraform reads as HCL.
    args += [
        f"-var={key}={value if isinstance(value, str) else json.dumps(value)}"
        for key, value in spec.get("variables", {}).items()
    ]
    return Deployment(dir=cluster_dir, data=data, plan_args=args + tf_vars, **deployment)
