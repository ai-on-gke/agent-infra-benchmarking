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

"""A case's `control-plane`: what the scenario-runner installs on its cluster
before running it. Apart from cases, which the runners that read it are
imported by."""

CONTROL_PLANE = "control-plane"
AGENT_SUBSTRATE = "agent-substrate"
AGENT_SANDBOX = "agent-sandbox"
NO_CONTROL_PLANE = "none"
CONTROL_PLANES = [AGENT_SUBSTRATE, AGENT_SANDBOX, NO_CONTROL_PLANE]


def control_plane(case: dict) -> str:
    return case.get(CONTROL_PLANE, AGENT_SUBSTRATE)
