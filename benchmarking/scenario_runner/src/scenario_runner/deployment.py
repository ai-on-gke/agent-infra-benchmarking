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
    DESTROYED  an empty file, there once it is destroyed until it is applied again

Every path in it is relative, so the folder can be moved, and terraform runs
on it by hand from config/ as well:

    TF_DATA_DIR=../tf-data terraform plan

A GcsDeployment keeps the same layout in a GCS bucket, its state included.
"""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Iterator, Self

from google.cloud import storage

from . import RUNNER_DIR, terraform
from .cases import CLUSTERS_DIR
from .log import log
from .process import retry

RUNNER_INFRA_DIR = RUNNER_DIR / "infra"
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
STATES_DIR = "tfstates"
# The gcs backend's name for the default workspace's state, which the local
# backend's is given too.
STATE_FILE = "default.tfstate"
DESTROYED_FILE = "DESTROYED"
# Not in a GcsDeployment's bucket as a copy: the state, which the backend
# keeps there itself, and the providers, links into the plugin cache that init
# makes again.
UNSYNCED = (f"{STATES_DIR}/", "tf-data/providers/")


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
        return self.root / STATES_DIR / STATE_FILE

    @property
    def destroyed_flag(self) -> Path:
        return self.root / DESTROYED_FILE

    @property
    def created(self) -> bool:
        return self.config.is_dir()

    @property
    def destroyed(self) -> bool:
        return self.destroyed_flag.exists()

    @property
    def backend(self) -> tuple[str, dict[str, str]]:
        # Relative to config/, where terraform runs.
        return "local", {"path": f"../{STATES_DIR}/{STATE_FILE}"}

    def create(
        self,
        source: Path,
        variables: dict,
        *,
        tfvars_file: Path | None = None,
        tfvars_inline: str | None = None,
    ) -> Self:
        """Copies source into config/, replacing any copy already there, with
        the backend and the variables it is applied with: tfvars_file's, then
        tfvars_inline's, then variables, later ones winning. Any tf-data/ and
        tfstates/ already in root are kept."""
        shutil.rmtree(self.config, ignore_errors=True)
        shutil.copytree(source, self.config, ignore=shutil.ignore_patterns(*FOREIGN_FILES))
        (self.config / "backend_override.tf").write_text(terraform.backend_block(*self.backend))
        if tfvars_file is not None:
            name = TFVARS_FILE + (".json" if tfvars_file.suffix == ".json" else "")
            shutil.copyfile(tfvars_file, self.config / name)
        if tfvars_inline is not None:
            (self.config / TFVARS_INLINE_FILE).write_text(tfvars_inline)
        (self.config / VARIABLES_FILE).write_text(json.dumps(variables, indent=2) + "\n")
        self.data.mkdir(parents=True, exist_ok=True)
        return self

    def env(self) -> dict[str, str]:
        return terraform.data_env(self.data)

    def init(self) -> None:
        # create may have replaced the backend tf-data/ was initialized with.
        retry(str(self.root), lambda: terraform.run(self.config, "init", "-input=false", "-reconfigure", env=self.env()))

    def deploy(self, *args: str) -> None:
        """Plans and applies, with -destroy a destroy, which flags it destroyed
        once it succeeds. A failed plan or apply is planned again from the
        state it left."""
        destroying = "-destroy" in args
        if not destroying:
            # Unflagged before, as even an apply that fails may create part of it.
            self.destroyed_flag.unlink(missing_ok=True)
        retry(
            str(self.root),
            lambda: terraform.deploy(
                self.config,
                *args,
                json_output=self.json_output,
                env=self.env(),
                plan_file=str(self.data / "run.tfplan"),
            ),
        )
        if destroying:
            self.destroyed_flag.touch()

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


@cache
def storage_client() -> storage.Client:
    return storage.Client()


def synced(path: str) -> bool:
    return not path.startswith(UNSYNCED)


@dataclass(frozen=True, kw_only=True)
class GcsDeployment(Deployment):
    """A Deployment that keeps its root in a GCS bucket under prefix too, laid
    out the same: its state, which the gcs backend keeps in tfstates/ there,
    and a copy of the rest of the root. The copy is uploaded before every
    apply and after a destroy, and downloaded before a destroy, so a destroy
    needs nothing but the bucket."""

    bucket: str
    prefix: str

    @property
    def url(self) -> str:
        return f"gs://{self.bucket}/{self.prefix}"

    @property
    def backend(self) -> tuple[str, dict[str, str]]:
        # The backend names the state <prefix>/default.tfstate.
        return "gcs", {"bucket": self.bucket, "prefix": f"{self.prefix}/{STATES_DIR}"}

    def state(self) -> dict:
        """The state in the bucket, empty before the first apply."""
        blob = storage_client().bucket(self.bucket).blob(f"{self.prefix}/{STATES_DIR}/{STATE_FILE}")
        return json.loads(blob.download_as_text()) if blob.exists() else {}

    def local_files(self) -> dict[str, Path]:
        """The root's files to copy, by their path in it."""
        paths = {path.relative_to(self.root).as_posix(): path for path in self.root.rglob("*") if path.is_file()}
        return {name: path for name, path in paths.items() if synced(name)}

    def remote_files(self) -> dict[str, storage.Blob]:
        """The bucket's copies, by their path in the root."""
        blobs = storage_client().list_blobs(self.bucket, prefix=f"{self.prefix}/")
        named = {blob.name.removeprefix(f"{self.prefix}/"): blob for blob in blobs}
        return {name: blob for name, blob in named.items() if synced(name)}

    def upload(self) -> None:
        """Makes the bucket's copy of the root the same as the root."""
        local, remote = self.local_files(), self.remote_files()
        bucket = storage_client().bucket(self.bucket)
        for name, path in local.items():
            bucket.blob(f"{self.prefix}/{name}").upload_from_filename(path)
        for name in remote.keys() - local.keys():
            remote[name].delete()
        log(f"Uploaded {self.root} to {self.url}")

    def download(self) -> None:
        """Makes the root the same as the bucket's copy of it."""
        local, remote = self.local_files(), self.remote_files()
        for name, blob in remote.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(path)
        for name in local.keys() - remote.keys():
            local[name].unlink()
        log(f"Downloaded {self.url} to {self.root}")

    def deploy(self, *args: str) -> None:
        destroying = "-destroy" in args
        if not destroying:
            # The DESTROYED flag leaves the bucket's copy too before anything
            # is created.
            self.destroyed_flag.unlink(missing_ok=True)
            self.upload()
        super().deploy(*args)
        if destroying:
            self.upload()

    def destroy(self) -> None:
        self.download()
        super().destroy()


def cluster_deployment(
    spec: dict, deployment: Deployment, tf_vars: dict, substrate_version: str | None, substrate_repository: str | None
) -> Deployment:
    """Creates deployment of the config a case's infra.cluster names, with its
    tfvars, tfvars_inline and variables, then substrate_version and the
    substrate_repository its images are installed from unless None, and
    tf_vars, later ones winning."""
    cluster_dir = CLUSTERS_DIR / spec["name"]
    variables = dict(spec.get("variables", {}))
    if substrate_version is not None:
        variables["substrate_version"] = substrate_version
        variables["substrate_image_repository"] = substrate_repository
    variables.update(tf_vars)
    return deployment.create(
        cluster_dir,
        variables,
        # An absolute path stays as it is.
        tfvars_file=cluster_dir / spec["tfvars"] if "tfvars" in spec else None,
        tfvars_inline=spec.get("tfvars_inline"),
    )
