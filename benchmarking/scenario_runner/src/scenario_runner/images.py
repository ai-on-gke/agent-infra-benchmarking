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

"""Names the images of the benchmarking images.json files in a repository,
each <repo>/benchmark/<name>:<tag>, as builder.py pushes them; checks they
are there, and pins one to the digest its registry serves."""

from __future__ import annotations

import functools
import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import RUNNER_DIR
from .process import output

ROOT = RUNNER_DIR.parent.parent
LOCUST_IMAGES = ROOT / "benchmarking" / "locust" / "images.json"
WORKLOADS_IMAGES = ROOT / "benchmarking" / "workloads" / "images.json"
SANDBOX_BENCHMARK_IMAGES = ROOT / "benchmarking" / "sandbox-benchmark" / "images.json"
IMAGE_FILES = [LOCUST_IMAGES, WORKLOADS_IMAGES, SANDBOX_BENCHMARK_IMAGES]
# The submodules the images build from besides substrate, whose uncommitted
# changes the tag covers when they are checked out.
OTHER_SOURCES = [ROOT / "dependency-repos" / "env", ROOT / "dependency-repos" / "ai-sandbox-benchmark"]
# The nodes', whatever the build host's.
DEFAULT_PLATFORM = "linux/amd64"
# Of the uncommitted changes' digest in a dirty tree's image tag, which a
# Docker tag (128) and the commit label (63) both take with the commit.
DIRTY_DIGEST_LENGTH = 12
# An Artifact Registry Docker repository's image path prefix.
DOCKER_REPO = re.compile(r"(?P<location>[a-z0-9-]+)-docker\.pkg\.dev/(?P<project>[^/]+)/(?P<repository>[^/]+)")


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


def missing_requirements() -> list[str]:
    return [tool for tool in ("git", "gcloud") if not shutil.which(tool)]


def repository_name(docker_repo: str) -> str:
    """The full resource name of the repository an image path prefix such as
    us-central1-docker.pkg.dev/my-project/my-repo is of."""
    match = DOCKER_REPO.fullmatch(docker_repo)
    if not match:
        raise ValueError(f"{docker_repo!r} is not <location>-docker.pkg.dev/<project>/<repository>")
    return f"projects/{match['project']}/locations/{match['location']}/repositories/{match['repository']}"


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
def commit() -> str:
    """The commit the repo is at. Read once, so a commit made during a run
    doesn't change it between the build and what reads it."""
    return output(["git", "rev-parse", "HEAD"], cwd=ROOT).strip()


def changes_digest(repo: Path) -> str:
    """A digest of repo's uncommitted changes, untracked files included and
    ignored ones not; empty for a clean tree."""
    diff = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=repo, capture_output=True, check=True).stdout
    untracked = output(["git", "ls-files", "-z", "--others", "--exclude-standard"], cwd=repo).split("\0")
    if not diff and not any(untracked):
        return ""
    digest = hashlib.sha256(diff)
    for path in sorted(p for p in untracked if p):
        digest.update(path.encode() + b"\0")
        if (repo / path).is_file():
            digest.update(hashlib.sha256((repo / path).read_bytes()).digest())
    return digest.hexdigest()


@functools.cache
def tag() -> str:
    """What the images built from this tree are tagged: the commit, and for a
    tree with uncommitted changes, here or in the submodules the images build
    from, -dirty-<digest of them>. So a manifest names the image built from
    the code that rendered it, and an image already pushed under it is of
    exactly this code. Read once, like commit."""
    from . import substrate

    sources = [ROOT, substrate.SUBSTRATE_DIR, *(path for path in OTHER_SOURCES if (path / ".git").exists())]
    changes = "".join(changes_digest(path) for path in sources)
    if not changes:
        return commit()
    return f"{commit()}-dirty-{hashlib.sha256(changes.encode()).hexdigest()[:DIRTY_DIGEST_LENGTH]}"


def ref(repo: str, name: str, image_tag: str | None = None) -> str:
    return f"{repo}/{name}:{image_tag or tag()}"


def group_repo(repo: str, group: str) -> str:
    """The folder of repo a group of images goes in: substrate, env,
    benchmark or agent-sandbox."""
    return f"{repo}/{group}"


def image_ref(file: Path, repo: str, name: str) -> str:
    """As ref, for an image that has to be one of file's."""
    if name not in json.loads(file.read_text()):
        raise ImageError(f"no image {name} in {file}")
    return ref(group_repo(repo, "benchmark"), name)


def digest(image: str) -> str | None:
    """The digest the registry serves for image, or None if it has none or
    gcloud's account cannot read it."""
    describe = ["gcloud", "artifacts", "docker", "images", "describe", image, "--format=value(image_summary.digest)"]
    proc = subprocess.run(describe, capture_output=True, text=True)
    value = proc.stdout.strip()
    return value if not proc.returncode and value.startswith("sha256:") else None


def refs(repo: str, files: list[Path]) -> list[str]:
    """The images of files in repo's benchmark folder, tagged tag()."""
    return [ref(group_repo(repo, "benchmark"), image.name) for image in read_images(*files)]


def missing(image_refs: list[str]) -> list[str]:
    """The images the registry has no digest for, or gcloud's account cannot read."""
    return [image for image in image_refs if digest(image) is None]


def pin(image: str) -> str:
    """The image pinned to the digest its registry serves for its tag now, so
    what reads it and what runs it are the same bits even if the tag moves."""
    image_digest = digest(image)
    if image_digest is None:
        raise ImageError(f"cannot resolve {image} to a digest; is it pushed, and can gcloud's account read it?")
    # Only the last path segment can carry a tag, which leaves a registry port alone.
    head, _, last = image.split("@", 1)[0].rpartition("/")
    return f"{head}/{last.split(':', 1)[0]}@{image_digest}"
