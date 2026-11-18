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

"""Deploys or deletes mock-llm, from workloads/manifests/mock-llm.yaml.tmpl:
the LLM the openclaw actors call instead of a real one."""

from __future__ import annotations

import re
import subprocess
from string import Template

from .. import RUNNER_DIR, images
from ..control_planes import worker_pool
from ..infra.cluster import Cluster
from ..log import log
from ..process import stream

MOCK_LLM_MANIFEST = RUNNER_DIR.parent / "workloads" / "manifests" / "mock-llm.yaml.tmpl"
MOCK_LLM_IMAGE = "mock-llm"
# The recordings baked into mock-llm at /recordings, each a directory with the
# cassette.jsonl and turns.txt of the recorder's `run.sh WORKLOAD record NAME -i`.
RECORDINGS_DIR = RUNNER_DIR.parent / "workloads" / "images" / "mock-llm" / "recordings"
REPLAY_TIMINGS = ("instant", "recorded")
# Has to match the manifest's, which the openclaw template's MOCK_LLM_BASE_URL
# names.
NAMESPACE = worker_pool.NAMESPACE
MOCK_LLM_URL = f"http://mock-llm.{NAMESPACE}.svc.cluster.local"
# How much sooner than the worker's deadline for a call, the case's
# workload-timeout, the actor gives up on a turn: so a slow turn ends in the
# actor with a 504 that says so, and its processes are gone before the worker
# suspends the actor.
EXEC_TIMEOUT_MARGIN_SECS = 20
# The actor's, for a case with no workload-timeout.
DEFAULT_EXEC_TIMEOUT_SECS = 100
# A Go duration, as the worker's --workload-timeout takes it.
DURATION_PART_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(h|ms|m|s)")
DURATION_UNIT_SECS = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}


def deploy_mock_llm(
    cluster: Cluster, image_repo: str, timeout: int, recording: str | None = None, replay_timing: str = "instant"
) -> None:
    """Expects mock-llm to have been built from this tree into image_repo.
    With a recording, of RECORDINGS_DIR, mock-llm replays it rather than
    scripting the answers."""
    log(f"workloads: deploying mock-llm, {f'replaying {recording} ({replay_timing})' if recording else 'scripted'}")
    image = images.pin(images.image_ref(images.WORKLOADS_IMAGES, image_repo, MOCK_LLM_IMAGE))
    manifest = Template(MOCK_LLM_MANIFEST.read_text()).substitute(
        IMAGE_WITH_DIGEST=image,
        MOCK_LLM_MODE="replay" if recording else "script",
        CASSETTE=f"/recordings/{recording}/cassette.jsonl" if recording else "",
        REPLAY_TIMING=replay_timing,
    )
    stream(cluster.kubectl("apply", "-f", "-", namespace=NAMESPACE), input=manifest)
    log(f"workloads: waiting for mock-llm to be ready (timeout: {timeout}s)")
    stream(cluster.kubectl("rollout", "status", "deployment/mock-llm", f"--timeout={timeout}s", namespace=NAMESPACE))


def recording_turns_url(name: str) -> str:
    """Where mock-llm, replaying the recording, serves the prompts of its
    conversation to the worker. They are checked here too, in this tree that
    mock-llm is built from, to fail before a cluster is up."""
    path = RECORDINGS_DIR / name
    if not name or name in (".", "..") or "/" in name or not (path / "cassette.jsonl").is_file():
        have = sorted(p.name for p in RECORDINGS_DIR.iterdir() if (p / "cassette.jsonl").is_file())
        raise ValueError(f"no recording {name!r} in {RECORDINGS_DIR}; there are {', '.join(have) or 'none'}")
    turns_file = path / "turns.txt"
    turns = turns_file.read_text().splitlines() if turns_file.is_file() else []
    if not turns or not all(turns):
        raise ValueError(f"recording {name} has no conversation in {turns_file}; record one with the recorder's run.sh WORKLOAD record NAME -i")
    return f"{MOCK_LLM_URL}/recordings/{name}/turns"


def exec_timeout_secs(workload_timeout: str | None) -> int:
    """The templates' EXEC_TIMEOUT_SECS for a case's workload-timeout,
    such as 120s or 4m."""
    if not workload_timeout or workload_timeout == "0":
        return DEFAULT_EXEC_TIMEOUT_SECS
    parts = DURATION_PART_RE.findall(workload_timeout)
    if "".join(number + unit for number, unit in parts) != workload_timeout:
        raise ValueError(f"workload-timeout {workload_timeout!r} is not a duration such as 120s or 4m")
    secs = sum(float(number) * DURATION_UNIT_SECS[unit] for number, unit in parts)
    if secs <= EXEC_TIMEOUT_MARGIN_SECS:
        raise ValueError(
            f"workload-timeout {workload_timeout} leaves the actors no time for a turn; it needs more than"
            f" {EXEC_TIMEOUT_MARGIN_SECS}s"
        )
    return int(secs - EXEC_TIMEOUT_MARGIN_SECS)


def delete_mock_llm(cluster: Cluster) -> None:
    try:
        stream(cluster.kubectl("delete", "deployment,service", "mock-llm", "--ignore-not-found", namespace=NAMESPACE))
    except subprocess.CalledProcessError:
        log("workloads: mock-llm not deleted")
