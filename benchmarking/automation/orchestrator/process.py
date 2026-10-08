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
import tempfile
from pathlib import Path
from typing import IO, Callable

from .log import log


def environ(env: dict[str, str] | None) -> dict[str, str] | None:
    """The process's environment with env added, for subprocess."""
    return {**os.environ, **env} if env else None


def stream(
    cmd: list[str],
    log_line: Callable[[str], None] = log,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    input: str | None = None,
    stdout: IO[str] | None = None,
) -> None:
    """Runs a command, with input on its stdin, and passes each line of its
    output to log_line as it comes. With stdout, its stdout goes there and only
    its stderr is logged."""
    with subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=stdout or subprocess.PIPE,
        stderr=subprocess.PIPE if stdout else subprocess.STDOUT,
        text=True,
        env=environ(env),
        cwd=cwd,
    ) as proc:
        try:
            if input is not None:
                proc.stdin.write(input)
                proc.stdin.close()
            for line in proc.stderr if stdout else proc.stdout:
                log_line(line.rstrip("\n"))
        except BaseException:
            # As subprocess.run does, e.g. on SIGTERM.
            proc.kill()
            raise
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def output(
    cmd: list[str],
    log_line: Callable[[str], None] = log,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    input: str | None = None,
) -> str:
    """Runs a command as stream does, and returns its stdout rather than
    logging it."""
    with tempfile.TemporaryFile("w+") as out:
        stream(cmd, log_line, env, cwd, input, stdout=out)
        out.seek(0)
        return out.read()


def failure(exc: subprocess.CalledProcessError) -> str:
    if exc.cmd[0] == "terraform":
        # cmd[1] is -chdir=<the config's directory>.
        return f"terraform {exc.cmd[2]} failed in {exc.cmd[1].removeprefix('-chdir=')}"
    return f"{' '.join(exc.cmd)} failed"
