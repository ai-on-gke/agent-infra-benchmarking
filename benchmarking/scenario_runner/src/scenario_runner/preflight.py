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

"""Checks, before anything is applied, what the cases need of this machine,
the project and their clusters' configs, so a mistake fails in seconds rather
than once a cluster is up. Every problem found is reported, not only the
first."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import benchmarks
from .control_planes import agent_sandbox, substrate
from .infra.cluster_configs import ClusterConfig, execution
from .infra.deployment import Deployment, declared_variables, tf_vars
from .infra.terraform import data_env
from .log import log
from .process import environ
from .scenarios.cases import BARE_METAL
from .scenarios.install import AGENT_SANDBOX, SUBSTRATE, control_plane, sandbox_class

# `kubectl wait --for=create`, which the runner waits for the worker pool with.
MIN_KUBECTL = (1, 31)
# The settings of a case's cluster the checks read, if its config declares them.
CLUSTER_SETTINGS = (
    "nested_virtualization",
    "sandboxed_machine_type",
    "sandboxed_node_count",
    "sandboxed_min_node_count",
    "sandboxed_max_node_count",
    "additional_node_pools",
    "kubernetes_version",
    "zone",
    "regional_control_plane",
)
GKE_CHANNEL = "REGULAR"


def nested_virtualization(machine_type: str) -> bool:
    """Whether GCE offers nested virtualization, which microvm needs for
    /dev/kvm, on the machine type: Intel families only, so not E2, AMD
    (n2d, c2d, c3d, ...) or Arm (t2a, c4a, ...)."""
    family = machine_type.split("-", 1)[0]
    return family != "e2" and not family.endswith(("d", "a"))


def kubectl_version() -> tuple[int, int] | None:
    if not shutil.which("kubectl"):
        return None
    proc = subprocess.run(["kubectl", "version", "--client", "-o", "json"], capture_output=True, text=True)
    try:
        client = json.loads(proc.stdout)["clientVersion"]
        return int(client["major"]), int(re.match(r"\d+", client["minor"])[0])
    except (ValueError, KeyError, TypeError):
        return None


def tar_has_zstd() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        # An empty directory, beside rather than holding the archive, which GNU
        # tar would refuse to put in itself.
        (Path(tmp) / "empty").mkdir()
        archive = Path(tmp) / "empty.tar.zst"
        proc = subprocess.run(["tar", "--zstd", "-cf", str(archive), "-C", f"{tmp}/empty", "."], capture_output=True)
        return proc.returncode == 0


def credential_problems() -> list[str]:
    """Application Default Credentials, which Terraform and the Python
    clients use, and gcloud's own, which the image builds and pins use."""
    import google.auth
    import google.auth.exceptions
    from google.auth.transport.requests import Request

    problems = []
    try:
        credentials, _ = google.auth.default()
        credentials.refresh(Request())
    except google.auth.exceptions.GoogleAuthError as exc:
        problems.append(f"no usable Application Default Credentials ({exc}); run gcloud auth application-default login")
    if not shutil.which("gcloud"):
        problems.append("gcloud is missing")
    elif subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True).returncode:
        problems.append("gcloud has no usable account; run gcloud auth login")
    return problems


def creates_gke_cluster(config: Path) -> bool:
    """Whether the config, in the .tf files terraform reads, creates a GKE cluster."""
    files = [tf for tf in config.rglob("*.tf") if not tf.name.startswith(".")]
    return any("google_container_cluster" in tf.read_text() for tf in files)


def cluster_settings(config: ClusterConfig, project_id: str, region: str) -> dict:
    """The settings of CLUSTER_SETTINGS the case's cluster would be applied
    with, evaluated by terraform from its config and variables, which also
    runs the config's own variable validations. Raises ValueError with
    terraform's message if they fail."""
    declared = declared_variables(config.CONFIG) & set(CLUSTER_SETTINGS)
    with tempfile.TemporaryDirectory() as tmp:
        deployment = config.deployment(Deployment(Path(tmp)), execution(tf_vars(project_id, region, "preflight")))
        env = data_env(deployment.data)
        init = ["terraform", f"-chdir={deployment.config}", "init", "-input=false", "-no-color"]
        proc = subprocess.run(init, capture_output=True, text=True, env=environ(env))
        if proc.returncode:
            raise ValueError(f"terraform init failed: {proc.stderr.strip() or proc.stdout.strip()}")
        expression = "jsonencode({" + ", ".join(f"{name} = var.{name}" for name in sorted(declared)) + "})"
        console = ["terraform", f"-chdir={deployment.config}", "console", "-no-color"]
        proc = subprocess.run(console, input=expression, capture_output=True, text=True, env=environ(env))
        # A failed variable validation is printed to stderr, and console still
        # prints the value and exits 0.
        if proc.returncode or "Error:" in proc.stderr:
            errors = proc.stderr.split("Warning:", 1)[0].strip()
            raise ValueError(errors or proc.stdout.strip())
        # console prints the JSON as an HCL string, after any warnings.
        value = proc.stdout.strip().splitlines()[-1]
        if value == "(known after apply)":
            raise ValueError(f"a variable of {', '.join(sorted(declared))} has no value")
        return json.loads(json.loads(value))


