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

"""Installs substrate on a case's cluster from the submodule's images, which
builder.py pushes ahead of the run, with its hack/install-ate.sh
--deploy-ate-system, and hack/install-microvm-deps.sh --install on a microvm
one, configured from the cluster's Terraform outputs. The gVisor SandboxConfig's assets are then
mirrored into the cluster's bucket."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

from . import RUNNER_DIR, images
from .cluster import Cluster
from .log import log
from .process import output, retry, stream

SUBSTRATE_DIR = RUNNER_DIR.parent.parent / "dependency-repos" / "substrate"
SUBSTRATE_PATH = SUBSTRATE_DIR.relative_to(images.ROOT).as_posix()
LABEL_VALUE = re.compile(r"[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?")
# What a pre-built install takes: images.Components in
# cmd/ate-setup/internal/images, less the demos.
SUBSTRATE_PACKAGES = [
    "./cmd/ateapi",
    "./cmd/atecontroller",
    "./cmd/atelet",
    "./cmd/atenet",
    "./cmd/credential-provider/kubernetes-secrets",
    "./cmd/podcertcontroller",
    "./cmd/snapshot-plugin",
    "./cmd/ateom-gvisor",
    "./cmd/ateom-microvm",
]
# What workloads/manifests name by ko:// reference that a pre-built install
# doesn't take.
WORKLOAD_PACKAGES = ["./cmd/benchmarking/glutton"]
KO_REF = re.compile(r"ko://github\.com/agent-substrate/substrate/([^\s\"',\]}]+)")
ENVOY_DATAPLANE_DIR = f"{SUBSTRATE_PATH}/cmd/dataplane/envoy"
ENVOY_DATAPLANE = images.Image(
    name="envoy-dataplane", dockerfile=f"{ENVOY_DATAPLANE_DIR}/Dockerfile", context=ENVOY_DATAPLANE_DIR
)
# The SandboxConfig install-ate.sh lays down.
GVISOR_CONFIG = "gvisor-default"
# Where in the cluster's bucket its assets are mirrored to, clear of the
# snapshot locations the object-store plugin refuses assets under.
ASSET_MIRROR_PREFIX = "sandbox-assets"
MICROVM_SANDBOX_CONFIG = "manifests/microvm/sandboxconfig-microvm.yaml.tmpl"
# The nodes' architecture, as the SandboxConfig's assets name it.
MICROVM_ARCH = images.DEFAULT_PLATFORM.rsplit("/", 1)[-1]
MICROVM_ASSETS = ("cloud-hypervisor", "virtiofsd", "vmlinux", "rootfs.img")
MICROVM_ASSETS_DIR = SUBSTRATE_DIR / "bin" / "microvm-assets" / MICROVM_ARCH
# The microvm scripts read these from the environment, so a developer's own
# would change what they download and where; the versions are the ones the
# submodule's scripts pin.
MICROVM_ASSET_ENV = {"ARCH": MICROVM_ARCH, "OUT": str(MICROVM_ASSETS_DIR), "KATA_VER": None, "CH_VER": None}


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


def image_refs(repo: str) -> list[str]:
    """What install and resolve take from repo, tagged build_version(): every
    image of a pre-built install, SUBSTRATE_PACKAGES and envoy-dataplane, and
    the ones workloads/manifests name. builder.py pushes them, each named
    after the last element of its package, as ate-setup's ko runner
    (cmd/ate-setup/internal/ko) names them."""
    names = [package.rsplit("/", 1)[-1] for package in [*SUBSTRATE_PACKAGES, *WORKLOAD_PACKAGES]]
    return [images.ref(repo, name, build_version()) for name in [*names, ENVOY_DATAPLANE.name]]


def image_repo(cluster: Cluster) -> str:
    """Where the cluster's substrate images are, image_refs's repo."""
    if not cluster.image_docker_repo:
        raise SubstrateError(
            f"cluster {cluster.name} names no repository of substrate images; a dev infra applied before it did"
            " needs --dev-infra-update"
        )
    return cluster.image_docker_repo


def resolve(cluster: Cluster, manifest: str) -> str:
    """The manifest with each ko:// reference to substrate replaced by the
    image of it image_refs names, pinned to its digest."""
    repo = image_repo(cluster)
    # As image_refs names them.
    return KO_REF.sub(lambda ref: images.pin(f"{repo}/{ref[1].rsplit('/', 1)[-1]}:{build_version()}"), manifest)


def missing_submodule() -> list[str]:
    return [] if (SUBSTRATE_DIR / "go.mod").is_file() else [f"the substrate submodule in {SUBSTRATE_DIR}"]


def pinned_commit() -> str:
    """The submodule commit the repo pins: in its index, which is HEAD's
    unless a new one is staged."""
    entry = output(["git", "ls-files", "--stage", "--", SUBSTRATE_PATH], cwd=images.ROOT).split()
    if len(entry) < 2 or entry[0] != "160000":
        raise SubstrateError(f"the repo pins no submodule at {SUBSTRATE_PATH}")
    return entry[1]


def checkout_commit() -> str:
    return output(["git", "rev-parse", "HEAD"], cwd=SUBSTRATE_DIR).strip()


def check_pinned() -> None:
    """Refuses a submodule checked out at another commit than the one the
    repo pins, as after a pull without git submodule update: the run would
    build, benchmark and label that other substrate. A new pin staged with
    git add counts, and uncommitted changes in the submodule are allowed: the
    build version marks them -dirty, and run.json the repo's state."""
    missing = missing_submodule()
    if missing:
        raise SubstrateError(f"{missing[0]} is not checked out; run git submodule update --init")
    pinned, checked_out = pinned_commit(), checkout_commit()
    if pinned != checked_out:
        raise SubstrateError(
            f"{SUBSTRATE_PATH} is checked out at {checked_out[:12]}, but the repo pins {pinned[:12]}; run"
            f" git submodule update, or git add {SUBSTRATE_PATH} to benchmark the commit it is at"
        )
    if output(["git", "status", "--porcelain"], cwd=SUBSTRATE_DIR).strip():
        log(f"substrate: {SUBSTRATE_PATH} has uncommitted changes; its build version is {build_version()}", "WARNING")


