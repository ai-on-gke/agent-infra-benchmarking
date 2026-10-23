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

"""What the repo and its submodules are checked out at."""

from __future__ import annotations

import subprocess
from pathlib import Path


def head(repo: Path) -> str:
    """The commit repo is at; raises CalledProcessError if git cannot tell."""
    cmd = ["git", "rev-parse", "HEAD"]
    return subprocess.run(cmd, cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def describe(repo: Path) -> str:
    """git describe of repo, -dirty with uncommitted changes; empty if git
    cannot tell."""
    proc = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], cwd=repo, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""
