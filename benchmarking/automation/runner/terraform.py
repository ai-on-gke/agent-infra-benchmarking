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

"""Runs terraform in a config's directory and logs its output, each of its
messages as one log entry. A failed command raises CalledProcessError, whose
cmd[2] is the terraform command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .log import log

# Relative to the config's directory; in .terraform/, which git and the image
# leave out.
PLAN_FILE = ".terraform/run.tfplan"
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


def log_event(line: str) -> None:
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
        values = [
            f"{name} = {'(sensitive value)' if out.get('sensitive') else json.dumps(out['value'])}"
            for name, out in event.get("outputs", {}).items()
            if "value" in out
        ]
        if not values:
            # Before apply, only how many there are.
            return
        message = "\n".join(["Outputs:", *values])
    log(message, SEVERITIES.get(event.get("@level"), "DEFAULT"))


def run(chdir: Path, command: str, *args: str) -> None:
    """Runs a terraform command quick enough to wait for, and logs all its
    output as one entry."""
    proc = subprocess.run(
        ["terraform", f"-chdir={chdir}", command, "-no-color", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log(proc.stdout.strip(), "ERROR" if proc.returncode else "INFO")
    proc.check_returncode()


def run_json(chdir: Path, command: str, *args: str) -> None:
    """Runs a terraform command with -json and logs each of its messages, an
    error with all its detail, as one entry as it comes."""
    cmd = ["terraform", f"-chdir={chdir}", command, "-json", *args]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
        try:
            for line in proc.stdout:
                if line.strip():
                    log_event(line.rstrip("\n"))
        except BaseException:
            # As subprocess.run does, e.g. on SIGTERM.
            proc.kill()
            raise
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def deploy(chdir: Path, *plan_args: str) -> None:
    """Plans, logs the whole plan as terraform shows it, which -json leaves
    out, and applies that plan."""
    run_json(chdir, "plan", "-input=false", f"-out={PLAN_FILE}", *plan_args)
    run(chdir, "show", PLAN_FILE)
    run_json(chdir, "apply", "-input=false", PLAN_FILE)
