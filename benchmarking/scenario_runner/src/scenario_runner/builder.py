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

"""Builds the images the scenario-runner pulls and pushes them to a
repository ahead of its runs, with local Docker and ko, each
under the name and tag images.py, substrate.py, ate_env.py and
agent_sandbox.py look it up by. The groups are substrate's, env's,
agent-sandbox's, and the benchmark's own of the images.json files."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence

from . import agent_sandbox, ate_env, images, substrate
from .images import Image, ImageError
from .log import log
from .process import stream

GROUPS = ("substrate", "env", "agent-sandbox", "benchmark")
# The ko substrate's hack/tools/ko pins.
KO = "github.com/google/ko@v0.19.1"
# Where ko stamps substrate's build version, as ate-setup's ko runner does.
VERSION_PKG = "github.com/agent-substrate/substrate/internal/version"


def missing_requirements(groups: list[str]) -> list[str]:
    tools = ["git", "gcloud", "docker"]
    if {"substrate", "env"} & set(groups):
        tools.append("go")
    missing = [tool for tool in tools if not shutil.which(tool)]
    if "substrate" in groups:
        missing += substrate.missing_submodule()
    if "env" in groups and not (ate_env.ENV_DIR / "go.mod").is_file():
        missing.append(f"the env submodule in {ate_env.ENV_DIR}")
    if "agent-sandbox" in groups:
        missing += agent_sandbox.missing_submodule()
    return missing


def refs(group: str, repo: str) -> list[str]:
    """The images of group in repo, as the scenario-runner looks them up."""
    if group == "substrate":
        return substrate.image_refs(repo)
    if group == "env":
        return ate_env.image_refs(repo)
    if group == "agent-sandbox":
        return agent_sandbox.image_refs(repo)
    return images.refs(repo, images.IMAGE_FILES)


def dirty(group: str) -> bool:
    """Whether group's tag is git describe's -dirty, which stays the same
    however the uncommitted changes change, so an image under it may be of
    other ones. The benchmark's tag has a digest of the changes in it."""
    if group == "substrate":
        return substrate.build_version().endswith("-dirty")
    if group == "env":
        return ate_env.build_version().endswith("-dirty")
    if group == "agent-sandbox":
        return agent_sandbox.build_version().endswith("-dirty")
    return False


def build(group: str, repo: str, image_refs: list[str], login: bool, cache_args: Sequence[str] = ()) -> None:
    """Builds and pushes group's images to repo: of the benchmark's, only
    image_refs; of the others, all of them. cache_args go to each docker
    build, as docker_build_and_push says; ko's builds take none."""
    if group == "substrate":
        build_substrate(repo, login, cache_args)
    elif group == "env":
        build_env(repo, login)
    elif group == "agent-sandbox":
        build_agent_sandbox(repo, login, cache_args)
    else:
        repo = images.group_repo(repo, group)
        wanted = [i for i in images.read_images(*images.IMAGE_FILES) if images.ref(repo, i.name) in image_refs]
        docker_build_and_push(wanted, repo, images.tag(), images.DEFAULT_PLATFORM, login, cache_args)


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


def docker_build_and_push(
    to_build: list[Image], repo: str, image_tag: str, platform: str, login: bool, cache_args: Sequence[str] = ()
) -> None:
    """Without login, uses the registry credentials Docker already has.
    cache_args, such as --cache-from=type=gha,scope={image}, go to every build
    with {image} as the image's name. The build pushes, as the
    docker-container builder a cache export takes loads nothing into Docker."""
    logged_in: set[str] = set()
    for image in to_build:
        image_ref = images.ref(repo, image.name, image_tag)
        host = image_ref.split("/", 1)[0]
        if login and host not in logged_in:
            docker_login(host)
            logged_in.add(host)
        log(f"images: building and pushing {image_ref} (platform: {image.platform or platform})")
        cache = [arg.replace("{image}", image.name) for arg in cache_args]
        build = image.docker_build_args(image_ref, platform)
        # buildx, as docker build ignores the builder docker buildx use selects.
        stream(["docker", "buildx", *build[:-1], "--push", "--provenance=false", *cache, build[-1]], cwd=images.ROOT)


def build_substrate(repo: str, login: bool, cache_args: Sequence[str] = ()) -> None:
    """Pushes substrate.image_refs, with ko and Docker."""
    repo = images.group_repo(repo, "substrate")
    version = substrate.build_version()
    log(f"substrate: building the {version} images into {repo}")
    ko_build = ["hack/run-tool.sh", "ko", "build", "--base-import-paths", f"--tags={version}",
                f"--ldflags=-X={VERSION_PKG}.Version={version}", *substrate.SUBSTRATE_PACKAGES,
                *substrate.WORKLOAD_PACKAGES]  # fmt: skip
    env = {"KO_DEFAULTPLATFORMS": images.DEFAULT_PLATFORM, "KO_DOCKER_REPO": repo}
    if login:
        # ko login, as ko doesn't push with docker.
        host = repo.split("/", 1)[0]
        ko = [str(substrate.SUBSTRATE_DIR / "hack" / "run-tool.sh"), "ko", "login", host]
        token = access_token(host)
        stream([*ko, "--username=oauth2accesstoken", "--password-stdin"], cwd=substrate.SUBSTRATE_DIR, input=token)
    stream(ko_build, env=env, cwd=substrate.SUBSTRATE_DIR)
    docker_build_and_push([substrate.ENVOY_DATAPLANE], repo, version, images.DEFAULT_PLATFORM, login, cache_args)


def build_agent_sandbox(repo: str, login: bool, cache_args: Sequence[str] = ()) -> None:
    """Pushes agent_sandbox.image_refs."""
    repo = images.group_repo(repo, "agent-sandbox")
    version = agent_sandbox.build_version()
    log(f"agent-sandbox: building the {version} images into {repo}")
    docker_build_and_push(agent_sandbox.IMAGES, repo, version, images.DEFAULT_PLATFORM, login, cache_args)


def build_env(repo: str, login: bool) -> None:
    """Pushes ate_env.image_refs."""
    repo = images.group_repo(repo, "env")
    ko_build = ["go", "run", KO, "build", "--base-import-paths", f"--tags={ate_env.build_version()}", *ate_env.PACKAGES]
    env = {"KO_DEFAULTPLATFORMS": images.DEFAULT_PLATFORM, "KO_DOCKER_REPO": repo}
    log(f"env: building {', '.join(ate_env.PACKAGES)} into {repo}")
    if login:
        # ko login, as ko doesn't push with docker.
        host = repo.split("/", 1)[0]
        ko_login = ["go", "run", KO, "login", host, "--username=oauth2accesstoken", "--password-stdin"]
        stream(ko_login, cwd=ate_env.ENV_DIR, input=access_token(host))
    stream(ko_build, env=env, cwd=ate_env.ENV_DIR)
