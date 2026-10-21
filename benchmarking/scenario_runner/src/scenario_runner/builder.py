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
repository ahead of its runs, with local Docker and ko or on Cloud Build, each
under the name and tag images.py, substrate.py, ate_env.py and
agent_sandbox.py look it up by. The groups are substrate's, env's,
agent-sandbox's, and the benchmark's own of the images.json files."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from . import agent_sandbox, ate_env, images, substrate
from .images import Image, ImageError
from .log import log
from .process import output, stream

GROUPS = ("substrate", "env", "agent-sandbox", "benchmark")
# By digest as well as tag, so a rebuilt tag cannot change the build.
CLOUD_BUILDER = "gcr.io/cloud-builders/docker:24.0.9@sha256:9fcfbddaf676527b2be4a6473bb37ed028a88bc55bc5cd9dcc9db4f7d9bcebee"
# Of the go.mod's Go version, with git.
GO_BUILDER = "golang:1.27.2@sha256:007a316059246276cb214df93950be77837545dc78007a73e0291a4963d5d3bf"
# The ko substrate's hack/tools/ko pins.
KO = "github.com/google/ko@v0.19.1"
# Where ko stamps substrate's build version, as ate-setup's ko runner does.
VERSION_PKG = "github.com/agent-substrate/substrate/internal/version"
# What tar --exclude-vcs leaves out of the Cloud Build source.
VCS_NAMES = {".git", ".gitignore", ".gitattributes", ".gitmodules"}


def missing_requirements(groups: list[str], cloud_build: bool = False) -> list[str]:
    tools = ["git", "gcloud"] + ([] if cloud_build else ["docker"])
    if not cloud_build and {"substrate", "env"} & set(groups):
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


def build(group: str, repo: str, image_refs: list[str], project_id: str, cloud_build: bool, login: bool) -> None:
    """Builds and pushes group's images to repo: of the benchmark's, only
    image_refs; of the others, all of them."""
    if group == "substrate":
        build_substrate(repo, project_id, cloud_build, login)
    elif group == "env":
        build_env(repo, project_id, cloud_build, login)
    elif group == "agent-sandbox":
        build_agent_sandbox(repo, project_id, cloud_build, login)
    else:
        repo = images.group_repo(repo, group)
        wanted = [i for i in images.read_images(*images.IMAGE_FILES) if images.ref(repo, i.name) in image_refs]
        build_and_push(wanted, repo, images.tag(), project_id, cloud_build, login)


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


def docker_build_and_push(to_build: list[Image], repo: str, image_tag: str, platform: str, login: bool) -> None:
    logged_in: set[str] = set()
    for image in to_build:
        image_ref = images.ref(repo, image.name, image_tag)
        host = image_ref.split("/", 1)[0]
        if login and host not in logged_in:
            docker_login(host)
            logged_in.add(host)
        log(f"images: building {image_ref} (platform: {image.platform or platform})")
        stream(["docker", *image.docker_build_args(image_ref, platform)], cwd=images.ROOT)
        log(f"images: pushing {image_ref}")
        stream(["docker", "push", image_ref])


def without_vcs(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    return None if Path(info.name).name in VCS_NAMES else info


def cloud_build_step(image: Image, image_ref: str, platform: str) -> dict:
    """The Cloud Build step of image's docker build, run alongside the others."""
    return {
        "id": image.name,
        "name": CLOUD_BUILDER,
        # --build-context needs BuildKit, which docker 24 doesn't default to.
        "env": ["DOCKER_BUILDKIT=1"],
        "args": image.docker_build_args(image_ref, platform),
        "waitFor": ["-"],
    }


def cloud_build_submit(steps: list[dict], pushed: list[str], paths: list[str], project_id: str) -> None:
    """Runs steps as one Cloud Build, as the project's default Cloud Build
    account, which pushes the pushed images only once all have succeeded. It
    uploads only paths, at their repo-relative paths, so the steps' arguments
    are the local ones unchanged."""
    # Cloud Logging, as streaming from the default logs bucket needs project Viewer.
    options = {"machineType": "E2_HIGHCPU_8", "logging": "CLOUD_LOGGING_ONLY"}
    config = {"steps": steps, "images": pushed, "timeout": "3600s", "options": options}
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.tgz"
        with tarfile.open(source, "w:gz") as tar:
            for path in sorted(set(paths)):
                tar.add(images.ROOT / path, arcname=path, filter=without_vcs)
        config_file = Path(tmp) / "cloudbuild.json"
        config_file.write_text(json.dumps(config))
        submit = ["gcloud", "builds", "submit", str(source), f"--config={config_file}", f"--project={project_id}"]
        # gcloud holds the build log back until the end when stdout isn't a terminal.
        stream(submit, env={"PYTHONUNBUFFERED": "1"})


def cloud_build_and_push(to_build: list[Image], repo: str, image_tag: str, platform: str, project_id: str) -> None:
    """Runs the same docker builds as one Cloud Build."""
    image_refs = [images.ref(repo, image.name, image_tag) for image in to_build]
    builds = [f"  {r} (platform: {image.platform or platform})" for image, r in zip(to_build, image_refs)]
    log("images: building on Cloud Build:\n" + "\n".join(builds))
    steps = [cloud_build_step(image, image_ref, platform) for image, image_ref in zip(to_build, image_refs)]
    cloud_build_submit(steps, image_refs, [p for image in to_build for p in image.sources()], project_id)


def build_and_push(
    to_build: list[Image], repo: str, image_tag: str, project_id: str, cloud_build: bool, login: bool
) -> None:
    """cloud_build is for a host without a Docker daemon; without login, a
    local build uses the registry credentials Docker already has."""
    if cloud_build:
        cloud_build_and_push(to_build, repo, image_tag, images.DEFAULT_PLATFORM, project_id)
    else:
        docker_build_and_push(to_build, repo, image_tag, images.DEFAULT_PLATFORM, login)


def submodule_paths(submodule: Path) -> list[str]:
    """The submodule's files git doesn't ignore, less the ones deleted in the
    working tree, by their repo-relative paths."""
    files = output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=submodule)
    path = submodule.relative_to(images.ROOT).as_posix()
    return [f"{path}/{file}" for file in files.split("\0") if file and os.path.lexists(submodule / file)]


