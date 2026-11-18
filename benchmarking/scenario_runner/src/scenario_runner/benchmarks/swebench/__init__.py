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

"""Runs a case of type swebench: sandboxenv-rl's swebench-loader, which plays
an RL trainer against ate-env-api with real task images and no model, as the
loader Job of swebench/manifests/loader-job.yaml. Where the locust cases time
the platform's own calls, this times what a trainer sees through
sandboxenv-rl's API: cold and warm restores, park and wake across training
steps, and the throughput of a fleet of environments.

The case's `swebench` names the dataset, the loader's `mode`, and how the run
is shaped: `width` tasks at once, and in eval mode `tasks-per-image`, in steps
mode `steps`, `rollouts` and `park`. The `dataset` is `images`, names of the
images of swebench/images.json the loader's built-in workloads run in, which the
case then needs no SWE-bench images mirrored for, or `file`, the path in this
repository of a manifest that make-dataset.py of sandboxenv-rl made. ate-env-api
and its pool come from sandboxenv-rl's submodule, deployed by sandboxenv_rl,
whose pool `env-worker-count`, `api-replicas`, `actor-memory` and `actor-cpu`
size. The loader's events, stats and summary are uploaded by its pod to the
case's report folder, with its log."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Callable

from ... import LABEL_RE, RUNNER_DIR, git, images, reports, workloads
from ...infra.cluster import Cluster
from ...infra.cluster_configs import CommonCluster, from_infra
from ...log import ON_CLOUD_RUN, log
from ...process import output, stream
from ...reports import run_info
from ...scenarios.install import INSTALL, SANDBOX_CLASS, SUBSTRATE, control_plane, sandbox_class
from .. import jobs
from . import sandboxenv_rl

MANIFEST = RUNNER_DIR.parent / "swebench" / "manifests" / "loader-job.yaml"
# Have to match metadata.name and namespace in the manifest.
JOB = "swebench-loader"
NAMESPACE = jobs.NAMESPACE
LOADER_IMAGE = "swebench-loader"
# The Secret and ConfigMap the Job reads, which have to be in its namespace.
TOKEN_SECRET = "swebench-loader-token"
DATASET_CONFIGMAP = "swebench-loader-dataset"
# Where the manifest mounts the ConfigMap's one key.
DATASET_KEY = "dataset.jsonl"
DATASET_PATH = f"/data/{DATASET_KEY}"
# A ConfigMap holds a megabyte at most, and the API server's own bookkeeping takes some of it.
MAX_DATASET_BYTES = 900_000
# The loader's image is pulled, and its pod may wait for a node.
POD_START_TIMEOUT_SECS = 600
DEFAULT_TIMEOUT_SECS = 3600
# The manifest's terminationGracePeriodSeconds and a margin.
STOP_TIMEOUT_SECS = 150
# The case's mapping of its type, and what it may state.
TYPE = "swebench"
KEYS = {
    "infra", INSTALL, "env-worker-count", "api-replicas", "actor-cpu", "actor-memory", "dataset", "mode", "width",
    "tasks-per-image", "steps", "rollouts", "park", "max-images", "trajectory", "read-file", "validate",
    "loader-args", "timeout",
}  # fmt: skip
# The images.json files of the images a run pulls.
IMAGE_FILES = [images.SWEBENCH_IMAGES]
MODES = ("eval", "steps")
# swebench-loader run's -park.
PARKS = ("suspend", "suspend-sync", "suspend-async", "fork", "delete", "none")
DEFAULT_MODE = "eval"
DEFAULT_WIDTH = 10
DEFAULT_ENV_WORKER_COUNT = 10
# What the loader's built-in image-delivery workload reads, which the smoke
# rootfs has and is tiny.
WORKLOAD = "workload-file-read-one"
DEFAULT_READ_FILE = "/etc/os-release"
# What the case gives the loader through keys of its own, and what the run
# sets, which `loader-args` may not name.
MANAGED_FLAGS = {
    "dataset", "images", "endpoint", "atespace", "base-template", "mode", "width", "tasks-per-image", "steps", "rollouts",
    "park", "trajectory", "set", "validate", "run-id", "test-name", "tag", "out", "stats-out", "run-window", "events-dir",
    "job", "worker", "keep", "dry-run", "access", "backend", "control-addr", "router-addr", "auth-token-file",
    "convert-trace", "trace-out", "schedule", "label",
}  # fmt: skip
FLAG_RE = re.compile(r"^--?([A-Za-z][A-Za-z0-9-]*)")


class SwebenchError(RuntimeError):
    pass


class SwebenchTimeout(SwebenchError):
    """The run outlived its `timeout` and is still going. On a dev infra the
    next run would recreate the pool under it, so a series stops here."""


def missing_requirements() -> list[str]:
    return list(dict.fromkeys(images.missing_requirements() + sandboxenv_rl.missing_requirements()))


def ingress_spiffe_ids(case: dict) -> list[str]:
    """The identities besides the runner's that the case's substrate has to let
    through its router: the one ate-env-api dials it as."""
    return [sandboxenv_rl.API_SPIFFE_ID]


def whole(value: object, least: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= least


def task_images() -> set[str]:
    """The images of swebench/images.json a `dataset.images` may name: all
    but the loader's."""
    return set(json.loads(images.SWEBENCH_IMAGES.read_text())) - {LOADER_IMAGE}