def missing_requirements() -> list[str]:
    """What installing it takes; builder.py says what building it does."""
    missing = [tool for tool in ("go", "gcloud") if not shutil.which(tool)]
    return list(dict.fromkeys(missing + images.missing_requirements() + missing_submodule()))


def install(cluster: Cluster) -> None:
    """From the images image_refs names, so the installer builds nothing.
    A failed install runs again from the start."""
    retry(f"substrate on cluster {cluster.name}", lambda: install_once(cluster))


def install_once(cluster: Cluster) -> None:
    cluster.get_credentials()
    env = {
        "PROJECT_ID": cluster.project_id,
        "CLUSTER_NAME": cluster.name,
        "CLUSTER_LOCATION": cluster.location,
        "BUCKET_NAME": cluster.bucket,
        "ATE_IMAGE_REPO": image_repo(cluster),
        "ATE_IMAGE_TAG": build_version(),
        # The nodes' platform, whatever the build host's.
        "KO_DEFAULTPLATFORMS": images.DEFAULT_PLATFORM,
        # The one get_credentials wrote, internal endpoint and all, rather than
        # the developer's own, so the installer runs no get-credentials of its own.
        "KUBECTL_CONTEXT": cluster.context,
        "KUBECONFIG": str(cluster.kubeconfig),
        # A .ate-dev-env.sh in the submodule would name another cluster.
        "NO_DEV_ENV": "1",
    }
    log(f"substrate: installing on cluster {cluster.name}")
    # Egress credential injection is off: no workload needs the bundled
    # provider, and the installer requires a choice either way.
    install_ate = [str(SUBSTRATE_DIR / "hack" / "install-ate.sh"), "--deploy-ate-system", '--credential-provider={"enabled":false}']
    stream(install_ate, env=env, cwd=SUBSTRATE_DIR)
    mirror_gvisor_assets(cluster)
    # install-ate.sh lays down only the gVisor SandboxConfig. This one needs
    # substrate's CRDs, so it comes after.
    if cluster.sandbox_class == "microvm":
        # Assembled and checked here, so install-microvm-deps.sh only uploads
        # them: it skips assembling a complete set it would assemble the same.
        microvm_env = {**env, **MICROVM_ASSET_ENV}
        verify_microvm_assets(assemble_microvm_assets(microvm_env))
        log(f"substrate: installing the microvm SandboxConfig and its assets on cluster {cluster.name}")
        stream([str(SUBSTRATE_DIR / "hack" / "install-microvm-deps.sh"), "--install"], env=microvm_env, cwd=SUBSTRATE_DIR)


