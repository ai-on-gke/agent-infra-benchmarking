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

"""Builds the submodule's images once per execution, and installs substrate
from them on a case's cluster with its hack/install-ate.sh --deploy-ate-system,
and hack/install-microvm-deps.sh --install on a microvm one, configured from
the cluster's Terraform outputs."""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess

from . import RUNNER_DIR, images
from .cluster import Cluster
from .log import log
from .process import stream

SUBSTRATE_DIR = RUNNER_DIR.parent.parent / "dependency-repos" / "substrate"
LABEL_VALUE = re.compile(r"[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?")
# What workloads/manifests name by ko:// reference that a pre-built install
# doesn't take.
WORKLOAD_PACKAGES = ["./cmd/benchmarking/glutton"]
KO_REF = re.compile(r"ko://github\.com/agent-substrate/substrate/([^\s\"',\]}]+)")
ENVOY_DATAPLANE_DIR = (SUBSTRATE_DIR / "cmd" / "dataplane" / "envoy").relative_to(images.ROOT).as_posix()
ENVOY_DATAPLANE = images.Image(
    name="envoy-dataplane", dockerfile=f"{ENVOY_DATAPLANE_DIR}/Dockerfile", context=ENVOY_DATAPLANE_DIR
)


class SubstrateError(RuntimeError):
    pass


def revision() -> str:
    """The submodule's short commit, which every failure is explained against."""
    proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=SUBSTRATE_DIR, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else "unknown revision"


@functools.cache
def build_version() -> str:
    """The submodule's build version, which the cluster labels its nodes with,
    read once per run. It has to be the one hack/install-ate.sh stamps on the
    atelet DaemonSet, so it is worked out as ko.BuildVersion
    (cmd/ate-setup/internal/ko/ko.go) does: VERSION if set, else git describe
    in the submodule, else "dev"."""
    missing = missing_submodule()
    if missing:
        raise SubstrateError(f"the substrate build version requires {missing[0]}")
    version = os.environ.get("VERSION", "")
    if not version:
        cmd = ["git", "describe", "--tags", "--always", "--dirty"]
        proc = subprocess.run(cmd, cwd=SUBSTRATE_DIR, capture_output=True, text=True)
        version = proc.stdout.strip() if proc.returncode == 0 else ""
    version = version or "dev"
    # ate-setup rejects one that is not a label value rather than sanitizing it.
    if not LABEL_VALUE.fullmatch(version):
        raise SubstrateError(f"substrate build version {version!r} is not a Kubernetes label value; set VERSION")
    return version


def build_images(repo: str, project_id: str, cloud_build: bool = False, login: bool = True) -> None:
    """Builds and pushes to repo, tagged build_version(), what install and
    resolve take: every image of a pre-built install, which is the Makefile's
    build-images and envoy-dataplane, and the ones workloads/manifests name.
    ko builds here either way; envoy-dataplane, from a Dockerfile, with local
    Docker or on Cloud Build. Without login, ko uses the registry credentials
    Docker's configuration already has."""
    version = build_version()
    log(f"substrate: building the {version} images into {repo}")
    if login:
        # ko login, as ko pushes even with cloud_build, where there may be no docker.
        host = repo.split("/", 1)[0]
        ko = [str(SUBSTRATE_DIR / "hack" / "run-tool.sh"), "ko", "login", host]
        token = images.access_token(host)
        stream([*ko, "--username=oauth2accesstoken", "--password-stdin"], cwd=SUBSTRATE_DIR, input=token)
    make =["make", "build-images", f"KO_DOCKER_REPO={repo}", f"VERSION={version}", f"KO_TAGS=--tags={version}"]
    env = {"KO_DEFAULTPLATFORMS": images.DEFAULT_PLATFORM}
    stream(make, env=env, cwd=SUBSTRATE_DIR)
    stream([*make, f"IMAGES={' '.join(WORKLOAD_PACKAGES)}"], env=env, cwd=SUBSTRATE_DIR)
    if cloud_build:
        images.cloud_build_and_push([ENVOY_DATAPLANE], repo, version, images.DEFAULT_PLATFORM, project_id)
    else:
        images.docker_build_and_push([ENVOY_DATAPLANE], repo, version, images.DEFAULT_PLATFORM, login)


def image_repo(cluster: Cluster) -> str:
    """Where the cluster's substrate images are, which build_images pushed to."""
    if not cluster.substrate_docker_repo:
        raise SubstrateError(
            f"cluster {cluster.name} names no repository of substrate images; a dev infra applied before it did"
            " needs --dev-infra-update"
        )
    return cluster.substrate_docker_repo


def resolve(cluster: Cluster, manifest: str) -> str:
    """The manifest with each ko:// reference to substrate replaced by the
    image build_images pushed of it, pinned to its digest."""
    repo = image_repo(cluster)
    # ko names an image after the last element of its package, as the Makefile has it.
    return KO_REF.sub(lambda ref: images.pin(f"{repo}/{ref[1].rsplit('/', 1)[-1]}:{build_version()}"), manifest)


def missing_submodule() -> list[str]:
    return [] if (SUBSTRATE_DIR / "go.mod").is_file() else [f"the substrate submodule in {SUBSTRATE_DIR}"]


def missing_requirements(cloud_build: bool = False) -> list[str]:
    missing = [tool for tool in ("make", "go", "gcloud") if not shutil.which(tool)]
    return list(dict.fromkeys(missing + images.missing_requirements(cloud_build) + missing_submodule()))


def install(cluster: Cluster) -> None:
    """From the images build_images pushed, so the installer builds nothing."""
    env = {
        "PROJECT_ID": cluster.project_id,
        "CLUSTER_NAME": cluster.name,
        "CLUSTER_LOCATION": cluster.location,
        "BUCKET_NAME": cluster.bucket,
        "ATE_IMAGE_REPO": image_repo(cluster),
        "ATE_IMAGE_TAG": build_version(),
        # The nodes' platform, whatever the build host's.
        "KO_DEFAULTPLATFORMS": images.DEFAULT_PLATFORM,
        # Empty, so the installer runs get-credentials itself and uses the
        # context it writes into KUBECONFIG, rather than the developer's own.
        "KUBECTL_CONTEXT": "",
        "KUBECONFIG": str(cluster.kubeconfig),
        # A .ate-dev-env.sh in the submodule would name another cluster.
        "NO_DEV_ENV": "1",
    }
    log(f"substrate: installing on cluster {cluster.name}")
    # Egress credential injection is off: no workload needs the bundled
    # provider, and the installer requires a choice either way.
    install_ate = [str(SUBSTRATE_DIR / "hack" / "install-ate.sh"), "--deploy-ate-system", '--credential-provider={"enabled":false}']
    stream(install_ate, env=env, cwd=SUBSTRATE_DIR)
    # install-ate.sh lays down only the gVisor SandboxConfig. This one needs
    # substrate's CRDs, so it comes after.
    if cluster.sandbox_class == "microvm":
        log(f"substrate: installing the microvm SandboxConfig and its assets on cluster {cluster.name}")
        stream([str(SUBSTRATE_DIR / "hack" / "install-microvm-deps.sh"), "--install"], env=env, cwd=SUBSTRATE_DIR)