def check_dataset(where: str, dataset: object) -> dict:
    """The case's `dataset` with a `file` as the path under the repository
    root, or the `images` as a list."""
    if not isinstance(dataset, dict) or len(dataset) != 1 or next(iter(dataset)) not in ("file", "images"):
        raise ValueError(f"{where} must be a mapping of one of `file` and `images`, got {dataset!r}")
    if "file" in dataset:
        file = dataset["file"]
        if not isinstance(file, str) or not file:
            raise ValueError(f"{where}.file must be the path of a JSONL manifest in this repository")
        path = (images.ROOT / file).resolve()
        if images.ROOT.resolve() not in path.parents:
            raise ValueError(f"{where}.file must be inside this repository, got {file!r}")
        if not path.is_file():
            raise ValueError(f"{where}.file {file} is not a file; make one with sandboxenv-rl's make-dataset.py")
        size = path.stat().st_size
        if size > MAX_DATASET_BYTES:
            raise ValueError(
                f"{where}.file {file} is {size} bytes, over the {MAX_DATASET_BYTES} a ConfigMap holds; use fewer instances"
            )
        return {"file": file}
    names = dataset["images"]
    known = task_images()
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names) or len(set(names)) != len(names):
        raise ValueError(f"{where}.images must be a list of distinct image names, such as {sorted(known)}")
    unknown = set(names) - known
    if unknown:
        raise ValueError(f"{where}.images has unknown {', '.join(sorted(unknown))}; there are {', '.join(sorted(known))}")
    return {"images": names}


def check_loader_args(where: str, args: object) -> list[str]:
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError(f"{where} must be a list of flags for `swebench-loader run`, as strings")
    for arg in args:
        flag = FLAG_RE.match(arg)
        if flag and flag[1] in MANAGED_FLAGS:
            raise ValueError(f"{where} names -{flag[1]}, which the case's own keys or the run set")
    return args


