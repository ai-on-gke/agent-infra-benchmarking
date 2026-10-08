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

"""Runs terraform in a config's directory and logs its output, as text or as
-json, each of whose messages is one log entry. A failed command raises
CalledProcessError, whose cmd[2] is the terraform command."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import RUNNER_DIR
from .log import log
from .process import environ, stream

# Every TF_DATA_DIR the orchestrator uses starts empty, so its providers come
# from here rather than from a download each.
PLUGIN_CACHE_DIR = Path(os.environ.get("TF_PLUGIN_CACHE_DIR") or RUNNER_DIR / ".terraform-plugin-cache")

# terraform -json's @level, as a Cloud Logging severity.
SEVERITIES = {"trace": "DEBUG", "debug": "DEBUG", "info": "INFO", "warn": "WARNING", "error": "ERROR"}


def format_diagnostic(diag: dict) -> str:
    """Lays out a terraform -json diagnostic as terraform's text output does."""
    lines = [f"{diag['severity'].capitalize()}: {diag['summary']}", ""]
    if diag.get("address"):
        lines.append(f"  with {diag['address']},")
    rng, snippet = diag.get("range"), diag.get("snippet")
    if rng:
        context = f", in {snippet['context']}" if snippet and snippet.get("context") else ""
        lines.append(f"  on {rng['filename']} line {rng['start']['line']}{context}:")
    if snippet:
        lines.append(f"  {snippet['start_line']}: {snippet['code']}")
        lines += [f"    {v['traversal']} {v['statement']}" for v in snippet.get("values", [])]
    if diag.get("detail"):
        lines += ["", diag["detail"]]
    return "\n".join(lines).rstrip()


def format_output(name: str, out: dict) -> str:
    """An output as terraform output shows it, a sensitive one hidden."""
    return f"{name} = {'(sensitive value)' if out.get('sensitive') else json.dumps(out['value'])}"


def log_event(line: str) -> None:
    if not line.strip():
        return
    try:
        event = json.loads(line)
    except ValueError:
        event = None
    if not isinstance(event, dict):
        # Whatever terraform writes outside -json, e.g. a crash.
        log(line)
        return
    kind = event.get("type")
    if kind == "planned_change":
        # The plan's own entry, from terraform show, has each change in full.
        return
    message = event.get("@message", line)
    if "diagnostic" in event:
        message = format_diagnostic(event["diagnostic"])
    elif kind == "outputs":
        values = [format_output(name, out) for name, out in event.get("outputs", {}).items() if "value" in out]
        if not values:
            # Before apply, only how many there are.
            return
        message = "\n".join(["Outputs:", *values])
    log(message, SEVERITIES.get(event.get("@level"), "DEFAULT"))


def data_env(path: Path) -> dict[str, str]:
    PLUGIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return {"TF_DATA_DIR": str(path), "TF_PLUGIN_CACHE_DIR": str(PLUGIN_CACHE_DIR)}


def backend_block(backend: str, settings: dict[str, str]) -> str:
    # A JSON string is an HCL one too.
    lines = [f"    {key} = {json.dumps(value)}" for key, value in settings.items()]
    return "\n".join(["terraform {", f'  backend "{backend}" {{', *lines, "  }", "}"]) + "\n"


def run(chdir: Path, command: str, *args: str, env: dict[str, str] | None = None) -> None:
    """Runs a terraform command quick enough to wait for, and logs all its
    output as one entry."""
    proc = subprocess.run(
        ["terraform", f"-chdir={chdir}", command, "-no-color", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environ(env),
    )
    log(proc.stdout.strip(), "ERROR" if proc.returncode else "INFO")
    proc.check_returncode()


def output(chdir: Path, env: dict[str, str] | None = None) -> dict:
    """The config's outputs, by name. Only an error is logged, as -json shows
    sensitive values too."""
    cmd = ["terraform", f"-chdir={chdir}", "output", "-json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=environ(env))
    if proc.returncode:
        log(proc.stderr.strip(), "ERROR")
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return {name: out["value"] for name, out in json.loads(proc.stdout).items()}


def run_text(chdir: Path, command: str, *args: str, env: dict[str, str] | None = None) -> None:
    """Runs a terraform command and logs each line of its output as one entry
    as it comes."""
    stream(["terraform", f"-chdir={chdir}", command, "-no-color", *args], env=env)


def run_json(chdir: Path, command: str, *args: str, env: dict[str, str] | None = None) -> None:
    """Runs a terraform command with -json and logs each of its messages, an
    error with all its detail, as one entry as it comes."""
    stream(["terraform", f"-chdir={chdir}", command, "-json", *args], log_event, env=env)


def deploy(
    chdir: Path,
    *plan_args: str,
    plan_file: str,
    json_output: bool = False,
    env: dict[str, str] | None = None,
) -> None:
    """Plans and applies that plan. With json_output, also logs the whole plan
    as terraform shows it, which -json leaves out."""
    if not json_output:
        run_text(chdir, "plan", "-input=false", f"-out={plan_file}", *plan_args, env=env)
        run_text(chdir, "apply", "-input=false", plan_file, env=env)
        return
    run_json(chdir, "plan", "-input=false", f"-out={plan_file}", *plan_args, env=env)
    run(chdir, "show", plan_file, env=env)
    run_json(chdir, "apply", "-input=false", plan_file, env=env)
