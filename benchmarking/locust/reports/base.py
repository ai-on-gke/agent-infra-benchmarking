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

"""Shared types for run reports.

A report turns one finished run into one self-contained file. It reads the
run's series back out of Prometheus rather than accumulating state during the
run, so a report can be added without touching the load path.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RunContext:
    """What a report knows about the run it is describing."""

    run_id: str
    test_case: str
    users: int
    spawn_rate: float
    duration: str
    workload: str
    template: str
    user_class: str
    # Unix seconds bounding the query window.
    started: float
    ended: float
    prometheus_url: str


class Report(Protocol):
    name: str
    filename: str

    def render(self, ctx: RunContext) -> str:
        """Return the file's complete contents, or raise if the run has no
        data for it."""

