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

"""Applies and destroys a Terraform config: infra/orchestrator, or the
infra/clusters/ one a case's infra.cluster names."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import AUTOMATION_DIR, terraform
from .cases import CLUSTERS_DIR

ORCHESTRATOR_DIR = AUTOMATION_DIR / "infra" / "orchestrator"


@dataclass
class Deployment:
    """A Terraform config, applied with the state backend_config points to."""

    dir: Path
    # The TF_DATA_DIR, which also holds the plan.
    data: Path
    backend: str
    backend_config: list[str]
    # Names the backend override file, so unique to whoever applies it.
    owner: str
    plan_args: list[str]
    json_output: bool = False

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
            *self.plan_args,
            json_output=self.json_output,
            env=terraform.data_env(self.data),
            plan_file=str(self.data / "run.tfplan"),
        )

    def outputs(self) -> dict:
        """Its outputs, by name; only while initialized."""
        return terraform.output(self.dir, env=terraform.data_env(self.data))

    def apply(self) -> None:
        with self.initialized():
            self.deploy()

    def destroy(self) -> None:
        with self.initialized():
            self.deploy("-destroy")

    @contextmanager
    def applied(self) -> Iterator[None]:
        """Applies the config for the with block, and destroys it after, also
        after a failed apply, which may have created part of it."""
        with self.initialized():
            try:
                self.deploy()
                yield
            finally:
                self.deploy("-destroy")


def cluster(spec: dict, data: Path, tf_vars: list[str], **deployment) -> Deployment:
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
