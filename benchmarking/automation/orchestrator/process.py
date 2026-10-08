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

"""Runs the orchestrator's commands, logging their output as it comes."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from .log import log


def environ(env: dict[str, str] | None) -> dict[str, str] | None:
    """The process's environment with env added, for subprocess."""
    return {**os.environ, **env} if env else None


def stream(
    cmd: list[str],
    log_line: Callable[[str], None] = log,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> None:
    """Runs a command and passes each line of its output to log_line as it
    comes."""
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environ(env), cwd=cwd
    ) as proc:
        try:
            for line in proc.stdout:
                log_line(line.rstrip("\n"))
        except BaseException:
            # As subprocess.run does, e.g. on SIGTERM.
            proc.kill()
            raise
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def failure(exc: subprocess.CalledProcessError) -> str:
    if exc.cmd[0] == "terraform":
        # cmd[1] is -chdir=<the config's directory>.
        return f"terraform {exc.cmd[2]} failed in {exc.cmd[1].removeprefix('-chdir=')}"
    return f"{' '.join(exc.cmd)} failed"