def cluster_location(settings: dict, region: str) -> str:
    """Where the case's cluster goes: its zone, or the region for a regional
    control plane or a config with no zone."""
    return settings["zone"] if settings.get("zone") and not settings.get("regional_control_plane") else region


def gke_versions(location: str, project_id: str) -> list[str] | None:
    """The versions GKE_CHANNEL offers in the location, which a zone and its
    region can differ in, or None if they cannot be read."""
    cmd = ["gcloud", "container", "get-server-config", f"--location={location}", f"--project={project_id}",
           "--format=json(channels)"]  # fmt: skip
    if not shutil.which("gcloud"):
        return None
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        return None
    channels = json.loads(proc.stdout).get("channels") or []
    return next((c.get("validVersions", []) for c in channels if c.get("channel") == GKE_CHANNEL), [])


def cluster_problems(
    name: str, config: ClusterConfig, settings: dict, versions: list[str] | None, gke: bool, location: str
) -> list[str]:
    problems = []
    machine_type = settings.get("sandboxed_machine_type")
    if settings.get("nested_virtualization") and machine_type and not nested_virtualization(machine_type):
        problems.append(
            f"case {name}'s cluster has nested virtualization, but its sandboxed pool is {machine_type}, which GCE"
            " offers it on only in an Intel family such as n2 or c2 (not E2, AMD or Arm)"
        )
    version = settings.get("kubernetes_version")
    if gke and version and versions is not None and not offered(version, versions):
        minor = ".".join(version.split(".")[:2])
        same_minor = ", ".join(v for v in versions if offered(minor, [v])) or "none"
        problems.append(
            f"case {name}'s cluster pins GKE {version}, which the {GKE_CHANNEL} channel does not offer in"
            f" {location}, where the cluster goes; of {minor} it offers {same_minor} there, of all"
            f" {', '.join(sorted({'.'.join(v.split('.')[:2]) for v in versions}))}. Set kubernetes_version in"
            f" infra/clusters/{config.name}/variables.tf to one it offers"
        )
    return problems


def machine_type_cpus(machine_type: str, zone: str, project_id: str) -> int | str | None:
    """The machine type's vCPUs in the zone, a message if the zone does not
    offer it, or None if that cannot be read."""
    cmd = ["gcloud", "compute", "machine-types", "describe", machine_type, f"--zone={zone}",
           f"--project={project_id}", "--format=value(guestCpus)"]  # fmt: skip
    if not shutil.which("gcloud"):
        return None
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        return f"{zone} does not offer {machine_type}" if "was not found" in proc.stderr else None
    return int(proc.stdout.strip())


def cpu_quota(family: str, region: str, project_id: str) -> tuple[float, float] | None:
    """The region's usage and limit of the machine family's CPU quota, or None
    if it cannot be read or the region has none of its own for it."""
    cmd = ["gcloud", "compute", "regions", "describe", region, f"--project={project_id}", "--format=json(quotas)"]
    if not shutil.which("gcloud"):
        return None
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        return None
    metric = f"{family.upper()}_CPUS"
    quota = next((q for q in json.loads(proc.stdout).get("quotas", []) if q.get("metric") == metric), None)
    return (quota["usage"], quota["limit"]) if quota else None


def bare_metal_problems(
    name: str, settings: dict, placements: list[dict], project_id: str, region: str
) -> list[str]:
    """What stands in the way of the case's sandboxed pool at each of the
    placements: a zone that does not offer its machine type, or a CPU quota
    the pool does not fit in at all. A pool that fits in the quota but not in
    what is free of it now is only warned of, as other runs may free it."""
    problems = []
    nodes = settings.get("sandboxed_max_node_count") or settings.get("sandboxed_node_count") or 1
    for placement in placements:
        machine_type, zone = placement["machine-type"], placement["zone"]
        cpus = machine_type_cpus(machine_type, zone, project_id)
        if isinstance(cpus, str):
            problems.append(f"case {name}'s bare-metal placement cannot be used: {cpus}")
            continue
        family = machine_type.split("-", 1)[0]
        quota = cpu_quota(family, region, project_id) if cpus else None
        if quota is None:
            log(f"preflight: cannot read the {family} CPU quota for {machine_type} in {region}; not checking it",
                "WARNING")  # fmt: skip
            continue
        usage, limit = quota
        if cpus * nodes > limit:
            problems.append(
                f"case {name}'s sandboxed pool of {nodes} x {machine_type} needs {cpus * nodes} {family.upper()} vCPUs,"
                f" but the quota in {region} is {limit:g}; ask for more or run fewer nodes"
            )
        elif cpus * nodes > limit - usage:
            log(f"preflight: case {name}'s {nodes} x {machine_type} needs {cpus * nodes} {family.upper()} vCPUs, of"
                f" which {region} has {limit - usage:g} free now", "WARNING")  # fmt: skip
    return problems


