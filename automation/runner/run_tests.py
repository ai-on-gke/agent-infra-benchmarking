#!/usr/bin/env python3

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

"""Applies the runner's infra/, runs the cases in tests.yaml one after another
(every case in the order listed, or the ones --test-case-name names in the
order given), and destroys the infra again."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

import yaml

RUNNER_DIR = Path(__file__).resolve().parent
TESTS_FILE = RUNNER_DIR.parent / "tests.yaml"
INFRA_DIR = RUNNER_DIR / "infra"
# Each execution's state is under STATE_PREFIX/<name> in the state bucket.
STATE_PREFIX = "automation/runner"
# Relative to INFRA_DIR; in .terraform/, which git and the image leave out.
PLAN_FILE = ".terraform/run.tfplan"
# Set by Cloud Run in a job's container.
ON_CLOUD_RUN = "CLOUD_RUN_JOB" in os.environ
# terraform -json's @level, as a Cloud Logging severity.
SEVERITIES = {"trace": "DEBUG", "debug": "DEBUG", "info": "INFO", "warn": "WARNING", "error": "ERROR"}


def log(message: str, severity: str = "INFO") -> None:
    # Cloud Run logs each line as an entry of its own, except a line of JSON,
    # which is one entry however many lines its message has.
    if ON_CLOUD_RUN:
        print(json.dumps({"message": message, "severity": severity}), flush=True)
    else:
        print(message, flush=True)


def fail(message: str) -> NoReturn:
    log(f"run_tests: {message}", "ERROR")
    sys.exit(1)


def load_cases(tests_file: Path) -> dict[str, dict]:
    with tests_file.open() as f:
        cases = (yaml.safe_load(f) or {}).get("cases")
    if not isinstance(cases, dict) or not cases:
        raise ValueError(f"{tests_file} has no `cases` mapping")
    for name, case in cases.items():
        if not isinstance(case, dict):
            raise ValueError(f"{tests_file}: case {name} is not a mapping")
    return cases


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


def log_terraform_event(line: str) -> None:
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


def terraform(command: str, *args: str) -> None:
    """Runs a terraform command quick enough to wait for, and logs all its
    output as one entry."""
    proc = subprocess.run(
        ["terraform", f"-chdir={INFRA_DIR}", command, "-no-color", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log(proc.stdout.strip(), "ERROR" if proc.returncode else "INFO")
    proc.check_returncode()


def terraform_json(command: str, *args: str) -> None:
    """Runs a terraform command with -json and logs each of its messages, an
    error with all its detail, as one entry as it comes."""
    cmd = ["terraform", f"-chdir={INFRA_DIR}", command, "-json", *args]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
        try:
            for line in proc.stdout:
                if line.strip():
                    log_terraform_event(line.rstrip("\n"))
        except BaseException:
            # As subprocess.run does, e.g. on SIGTERM.
            proc.kill()
            raise
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def deploy(*plan_args: str) -> None:
    """Plans, logs the whole plan as terraform shows it, which -json leaves
    out, and applies that plan."""
    terraform_json("plan", "-input=false", f"-out={PLAN_FILE}", *plan_args)
    terraform("show", PLAN_FILE)
    terraform_json("apply", "-input=false", PLAN_FILE)


def run_case(name: str, case: dict) -> None:
    # A no-op for now.
    log(f"{name}: {json.dumps(case)}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tests-file", type=Path, default=TESTS_FILE, help=f"default: {TESTS_FILE}")
    p.add_argument(
        "--test-case-name",
        action="append",
        metavar="NAME",
        help="a case to run; repeat it for more. Default: every case",
    )
    p.add_argument("--project-id", required=True, help="GCP project to create the infra in")
    p.add_argument("--region", default="us-central1", help="default: %(default)s")
    p.add_argument("--state-bucket", required=True, help="GCS bucket holding the infra's Terraform state")
    p.add_argument(
        "--name",
        default=datetime.now(timezone.utc).strftime("run-%Y%m%dt%H%M%Sz"),
        help="this execution's infra and state prefix; unique to it. Default: run-<UTC timestamp>",
    )
    args = p.parse_args()

    try:
        cases = load_cases(args.tests_file)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        fail(str(exc))

    # dict.fromkeys drops repeated names, keeping the first.
    names = list(dict.fromkeys(args.test_case_name or cases))
    unknown = [name for name in names if name not in cases]
    if unknown:
        fail(f"no case {', '.join(unknown)} in {args.tests_file}; it has {', '.join(cases)}")

    if not shutil.which("terraform"):
        fail("terraform is required to apply the infra")

    # Cloud Run stops a task that outlives its timeout with SIGTERM; exiting
    # on it, rather than dying, still destroys the infra.
    signal.signal(signal.SIGTERM, lambda *_: fail("stopped by SIGTERM"))

    tf_vars = [
        f"-var=project_id={args.project_id}",
        f"-var=region={args.region}",
        f"-var=default_resource_name={args.name}",
    ]
    try:
        # -reconfigure, as the .terraform/ left by the last execution names its prefix.
        terraform(
            "init",
            "-input=false",
            "-reconfigure",
            f"-backend-config=bucket={args.state_bucket}",
            f"-backend-config=prefix={STATE_PREFIX}/{args.name}",
        )
        try:
            deploy(*tf_vars)
            for name in names:
                run_case(name, cases[name])
        finally:
            # Also after a failed apply, which may have created part of the infra.
            deploy("-destroy", *tf_vars)
    except subprocess.CalledProcessError as exc:
        fail(f"terraform {exc.cmd[2]} failed in {INFRA_DIR}")


if __name__ == "__main__":
    main()