def check_case(name: str, case: dict, deploy_workloads: bool) -> dict:
    """The case with its defaults filled in."""
    if not LABEL_RE.fullmatch(name):
        raise ValueError(f"case name {name!r} is not a valid label value")
    options = case.get(TYPE)
    if not isinstance(options, dict):
        raise ValueError(f"case {name} has no `{TYPE}` mapping")
    where = f"case {name}'s `{TYPE}"
    unknown = options.keys() - KEYS
    if unknown:
        raise ValueError(f"{where}` has unknown {', '.join(sorted(unknown))}")
    # ate-env-api runs on substrate, which the cluster has only with it.
    if control_plane(case) != SUBSTRATE:
        raise ValueError(f"{where}` needs `{TYPE}.{INSTALL}.{SUBSTRATE}.{SANDBOX_CLASS}`: ate-env-api runs on substrate")
    dataset = check_dataset(f"{where}.dataset`", options.get("dataset"))
    mode = options.get("mode", DEFAULT_MODE)
    if mode not in MODES:
        raise ValueError(f"{where}.mode` must be one of {', '.join(MODES)}, got {mode!r}")
    for key, least in (("env-worker-count", 1), ("api-replicas", 1), ("width", 1), ("max-images", 0), ("timeout", 1)):
        if key in options and not whole(options[key], least):
            raise ValueError(f"{where}.{key}` must be a whole number from {least}, got {options[key]!r}")
    # What is for one mode only.
    only = {"eval": ("tasks-per-image",), "steps": ("steps", "rollouts", "park")}
    for other, keys in only.items():
        for key in keys:
            if mode != other and key in options:
                raise ValueError(f"{where}.{key}` is for `mode: {other}`")
    for key in ("tasks-per-image", "steps", "rollouts"):
        if key in options and not whole(options[key], 1):
            raise ValueError(f"{where}.{key}` must be a whole number from 1, got {options[key]!r}")
    if options.get("park", "suspend") not in PARKS:
        raise ValueError(f"{where}.park` must be one of {', '.join(PARKS)}")
    memory = options.get("actor-memory", sandboxenv_rl.DEFAULT_ACTOR_MEMORY)
    if not isinstance(memory, str) or workloads.memory_mib(memory) is None:
        raise ValueError(f"{where}.actor-memory` must be a Kubernetes quantity such as 4Gi, got {memory!r}")
    cpu = options.get("actor-cpu", sandboxenv_rl.DEFAULT_ACTOR_CPU)
    if not isinstance(cpu, str) or not workloads.CPU_RE.fullmatch(cpu):
        raise ValueError(f"{where}.actor-cpu` must be a quoted cpu quantity such as \"2\" or \"1500m\", got {cpu!r}")
    if not isinstance(options.get("validate", False), bool):
        raise ValueError(f"{where}.validate` must be true or false")
    # An image-only dataset has nothing to grade, so it runs the loader's workload.
    trajectory = options.get("trajectory", WORKLOAD if "images" in dataset else None)
    if trajectory is not None and (not isinstance(trajectory, str) or not trajectory):
        raise ValueError(f"{where}.trajectory` must be a built-in trajectory's name or a comma-separated list")
    if "images" in dataset and trajectory != WORKLOAD:
        raise ValueError(f"{where}.dataset.images` has no grading material, so `trajectory` can only be {WORKLOAD}")
    read_file = options.get("read-file", DEFAULT_READ_FILE if trajectory == WORKLOAD else None)
    if read_file is not None and (trajectory != WORKLOAD or not isinstance(read_file, str) or not read_file.startswith("/")):
        raise ValueError(f"{where}.read-file` must be an absolute path, and is for `trajectory: {WORKLOAD}`")
    loader_args = check_loader_args(f"{where}.loader-args`", options.get("loader-args", []))
    cluster_config(name, case)
    return {
        **case,
        TYPE: {
            "mode": mode,
            "width": DEFAULT_WIDTH,
            "env-worker-count": DEFAULT_ENV_WORKER_COUNT,
            "api-replicas": sandboxenv_rl.DEFAULT_API_REPLICAS,
            "actor-memory": memory,
            "actor-cpu": cpu,
            "max-images": 0,
            "validate": False,
            "timeout": DEFAULT_TIMEOUT_SECS,
            **({"tasks-per-image": 1} if mode == "eval" else {"steps": 4, "rollouts": 2, "park": "suspend"}),
            **options,
            "dataset": dataset,
            "trajectory": trajectory,
            "read-file": read_file,
            "loader-args": loader_args,
        },
    }


def cluster_config(name: str, case: dict) -> CommonCluster:
    """The cluster the case runs on: its `swebench.infra.cluster`."""
    return from_infra(name, f"{TYPE}.infra", case[TYPE].get("infra"))


def commit(path: Path) -> str | None:
    try:
        return git.head(path)
    except subprocess.CalledProcessError:
        return None


def instance_row(name: str, image: str) -> dict:
    """An image-only manifest's record, as sandboxenv-rl's make-dataset.py
    --images-file writes it: no grading material."""
    return {
        "instance_id": name, "repo": "", "image": image, "patch": "", "test_patch": "", "eval_script": "",
        "fail_to_pass": [], "pass_to_pass": [], "log_parser": "", "eval_type": "",
    }  # fmt: skip


def dataset_manifest(case: dict, image_repo: str | None) -> tuple[str, dict[str, str]]:
    """The manifest the loader reads, as JSONL, and the images by name that a
    `dataset.images` is of, pinned to the digests the repository serves, as
    ate-env-api takes only an image pinned by digest."""
    dataset = case[TYPE]["dataset"]
    if "file" in dataset:
        return (images.ROOT / dataset["file"]).read_text(), {}
    refs = {name: images.pin(images.image_ref(images.SWEBENCH_IMAGES, image_repo, name)) for name in dataset["images"]}
    return "".join(json.dumps(instance_row(name, ref)) + "\n" for name, ref in refs.items()), refs


