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

"""The locustfiles as locust loads them: from a directory that is neither
theirs nor the image's, as -f /app/tests/<user-class>.py does."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from locust.argument_parser import parse_locustfile_paths

TESTS_DIR = Path(__file__).resolve().parent.parent / "tests"


def list_users(locustfile: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "locust", "-f", str(locustfile), "--list", *args],
        cwd=tempfile.mkdtemp(),
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


class Runworkload(unittest.TestCase):
    def test_loads_the_master_s_listeners_and_declares_its_user(self):
        # --warm-up is _master's: locust refuses it unless that is loaded.
        listed = list_users(TESTS_DIR / "runworkload.py", "--warm-up", "10s")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("RunWorkloadUser", listed.stdout)

    def test_the_gate_leaves_out_the_user_but_not_the_listeners(self):
        listed = list_users(TESTS_DIR / "runworkload.py", "--warm-up", "10s", LOCUST_NO_RUNWORKLOAD_USER="1")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertNotIn("RunWorkloadUser", listed.stdout)


class Master(unittest.TestCase):
    def test_is_not_a_locustfile_of_the_directory(self):
        paths = [Path(path).name for path in parse_locustfile_paths([str(TESTS_DIR)])]
        self.assertIn("runworkload.py", paths)
        self.assertNotIn("_master.py", paths)