def mirror_gvisor_assets(cluster: Cluster) -> None:
    """Copies GVISOR_CONFIG's gs:// assets in other buckets into the cluster's
    bucket, and points the config at the copies. atelet's Workload Identity
    token can't read a public bucket without uniform bucket-level access, such
    as gs://gvisor, and atelet doesn't fall back to reading it anonymously
    (agent-substrate/substrate#2613)."""
    config = json.loads(output(cluster.kubectl("get", "sandboxconfig", GVISOR_CONFIG, "-o=json")))
    patch = []
    for i, version in enumerate(config["spec"]["versions"]):
        for arch, assets in (version.get("assets") or {}).items():
            for name, asset in assets.items():
                url = asset["url"]
                bucket, _, path = url.removeprefix("gs://").partition("/")
                if not url.startswith("gs://") or bucket == cluster.bucket:
                    continue
                mirror = f"gs://{cluster.bucket}/{ASSET_MIRROR_PREFIX}/{bucket}/{path}"
                log(f"substrate: mirroring {url} to {mirror}")
                stream(["gcloud", "storage", "cp", "--no-clobber", url, mirror])
                patch.append({"op": "replace", "path": f"/spec/versions/{i}/assets/{arch}/{name}/url", "value": mirror})
    if patch:
        stream(cluster.kubectl("patch", "sandboxconfig", GVISOR_CONFIG, "--type=json", f"--patch={json.dumps(patch)}"))


def assemble_microvm_assets(env: dict[str, str | None]) -> Path:
    """Downloads the microvm assets into MICROVM_ASSETS_DIR unless a complete
    set of the versions the submodule pins is there, and returns the dir."""
    assemble = str(SUBSTRATE_DIR / "hack" / "microvm-assets" / "assemble.sh")
    stamp = output([assemble, "--print-stamp"], env=env, cwd=SUBSTRATE_DIR).strip()
    stamp_file = MICROVM_ASSETS_DIR / ".asset-versions"
    complete = all((MICROVM_ASSETS_DIR / name).is_file() for name in MICROVM_ASSETS)
    if not complete or not stamp_file.is_file() or stamp_file.read_text().strip() != stamp:
        log(f"substrate: assembling the microvm assets into {MICROVM_ASSETS_DIR}")
        stream([assemble], env=env, cwd=SUBSTRATE_DIR)
    return MICROVM_ASSETS_DIR


def pinned_asset_digests() -> dict[str, str]:
    """The sha256 the microvm SandboxConfig pins, in its default version, for
    each asset of the nodes' architecture, by its file name."""
    spec = yaml.safe_load((SUBSTRATE_DIR / MICROVM_SANDBOX_CONFIG).read_text())["spec"]
    version = next(v for v in spec["versions"] if v["name"] == spec["defaultVersion"])
    assets = version["assets"][MICROVM_ARCH]
    return {asset["url"].rsplit("/", 1)[-1]: asset["sha256"] for asset in assets.values()}


def verify_microvm_assets(assets_dir: Path) -> None:
    """Refuses assets other than the ones the SandboxConfig pins, before they
    are uploaded. atelet checks them again on the node, but only once an actor
    is resumed, where a mismatch shows as an actor stuck resuming."""
    pinned = pinned_asset_digests()
    if set(pinned) != set(MICROVM_ASSETS):
        raise SubstrateError(f"{MICROVM_SANDBOX_CONFIG} pins {', '.join(sorted(pinned))}, not {', '.join(MICROVM_ASSETS)}")
    wrong = []
    for name, want in pinned.items():
        digest = hashlib.sha256()
        with (assets_dir / name).open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != want:
            wrong.append(f"{name} is {digest.hexdigest()}, not {want}")
    if wrong:
        # Assembled again from scratch next time, rather than trusted.
        (assets_dir / ".asset-versions").unlink(missing_ok=True)
        raise SubstrateError(f"microvm assets in {assets_dir} do not match {MICROVM_SANDBOX_CONFIG}: {'; '.join(wrong)}")
    log(f"substrate: microvm assets match the sha256s {MICROVM_SANDBOX_CONFIG} pins")