def offered(version: str, versions: list[str]) -> bool:
    """Whether a build in versions is version, or of it, as GKE takes a minor
    such as 1.35 or a patch such as 1.35.8 for its latest build."""
    return any(v == version or v.startswith((f"{version}.", f"{version}-")) for v in versions)


def check(
    cases: dict[str, dict], project_id: str, region: str, applied: list[str], placements: list[dict] = ()
) -> list[str]:
    """Every problem the cases would run into, as messages; none means go.
    Only the clusters of the cases in applied are checked, as the others are
    not applied: a complete dev infra's, for one. A case on bare metal is
    checked at each of placements, any of which its cluster may end up at."""
    problems = []
    if not shutil.which("terraform"):
        return ["terraform is required to apply the infra"]
    installs = any(control_plane(case) == SUBSTRATE for case in cases.values())
    measured = {name: case for name, case in cases.items() if case["type"] != "noop"}
    if installs:
        missing = substrate.missing_requirements()
        if missing:
            return [f"installing substrate requires {', '.join(missing)}"]
        try:
            substrate.check_pinned()
            substrate.build_version()
        except (substrate.SubstrateError, subprocess.CalledProcessError) as exc:
            problems.append(str(exc))
    if any(control_plane(case) == AGENT_SANDBOX for case in cases.values()):
        missing = agent_sandbox.missing_submodule()
        if missing:
            return problems + [f"installing agent-sandbox requires {', '.join(missing)}"]
        try:
            agent_sandbox.build_version()
        except agent_sandbox.AgentSandboxError as exc:
            problems.append(str(exc))
    missing = benchmarks.missing_requirements(measured)
    if missing:
        return problems + [f"running the benchmark requires {', '.join(missing)}"]

    problems += credential_problems()
    if installs or measured:
        if not shutil.which("gke-gcloud-auth-plugin"):
            problems.append("gke-gcloud-auth-plugin is missing: gcloud components install gke-gcloud-auth-plugin")
        version = kubectl_version()
        if not shutil.which("kubectl"):
            problems.append("kubectl is missing")
        elif version is None or version < MIN_KUBECTL:
            have = ".".join(map(str, version)) if version else "unknown"
            problems.append(f"kubectl {have} is too old; the runner needs {'.'.join(map(str, MIN_KUBECTL))} or later")
    if any(sandbox_class(case) == "microvm" for case in cases.values()):
        if not shutil.which("curl"):
            problems.append("microvm cases need curl to download the kata assets")
        if not tar_has_zstd():
            problems.append("microvm cases need a tar that does --zstd, and zstd, to unpack the kata assets")

    offered: dict[str, list[str] | None] = {}
    evaluated: dict[ClusterConfig, dict | ValueError] = {}
    for name in applied:
        case = cases[name]
        config = benchmarks.cluster_config(name, case)
        bare_metal = case.get(BARE_METAL) and placements
        # Evaluated at the first placement; the others only move its pool.
        if bare_metal:
            config = config.placed(placements[0])
        if sandbox_class(case) == "microvm" and not (config.sandboxed_kvm or case.get(BARE_METAL)):
            problems.append(
                f"case {name} is microvm, whose guests boot on /dev/kvm, but its sandboxed pool has none; set its"
                " `infra.cluster.nested-virtualization: true` or run it on bare metal"
            )
        # Cases on the same cluster are evaluated once.
        if config not in evaluated:
            try:
                evaluated[config] = cluster_settings(config, project_id, region)
            except ValueError as exc:
                evaluated[config] = exc
        settings = evaluated[config]
        if isinstance(settings, ValueError):
            problems.append(f"case {name}'s cluster config does not evaluate: {settings}")
            continue
        gke = creates_gke_cluster(config.CONFIG)
        variants = [settings]
        if bare_metal:
            problems += bare_metal_problems(name, settings, placements, project_id, region)
            variants += [
                {**settings, "zone": p["zone"], "sandboxed_machine_type": p["machine-type"]} for p in placements[1:]
            ]
        for variant in variants:
            location = cluster_location(variant, region)
            if gke and location not in offered:
                offered[location] = gke_versions(location, project_id)
                if offered[location] is None:
                    log(f"preflight: cannot read the GKE versions {GKE_CHANNEL} offers in {location}; not checking"
                        " them", "WARNING")  # fmt: skip
            found = cluster_problems(name, config, variant, offered.get(location), gke, location)
            problems += [problem for problem in found if problem not in problems]
    return problems
