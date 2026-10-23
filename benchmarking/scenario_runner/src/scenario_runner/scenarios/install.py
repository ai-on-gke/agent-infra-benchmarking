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

"""The `install` in a case's type's mapping: what the scenario-runner installs
on its cluster before running it, a mapping of each to its own settings,
nothing when left out. Apart from cases, which the runners that read it are
imported by."""

from __future__ import annotations

from ..workloads import SANDBOX_CONFIGS

INSTALL = "install"
SUBSTRATE = "substrate"
AGENT_SANDBOX = "agent-sandbox"
# The control planes, of which a case installs one at most.
CONTROL_PLANES = [SUBSTRATE, AGENT_SANDBOX]
# Each's class of the case's workers, which substrate is installed with too.
SANDBOX_CLASS = "sandbox-class"
KEYS = {SUBSTRATE: {SANDBOX_CLASS}, AGENT_SANDBOX: {SANDBOX_CLASS}}


def check_install(given: object) -> None:
    """Raises ValueError, its message what is wrong with the case's `install`."""
    if not isinstance(given, dict):
        raise ValueError(f"must be a mapping of {', '.join(KEYS)} to their settings, got {given!r}")
    unknown = given.keys() - KEYS.keys()
    if unknown:
        raise ValueError(f"has unknown {', '.join(sorted(unknown))}")
    if len(given.keys() & set(CONTROL_PLANES)) > 1:
        raise ValueError(f"may name one of {', '.join(CONTROL_PLANES)} at most")
    for name, settings in given.items():
        if not isinstance(settings, dict) or settings.keys() != KEYS[name]:
            raise ValueError(f"`{name}` must be a mapping of {', '.join(sorted(KEYS[name]))}, got {settings!r}")
        if settings[SANDBOX_CLASS] not in SANDBOX_CONFIGS:
            raise ValueError(f"`{name}.{SANDBOX_CLASS}` must be one of {', '.join(SANDBOX_CONFIGS)}")


def installs(case: dict) -> dict:
    return (case.get(case["type"]) or {}).get(INSTALL) or {}


def control_plane(case: dict) -> str | None:
    """The control plane the case installs, None for none."""
    return next((name for name in CONTROL_PLANES if name in installs(case)), None)


def sandbox_class(case: dict) -> str | None:
    """The class of the case's workers, None without a control plane."""
    plane = control_plane(case)
    return installs(case)[plane][SANDBOX_CLASS] if plane else None
