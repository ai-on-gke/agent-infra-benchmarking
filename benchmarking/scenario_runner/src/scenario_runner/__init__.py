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

"""Runs the cases in scenarios.yaml on infra of their own, with the command
lines in cli/."""

import re
from pathlib import Path

RUNNER_DIR = Path(__file__).resolve().parent.parent.parent
# scenarios.yaml and the Terraform configs its cases run on.
SCENARIOS_DIR = RUNNER_DIR.parent / "scenarios"
# The runs' and dev infras' local files, gitignored.
LOCAL_DATA_DIR = RUNNER_DIR / ".local-data"
# A Kubernetes label value, which run ids, case names and build versions end up as.
LABEL_RE = re.compile(r"[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?")
