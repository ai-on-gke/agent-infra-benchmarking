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

"""Builds and pushes the images of the benchmarking images.json files to a
cluster's repository, with local Docker or on Cloud Build, each named
<repo>/<name>:<commit>; and pins one to the digest its registry serves."""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import RUNNER_DIR
from .cluster import Cluster
from .log import log
from .process import output, stream

ROOT = RUNNER_DIR.parent.parent
LOCUST_IMAGES = ROOT / "benchmarking" / "locust" / "images.json"
WORKLOADS_IMAGES = ROOT / "benchmarking" / "workloads" / "images.json"
# The nodes', whatever the build host's.
DEFAULT_PLATFORM = "linux/amd64"
CLOUD_BUILDER = "gcr.io/cloud-builders/docker:24.0.9"
# What tar --exclude-vcs leaves out of the Cloud Build source.
VCS_NAMES = {".git", ".gitignore", ".gitattributes", ".gitmodules"}


class ImageError(RuntimeError):
    pass


@dataclass(frozen=True)
class Image:
    """An entry of an images.json, its paths relative to the repo root."""

    name: str
    dockerfile: str
    context: str
    build_contexts: dict[str, str] = field(default_factory=dict)
    build_args: dict[str, str] = field(default_factory=dict)
    target: str | None = None
    args: list[str] = field(default_factory=list)
    platform: str | None = None
    description: str = ""

    def docker_build_args(self, ref: str, platform: str) -> list[str]:
        args = ["build", f"--platform={self.platform or platform}", f"--tag={ref}", f"--file={self.dockerfile}"]
        args += [f"--build-context={key}={value}" for key, value in self.build_contexts.items()]
        args += [f"--build-arg={key}={value}" for key, value in self.build_args.items()]
        if self.target:
            args.append(f"--target={self.target}")
        return [*args, *self.args, self.context]

    def sources(self) -> list[str]:
        """The paths its build reads."""
        paths = [self.context, *self.build_contexts.values()]
        if not self.dockerfile.startswith(self.context + "/"):
            paths.append(self.dockerfile)
        return paths


def missing_requirements(cloud_build: bool = False) -> list[str]:
    tools = ["git", "gcloud"] + ([] if cloud_build else ["docker"])
    return [tool for tool in tools if not shutil.which(tool)]


def read_images(*files: Path) -> list[Image]:
    """The images of every images.json, whose names have to be unique across
    them."""
    images: dict[str, Image] = {}
    for path in files:
        for name, entry in json.loads(path.read_text()).items():
            if name in images:
                raise ValueError(f"image {name} is in more than one of {', '.join(map(str, files))}")
            try:
                images[name] = Image(name=name, **entry)
            except TypeError as exc:
                raise ValueError(f"{path}: image {name}: {exc}") from None
    return list(images.values())


@functools.cache
def tag() -> str:
    """The commit the repo is at, so a manifest names the image built from the
    code that rendered it. Read once, so a commit made during a run doesn't
    change the tag between the build and what reads it."""
    return output(["git", "rev-parse", "HEAD"], cwd=ROOT).strip()


def ref(repo: str, name: str, image_tag: str | None = None) -> str:
    return f"{repo}/{name}:{image_tag or tag()}"


def image_ref(file: Path, repo: str, name: str) -> str:
    """As ref, for an image that has to be one of file's."""
    if name not in json.loads(file.read_text()):
        raise ImageError(f"no image {name} in {file}")
    return ref(repo, name)


def pin(image: str) -> str:
    """The image pinned to the digest its registry serves for its tag now, so
    what reads it and what runs it are the same bits even if the tag moves."""
    describe = ["gcloud", "artifacts", "docker", "images", "describe", image, "--format=value(image_summary.digest)"]
    try:
        digest = output(describe).strip()
    except subprocess.CalledProcessError:
        digest = ""
    if not digest.startswith("sha256:"):
        raise ImageError(f"cannot resolve {image} to a digest; is it pushed, and can gcloud's account read it?")
    # Only the last path segment can carry a tag, which leaves a registry port alone.
    head, _, last = image.split("@", 1)[0].rpartition("/")
    return f"{head}/{last.split(':', 1)[0]}@{digest}"