def loader_run_id(run_id: str, name: str) -> str:
    """The loader's own run id, which prefixes its environments' names: a DNS
    label of at most 20 characters, as it takes, and the run's, whose are
    longer."""
    return f"swe-{hashlib.sha256(f'{run_id}/{name}'.encode()).hexdigest()[:8]}"


def loader_args(name: str, case: dict, run_id: str) -> list[str]:
    """The arguments of `swebench-loader run`, but for the files it writes,
    which run_loader.py adds."""
    options = case[TYPE]
    args = [
        f"-dataset={DATASET_PATH}",
        f"-endpoint={sandboxenv_rl.API_ADDR}",
        f"-atespace={sandboxenv_rl.ATESPACE}",
        f"-base-template={sandboxenv_rl.TEMPLATE}",
        f"-mode={options['mode']}",
        f"-width={options['width']}",
        f"-images={options['max-images']}",
        f"-run-id={loader_run_id(run_id, name)}",
        f"-test-name={name}",
        f"-tag={run_id}",
    ]
    if options["mode"] == "eval":
        args.append(f"-tasks-per-image={options['tasks-per-image']}")
    else:
        args += [f"-steps={options['steps']}", f"-rollouts={options['rollouts']}", f"-park={options['park']}"]
    if options["trajectory"]:
        args.append(f"-trajectory={options['trajectory']}")
    if options["read-file"]:
        args.append(f"-set=READ_FILE={options['read-file']}")
    if options["validate"]:
        args.append("-validate")
    return [*args, *options["loader-args"]]


def render_job(values: dict[str, str]) -> str:
    return Template(MANIFEST.read_text()).substitute(values)


def apply_json(cluster: Cluster, obj: dict) -> None:
    """Server-side, so no last-applied-configuration annotation copies what
    the object holds, a token or the dataset."""
    stream(cluster.kubectl("apply", "--server-side", "-f", "-"), input=json.dumps(obj))


def wait_started(cluster: Cluster) -> None:
    """Waits for the Job's pod to be running, or done, as kubectl logs --follow
    of one still starting returns at once."""
    log(f"swebench: waiting for the {JOB} pod to start")
    deadline = time.monotonic() + POD_START_TIMEOUT_SECS
    phase = cluster.kubectl("get", "pods", f"--selector=job-name={JOB}", "-o=jsonpath={.items[0].status.phase}",
                            namespace=NAMESPACE)  # fmt: skip
    while time.monotonic() < deadline:
        if subprocess.run(phase, capture_output=True, text=True).stdout in ("Running", "Succeeded", "Failed"):
            return
        time.sleep(2)
    try:
        stream(cluster.kubectl("describe", "job", JOB, namespace=NAMESPACE))
    except subprocess.CalledProcessError:
        pass
    raise SwebenchError(f"the {JOB} pod did not start within {POD_START_TIMEOUT_SECS}s")


def wait_for_job(cluster: Cluster, stop_logs: Callable[[int], None], timeout: int) -> None:
    log("swebench: waiting for the run to finish")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ended = jobs.job_status(cluster, JOB, NAMESPACE)
        except jobs.StatusUnread:
            ended = None
        if ended:
            stop_logs(10)
            if ended == "Failed":
                # The log says why: the loader's exit status is the Job's.
                raise SwebenchError(f"{JOB} failed: {jobs.logs_hint(cluster, JOB, NAMESPACE)}")
            log(f"swebench: {JOB} completed: {jobs.logs_hint(cluster, JOB, NAMESPACE)}")
            return
        time.sleep(5)
    raise SwebenchTimeout(f"timed out after {timeout}s; the Job is left running")


