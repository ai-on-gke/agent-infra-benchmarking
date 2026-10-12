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

"""Runs the scenario-runner's commands, logging their output as it comes."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import IO, Callable, TypeVar

from .log import log

T = TypeVar("T")

ATTEMPTS = 3
RETRY_DELAY = 30


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
        lines = proc.stderr if stdout else proc.stdout
        try:
            if input is not None:
                proc.stdin.write(input)
                proc.stdin.close()
            for line in lines:
                log_line(line.rstrip("\n"))
        except BaseException as exc:
            # Stopped, not killed: terraform killed mid-apply leaves what it was
            # creating out of its state, and a GCS state lock behind. A Ctrl-C
            # reached it from the terminal too; anything else, such as cli.run's
            # SIGTERM, did not. Its output is read on until it exits, so a full
            # pipe cannot hold it up, and a second interrupt kills it.
            if not isinstance(exc, KeyboardInterrupt):
                proc.terminate()
            try:
                for line in lines:
                    log_line(line.rstrip("\n"))
                proc.wait()
            except BaseException:
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


def retry(what: str, func: Callable[[], T], attempts: int = ATTEMPTS, delay: float = RETRY_DELAY) -> T:
    """Calls func until a call doesn't raise CalledProcessError, at most
    attempts times, and raises the last one's."""
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except subprocess.CalledProcessError as exc:
            if attempt == attempts:
                raise
            log(f"{what}: {failure(exc)}, attempt {attempt} of {attempts}; retrying in {delay:g}s", "WARNING")
            time.sleep(delay)