def build_substrate(repo: str, project_id: str, cloud_build: bool, login: bool) -> None:
    """Pushes substrate.image_refs: with cloud_build all of them on Cloud
    Build, else with ko and Docker here."""
    repo = images.group_repo(repo, "substrate")
    version = substrate.build_version()
    log(f"substrate: building the {version} images into {repo}")
    ko_build = ["hack/run-tool.sh", "ko", "build", "--base-import-paths", f"--tags={version}",
                f"--ldflags=-X={VERSION_PKG}.Version={version}", *substrate.SUBSTRATE_PACKAGES,
                *substrate.WORKLOAD_PACKAGES]  # fmt: skip
    env = {"KO_DEFAULTPLATFORMS": images.DEFAULT_PLATFORM, "KO_DOCKER_REPO": repo}
    if cloud_build:
        cloud_build_substrate(ko_build, env, repo, version, project_id)
        return
    if login:
        # ko login, as ko doesn't push with docker.
        host = repo.split("/", 1)[0]
        ko = [str(substrate.SUBSTRATE_DIR / "hack" / "run-tool.sh"), "ko", "login", host]
        token = access_token(host)
        stream([*ko, "--username=oauth2accesstoken", "--password-stdin"], cwd=substrate.SUBSTRATE_DIR, input=token)
    stream(ko_build, env=env, cwd=substrate.SUBSTRATE_DIR)
    docker_build_and_push([substrate.ENVOY_DATAPLANE], repo, version, images.DEFAULT_PLATFORM, login)


def cloud_build_substrate(ko_build: list[str], env: dict, repo: str, version: str, project_id: str) -> None:
    """Runs the ko build alongside envoy-dataplane's docker build as one Cloud
    Build, uploading the submodule's files git doesn't ignore. ko pushes as the
    build's service account, through its Google keychain."""
    envoy = images.ref(repo, substrate.ENVOY_DATAPLANE.name, version)
    # hack/run-tool.sh finds the tools from the repo's top level, and the
    # upload has no .git. Its files keep the uploader's owner, not root.
    git = ["git config --global --add safe.directory '*'", "git init -q"]
    ko = {
        "id": "ko",
        "name": GO_BUILDER,
        "dir": substrate.SUBSTRATE_PATH,
        "entrypoint": "bash",
        "args": ["-c", " && ".join([*git, shlex.join(ko_build)])],
        # VCS stamping fails in a repository without commits.
        "env": [f"{key}={value}" for key, value in env.items()] + ["GOFLAGS=-buildvcs=false"],
        "waitFor": ["-"],
    }
    log(f"substrate: building on Cloud Build, envoy-dataplane as {envoy}")
    envoy_step = cloud_build_step(substrate.ENVOY_DATAPLANE, envoy, images.DEFAULT_PLATFORM)
    cloud_build_submit([ko, envoy_step], [envoy], submodule_paths(substrate.SUBSTRATE_DIR), project_id)


def build_agent_sandbox(repo: str, project_id: str, cloud_build: bool, login: bool) -> None:
    """Pushes agent_sandbox.image_refs."""
    repo = images.group_repo(repo, "agent-sandbox")
    version = agent_sandbox.build_version()
    log(f"agent-sandbox: building the {version} images into {repo}")
    build_and_push(agent_sandbox.IMAGES, repo, version, project_id, cloud_build, login)


def build_env(repo: str, project_id: str, cloud_build: bool, login: bool) -> None:
    """Pushes ate_env.image_refs."""
    repo = images.group_repo(repo, "env")
    ko_build = ["go", "run", KO, "build", "--base-import-paths", f"--tags={ate_env.build_version()}", *ate_env.PACKAGES]
    env = {"KO_DEFAULTPLATFORMS": images.DEFAULT_PLATFORM, "KO_DOCKER_REPO": repo}
    log(f"env: building {', '.join(ate_env.PACKAGES)} into {repo}")
    if cloud_build:
        cloud_build_env(ko_build, env, project_id)
        return
    if login:
        # ko login, as ko doesn't push with docker.
        host = repo.split("/", 1)[0]
        ko_login = ["go", "run", KO, "login", host, "--username=oauth2accesstoken", "--password-stdin"]
        stream(ko_login, cwd=ate_env.ENV_DIR, input=access_token(host))
    stream(ko_build, env=env, cwd=ate_env.ENV_DIR)


def cloud_build_env(ko_build: list[str], env: dict, project_id: str) -> None:
    """Runs the ko build on Cloud Build, uploading the submodule's files git
    doesn't ignore. ko pushes as the build's service account."""
    step = {
        "id": "ko",
        "name": GO_BUILDER,
        "dir": ate_env.ENV_PATH,
        "entrypoint": ko_build[0],
        "args": ko_build[1:],
        # VCS stamping fails in the upload, which has no .git.
        "env": [f"{key}={value}" for key, value in env.items()] + ["GOFLAGS=-buildvcs=false"],
        "waitFor": ["-"],
    }
    cloud_build_submit([step], [], submodule_paths(ate_env.ENV_DIR), project_id)
