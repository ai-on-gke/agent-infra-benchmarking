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

"""A deployment of a Terraform config, scenario_runner/infra or the
infra/clusters/ one a case's infra.cluster names, kept whole in a root folder:

    config/    a copy of the config, with its backend and variables in it
    tf-data/   its TF_DATA_DIR
    tfstates/  its state, with the local backend

Every path in it is relative, so the folder can be moved, and terraform runs
on it by hand from config/ as well:

    TF_DATA_DIR=../tf-data terraform plan
"""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import RUNNER_DIR, terraform
from .cases import CLUSTERS_DIR

ORCHESTRATOR_DIR = RUNNER_DIR / "infra"
# The config's own deployments' files, e.g. one applied by hand, which would
# change this one's backend, variables or state.
FOREIGN_FILES = (
    ".terraform",
    "override.tf",
    "override.tf.json",
    "*_override.tf",
    "*_override.tf.json",
    "terraform.tfvars",
    "terraform.tfvars.json",
    "*.auto.tfvars",
    "*.auto.tfvars.json",
    "*.tfstate",
    "*.tfstate.*",
)
# terraform reads them in this order, later ones winning.
TFVARS_FILE = "00-tfvars.auto.tfvars"
TFVARS_INLINE_FILE = "10-inline.auto.tfvars"
VARIABLES_FILE = "20-variables.auto.tfvars.json"
STATE_FILE = "terraform.tfstate"


def tf_vars(project_id: str, region: str, name: str) -> dict:
    """The variables every config takes, name naming its resources."""
    return {"project_id": project_id, "region": region, "default_resource_name": name}


@dataclass(frozen=True)
class Deployment:
    root: Path
    json_output: bool = False

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def data(self) -> Path:
        """The TF_DATA_DIR, which also holds the plan."""
        return self.root / "tf-data"

    @property
    def state_file(self) -> Path:
        """The state, with the local backend."""
        return self.root / "tfstates" / STATE_FILE

    @property
    def created(self) -> bool:
        return self.config.is_dir()

    @classmethod
    def create(
        cls,
        root: Path,
        source: Path,
        variables: dict,
        *,
        tfvars_file: Path | None = None,
        tfvars_inline: str | None = None,
        backend: str = "local",
        backend_config: dict[str, str] | None = None,
        json_output: bool = False,
    ) -> Deployment:
        """Copies source into root's config/, replacing any copy already
        there, with the backend and the variables it is applied with:
        tfvars_file's, then tfvars_inline's, then variables, later ones
        winning. Any tf-data/ and tfstates/ already in root are kept."""
        deployment = cls(root, json_output)
        shutil.rmtree(deployment.config, ignore_errors=True)
        shutil.copytree(source, deployment.config, ignore=shutil.ignore_patterns(*FOREIGN_FILES))
        if backend_config is None:
            # Relative to config/, where terraform runs.
            backend_config = {"path": str(Path("..") / deployment.state_file.relative_to(root))}
        (deployment.config / "backend_override.tf").write_text(terraform.backend_block(backend, backend_config))
        if tfvars_file is not None:
            name = TFVARS_FILE + (".json" if tfvars_file.suffix == ".json" else "")
            shutil.copyfile(tfvars_file, deployment.config / name)
        if tfvars_inline is not None:
            (deployment.config / TFVARS_INLINE_FILE).write_text(tfvars_inline)
        (deployment.config / VARIABLES_FILE).write_text(json.dumps(variables, indent=2) + "\n")
        deployment.data.mkdir(parents=True, exist_ok=True)
        return deployment

    def env(self) -> dict[str, str]:
        return terraform.data_env(self.data)

    def init(self) -> None:
        # create may have replaced the backend tf-data/ was initialized with.
        terraform.run(self.config, "init", "-input=false", "-reconfigure", env=self.env())

    def deploy(self, *args: str) -> None:
        terraform.deploy(
            self.config,
            *args,
            json_output=self.json_output,
            env=self.env(),
            plan_file=str(self.data / "run.tfplan"),
        )

    def outputs(self) -> dict:
        """Its outputs, by name; only after init."""
        return terraform.output(self.config, env=self.env())

    def state(self) -> dict:
        """The local backend's state, empty before the first apply."""
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else {}

    def state_outputs(self) -> dict:
        """Its outputs, by name, read off the local backend's state."""
        return {key: out["value"] for key, out in self.state().get("outputs", {}).items()}

    def apply(self) -> None:
        self.init()
        self.deploy()

    def destroy(self) -> None:
        self.init()
        self.deploy("-destroy")

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


def cluster_deployment(spec: dict, root: Path, tf_vars: dict, substrate_version: str | None, **create) -> Deployment:
    """Creates the deployment of the config a case's infra.cluster names, with
    its tfvars, tfvars_inline and variables, then substrate_version unless
    None and tf_vars, later ones winning."""
    cluster_dir = CLUSTERS_DIR / spec["name"]
    variables = dict(spec.get("variables", {}))
    if substrate_version is not None:
        variables["substrate_version"] = substrate_version
    variables.update(tf_vars)
    return Deployment.create(
        root,
        cluster_dir,
        variables,
        # An absolute path stays as it is.
        tfvars_file=cluster_dir / spec["tfvars"] if "tfvars" in spec else None,
        tfvars_inline=spec.get("tfvars_inline"),
        **create,
    )