def access_token(host: str) -> str:
    """gcloud's access token, to log in to host with as oauth2accesstoken."""
    token = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True)
    if token.returncode or not token.stdout.strip():
        raise ImageError(f"cannot get an access token from gcloud for {host}; run gcloud auth login")
    account = subprocess.run(["gcloud", "config", "get-value", "account"], capture_output=True, text=True)
    log(f"images: authenticating to {host} as {account.stdout.strip() or 'an unknown account'}")
    return token.stdout.strip()


def docker_login(host: str) -> None:
    """Rather than relying on gcloud auth configure-docker having been run for
    the host, which docker looks credentials up by."""
    stream(
        ["docker", "login", "--username=oauth2accesstoken", "--password-stdin", f"https://{host}"],
        input=access_token(host),
    )


def docker_build_and_push(images: list[Image], repo: str, image_tag: str, platform: str, login: bool) -> None:
    logged_in: set[str] = set()
    for image in images:
        image_ref = ref(repo, image.name, image_tag)
        host = image_ref.split("/", 1)[0]
        if login and host not in logged_in:
            docker_login(host)
            logged_in.add(host)
        log(f"images: building {image_ref} (platform: {image.platform or platform})")
        stream(["docker", *image.docker_build_args(image_ref, platform)], cwd=ROOT)
        log(f"images: pushing {image_ref}")
        stream(["docker", "push", image_ref])


def without_vcs(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    return None if Path(info.name).name in VCS_NAMES else info


def cloud_build_and_push(images: list[Image], repo: str, image_tag: str, platform: str, project_id: str) -> None:
    """Runs the same docker builds as one Cloud Build, which pushes them only
    once all have succeeded. It uploads only the paths they read, at their
    repo-relative paths, so their arguments are the local ones unchanged."""
    refs = [ref(repo, image.name, image_tag) for image in images]
    config = {
        "steps": [
            {
                "id": image.name,
                "name": CLOUD_BUILDER,
                # --build-context needs BuildKit, which docker 24 doesn't default to.
                "env": ["DOCKER_BUILDKIT=1"],
                "args": image.docker_build_args(image_ref, platform),
                "waitFor": ["-"],
            }
            for image, image_ref in zip(images, refs)
        ],
        "images": refs,
        "timeout": "3600s",
        "options": {"machineType": "E2_HIGHCPU_8"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.tgz"
        with tarfile.open(source, "w:gz") as tar:
            for path in sorted({p for image in images for p in image.sources()}):
                tar.add(ROOT / path, arcname=path, filter=without_vcs)
        config_file = Path(tmp) / "cloudbuild.json"
        config_file.write_text(json.dumps(config))

        builds = [f"  {r} (platform: {image.platform or platform})" for image, r in zip(images, refs)]
        log("images: building on Cloud Build:\n" + "\n".join(builds))
        # gcloud holds the build log back until the end when stdout isn't a terminal.
        stream(
            ["gcloud", "builds", "submit", str(source), f"--config={config_file}", f"--project={project_id}"],
            env={"PYTHONUNBUFFERED": "1"},
        )


def build_and_push(
    cluster: Cluster,
    files: list[Path],
    cloud_build: bool = False,
    login: bool = True,
    platform: str = DEFAULT_PLATFORM,
) -> None:
    """Builds and pushes the images of files to the cluster's repository.
    cloud_build is for a host without a Docker daemon; without login, a local
    build uses the registry credentials Docker already has."""
    images = read_images(*files)
    if cloud_build:
        cloud_build_and_push(images, cluster.docker_repo, tag(), platform, cluster.project_id)
    else:
        docker_build_and_push(images, cluster.docker_repo, tag(), platform, login)