def run_loader(
    cluster: Cluster,
    name: str,
    case: dict,
    run_id: str,
    folder: str,
    loader_image: str,
    image_repo: str,
    deploy_workloads: bool,
    bucket: str,
    report_prefix: str,
) -> None:
    """Runs the case's loader as one Job, whose reports go under
    <run_id>/<folder>/."""
    options = case[TYPE]
    if deploy_workloads:
        sandboxenv_rl.deploy(
            cluster, image_repo, sandbox_class(case), options["env-worker-count"], options["actor-memory"],
            options["actor-cpu"], options["api-replicas"],
        )  # fmt: skip
    template = workloads.deployed_template(cluster, sandboxenv_rl.TEMPLATE, sandboxenv_rl.ATESPACE)
    # Matters when the pool was not deployed by this run (--no-deploy-workloads).
    pool = cluster.kubectl(
        "get", "workerpool", sandboxenv_rl.POOL, "-o=jsonpath={.spec.sandboxClasses[*].name}", namespace=sandboxenv_rl.NAMESPACE
    )
    have = output(pool).strip()
    if have != sandbox_class(case):
        raise SwebenchError(
            f"worker pool {sandboxenv_rl.NAMESPACE}/{sandboxenv_rl.POOL} is {have or 'of no sandbox class'},"
            f" not {sandbox_class(case)}; deploy it again"
        )
    token = sandboxenv_rl.auth_token(cluster)
    if not token:
        raise SwebenchError(
            f"no token in {sandboxenv_rl.NAMESPACE}/{sandboxenv_rl.AUTH_SECRET} for the loader to call"
            " ate-env-api with; deploy it again"
        )

    dataset, dataset_images = dataset_manifest(case, image_repo)
    if len(dataset.encode()) > MAX_DATASET_BYTES:
        raise SwebenchError(f"the dataset is over the {MAX_DATASET_BYTES} bytes a ConfigMap holds")
    args = loader_args(name, case, run_id)
    info = run_info.collect(
        cluster, case, cluster_config(name, case), sandboxenv_rl.TEMPLATE, template,
        {LOADER_IMAGE: loader_image, **dataset_images}, bucket,
        pool=(sandboxenv_rl.NAMESPACE, sandboxenv_rl.POOL),
    )  # fmt: skip
    info["sandboxenv_rl_commit"] = commit(sandboxenv_rl.DIR)
    info["sandboxenv_rl_version"] = sandboxenv_rl.build_version()
    info["loader_args"] = args
    info["dataset"] = {
        "source": options["dataset"],
        "sha256": hashlib.sha256(dataset.encode()).hexdigest(),
        "instances": len(dataset.splitlines()),
    }
    values = {
        "LOADER_IMAGE": loader_image,
        "BENCHMARK_RUNNER_KSA": cluster.runner_ksa,
        "BENCHMARK_RUN_ID": run_id,
        "BENCHMARK_RECORD_NAME": name,
        # Base64, as both are JSON in YAML.
        "LOADER_ARGS": base64.b64encode(json.dumps(args).encode()).decode(),
        "BENCHMARK_RUN_INFO": base64.b64encode(json.dumps(info).encode()).decode(),
        "BENCHMARK_REPORT_BUCKET": bucket,
        "BENCHMARK_REPORT_PREFIX": report_prefix,
        "BENCHMARK_REPORT_FOLDER": folder,
        "TOKEN_SECRET": TOKEN_SECRET,
        "DATASET_CONFIGMAP": DATASET_CONFIGMAP,
    }

    cluster.apply_namespace(NAMESPACE)
    apply_json(cluster, {
        "apiVersion": "v1", "kind": "Secret", "metadata": {"name": TOKEN_SECRET, "namespace": NAMESPACE},
        "data": {"token": base64.b64encode(token.encode()).decode()},
    })  # fmt: skip
    apply_json(cluster, {
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": DATASET_CONFIGMAP, "namespace": NAMESPACE},
        "data": {DATASET_KEY: dataset},
    })  # fmt: skip
    # A Job's pod template is immutable. Foreground, so its pod is gone too and
    # the job-name lookup only sees this run's.
    log(f"swebench: deleting any previous {JOB}")
    stream(cluster.kubectl("delete", "job", JOB, "--ignore-not-found", "--cascade=foreground", "--wait", namespace=NAMESPACE))
    log(
        f"swebench: running {name} (run {run_id}):\n"
        f"  loader: {loader_image}\n"
        f"  loader args: {' '.join(args)}\n"
        f"  reports: gs://{bucket}/{report_prefix}/{run_id}/{folder}/"
    )
    stream(cluster.kubectl("apply", "-f", "-"), input=render_job(values))
    # The environments are the run's to delete while it is still going.
    still_running = False
    try:
        wait_started(cluster)
        with jobs.follow_logs(cluster, JOB, NAMESPACE) as stop_logs:
            wait_for_job(cluster, stop_logs, options["timeout"])
    except SwebenchTimeout:
        still_running = True
        raise
    except (KeyboardInterrupt, SystemExit):
        # Ctrl-C, or SIGTERM through cli.run's handler: stop the run so the
        # loader deletes its environments and uploads its events before the
        # cluster goes.
        log(f"swebench: interrupted; stopping {JOB} so it can upload what it recorded")
        still_running = not jobs.stop(cluster, (JOB,), NAMESPACE, STOP_TIMEOUT_SECS)
        raise
    finally:
        # Best-effort: the pod read both as it started, and a dev infra's
        # cluster outlives the run, which the token would too.
        for kind, object_name in (("secret", TOKEN_SECRET), ("configmap", DATASET_CONFIGMAP)):
            subprocess.run(
                cluster.kubectl("delete", kind, object_name, "--ignore-not-found", namespace=NAMESPACE), capture_output=True
            )
        if not still_running:
            reports.record_cleanup(cluster, sandboxenv_rl.ATESPACE, bucket, report_prefix, run_id, folder)
        reports.upload_logs(cluster, NAMESPACE, (JOB,), bucket, report_prefix, run_id, folder)


