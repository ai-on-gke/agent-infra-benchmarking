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

"""Applies and destroys the cluster a tests.yaml case runs on, the
infra/clusters/ config its infra.cluster names."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import terraform
from .cases import CLUSTERS_DIR


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
