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

"""Logs the orchestrator's messages, on Cloud Run as one log entry each."""

from __future__ import annotations

import json
import os

# Set by Cloud Run in a job's container.
ON_CLOUD_RUN = "CLOUD_RUN_JOB" in os.environ


def log(message: str, severity: str = "INFO") -> None:
    # Cloud Run logs each line as an entry of its own, except a line of JSON,
    # which is one entry however many lines its message has.
    if ON_CLOUD_RUN:
        print(json.dumps({"message": message, "severity": severity}), flush=True)
    else:
        print(message, flush=True)