def run(
    cluster: Cluster,
    name: str,
    case: dict,
    run_id: str | None = None,
    *,
    image_repo: str,
    deploy_workloads: bool = True,
    report_bucket: str | None = None,
    report_prefix: str = reports.REPORT_PREFIX,
    test_case: str | None = None,
    repeat: int = 0,
    warmup: int = 0,
    keeps_bucket: bool = False,
) -> None:
    """Runs the case, recorded under name, as part of run_id, by default a UTC
    timestamp and the commit, with the images built from this tree into
    image_repo. Without deploy_workloads, uses the ate-env-api and template
    already deployed. The reports go under report_prefix in report_bucket, by
    default the cluster's own, which keeps_bucket says outlives the run, as a dev
    infra's does, in a folder named name. Expects the cluster's credentials in
    its kubeconfig. test_case, repeat and warmup say which case it is a repeat
    of, as every type takes them; the loader's own repeats are the case's."""
    case = check_case(name, case, deploy_workloads)
    if sandbox_class(case) == "microvm" and not cluster.sandboxed_kvm:
        raise SwebenchError(f"case {name} is microvm, but cluster {cluster.name}'s sandboxed pool has no /dev/kvm for its guests")
    run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dt%H%M%Sz}-{images.commit()[:12]}"
    if not LABEL_RE.fullmatch(run_id):
        raise ValueError(f"run id {run_id!r} is not a valid label value")
    folder = name
    bucket = report_bucket or cluster.bucket
    reports.refuse_reused(bucket, report_prefix, run_id, folder)
    # By digest, so the Job runs exactly the image built from this tree even
    # if the tag is pushed again meanwhile; run.json records it.
    loader_image = images.pin(images.image_ref(images.SWEBENCH_IMAGES, image_repo, LOADER_IMAGE))

    downloaded = None
    try:
        run_loader(cluster, name, case, run_id, folder, loader_image, image_repo, deploy_workloads, bucket, report_prefix)
    finally:
        # A failed run's reports are kept too. On Cloud Run they'd go with the
        # container, so they stay only in the bucket.
        if ON_CLOUD_RUN:
            log(f"swebench: on Cloud Run, so the reports stay only in gs://{bucket}/{report_prefix}/{run_id}/{folder}/")
        else:
            downloaded = reports.download_reports(bucket, report_prefix, run_id, folder)
    if downloaded is False and report_bucket is None and keeps_bucket:
        log(f"swebench: the reports stay in gs://{bucket}/{report_prefix}/{run_id}/{folder}/", "WARNING")
    # The case's own bucket goes with its cluster, so the local copy is the
    # only one; without it the run recorded nothing that lasts.
    elif downloaded is False and report_bucket is None:
        raise SwebenchError(
            f"the reports of {name} could not be downloaded from gs://{bucket}/{report_prefix}/{run_id}/{folder}/, which"
            " is destroyed with the cluster; pass --report-bucket to keep them in a bucket of your own"
        )
