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
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Callable, Iterator, TypeVar

from .log import log

T = TypeVar("T")

ATTEMPTS = 3
RETRY_DELAY = 30
# How soon a second Ctrl-C has to follow the first to interrupt what is shielded.
SHIELD_SECS = 5

_shielding = False


def environ(env: dict[str, str | None] | None) -> dict[str, str] | None:
    """The process's environment with env added, for subprocess. A None value
    removes that variable."""
    if not env:
        return None
    return {key: value for key, value in {**os.environ, **env}.items() if value is not None}


def reached_by_ctrl_c(proc: subprocess.Popen) -> bool:
    """Whether a Ctrl-C at the terminal reached proc too, as it does when proc
    is in the terminal's foreground process group."""
    for f in (sys.stdin, sys.stderr):
        try:
            return os.tcgetpgrp(f.fileno()) == os.getpgid(proc.pid)
        except (OSError, AttributeError, ValueError):
            continue
    return False


def stream(
    cmd: list[str],
    log_line: Callable[[str], None] = log,
    env: dict[str, str | None] | None = None,
    cwd: Path | None = None,
    input: str | None = None,
    stdout: IO[str] | None = None,
) -> None:
    """Runs a command, with input on its stdin, and passes each line of its
    output to log_line as it comes. With stdout, its stdout goes there and only
    its stderr is logged. Within shielded, the command runs out of the
    terminal's reach."""
    with subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=stdout or subprocess.PIPE,
        stderr=subprocess.PIPE if stdout else subprocess.STDOUT,
        text=True,
        env=environ(env),
        cwd=cwd,
        start_new_session=_shielding,
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
            # creating out of its state, and a GCS state lock behind. It gets
            # one SIGINT: from the terminal along with this process, or else
            # from here, as a second makes terraform quit at once. Anything
            # else, such as cli.run's SIGTERM, is passed on as SIGTERM. Its
            # output is read on until it exits, so a full pipe cannot hold it
            # up, and a second interrupt kills it.
            if not isinstance(exc, KeyboardInterrupt):
                proc.terminate()
            elif not reached_by_ctrl_c(proc):
                proc.send_signal(signal.SIGINT)
            try:
                for line in lines:
                    log_line(line.rstrip("\n"))
                proc.wait()
            except BaseException:
                proc.kill()
            raise
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


@contextmanager
def shielded(what: str) -> Iterator[None]:
    """Shields the with block, such as the destroy that cleans up after a run,
    from a single Ctrl-C, its pauses and every command it runs alike: a Ctrl-C
    only warns, and a second within SHIELD_SECS of it raises
    KeyboardInterrupt, which stops the command running then."""
    global _shielding
    # Python takes signals only in the main thread, and only it can set a handler.
    if _shielding or threading.current_thread() is not threading.main_thread():
        yield
        return
    warned_at = None

    def interrupt(signum, frame) -> None:
        nonlocal warned_at
        if warned_at is not None and time.monotonic() - warned_at < SHIELD_SECS:
            raise KeyboardInterrupt
        warned_at = time.monotonic()
        log(
            f"{what} goes on; press Ctrl-C again within {SHIELD_SECS}s to interrupt it, which leaves what it has"
            " not removed yet",
            "WARNING",
        )

    previous = signal.signal(signal.SIGINT, interrupt)
    _shielding = True
    try:
        yield
    finally:
        _shielding = False
        signal.signal(signal.SIGINT, previous)


def output(
    cmd: list[str],
    log_line: Callable[[str], None] = log,
    env: dict[str, str | None] | None = None,
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


def retry(
    what: str,
    func: Callable[[], T],
    attempts: int = ATTEMPTS,
    delay: float = RETRY_DELAY,
    give_up: tuple[type[subprocess.CalledProcessError], ...] = (),
) -> T:
    """Calls func until a call doesn't raise CalledProcessError, at most
    attempts times, and raises the last one's, or at once one of give_up."""
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except subprocess.CalledProcessError as exc:
            if attempt == attempts or isinstance(exc, give_up):
                raise
            log(f"{what}: {failure(exc)}, attempt {attempt} of {attempts}; retrying in {delay:g}s", "WARNING")
            time.sleep(delay)
