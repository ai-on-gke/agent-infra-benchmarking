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

"""Checks the cases scenarios.yaml's cases make. A case's own keys are
REPEATS and BARE_METAL; the rest of what it states is in the mapping of its
type, named by the type, which only the type's module reads, but for the
cluster the type asks for in its `infra` and what it installs on it."""

from __future__ import annotations

from .. import LABEL_RE, benchmarks
from ..benchmarks import noop
from . import bare_metal
from .bare_metal import BARE_METAL
from .install import AGENT_SANDBOX, INSTALL, check_install, control_plane
from .repeats import REPEATS, check_repeats, run_names
from .variants import VARIANT, with_variants

CASE_KEYS = {REPEATS, BARE_METAL}


def load_cases(where: str, cases: dict) -> dict[str, dict]:
    """The cases, made of their parameters already, with their variants in
    their place, each checked, with `type` filled in from the mapping that
    names it and `repeats` with its defaults, so run.json records how the case
    was sampled."""
    return {name: checked(where, name, case) for name, case in with_variants(where, cases).items()}


def checked(where: str, name: str, case: dict) -> dict:
    known = benchmarks.types()
    if "type" in case:
        raise ValueError(f"{where}: case {name}'s type is the key of its mapping, one of {', '.join(known)}")
    given = [key for key in known if key in case]
    if len(given) != 1:
        raise ValueError(f"{where}: case {name} needs a mapping of exactly one type of {', '.join(known)}")
    kind = given[0]
    unknown = case.keys() - CASE_KEYS - {kind, VARIANT}
    if unknown:
        raise ValueError(f"{where}: case {name} has unknown {', '.join(sorted(unknown))}")
    options = case[kind]
    # noop's states nothing it must, and so may be left null.
    if options is None and kind == noop.TYPE:
        options = {}
    if not isinstance(options, dict):
        raise ValueError(f"{where}: case {name}'s `{kind}` must be a mapping")
    try:
        check_install(options.get(INSTALL, {}))
    except ValueError as exc:
        raise ValueError(f"{where}: case {name}'s `{kind}.{INSTALL}` {exc}") from None
    try:
        repeats = check_repeats(case.get(REPEATS, {}))
    except ValueError as exc:
        raise ValueError(f"{where}: case {name}'s `{REPEATS}` {exc}") from None
    try:
        bare_metal.check_case(case)
    except ValueError as exc:
        raise ValueError(f"{where}: case {name}'s {exc}") from None
    case = {**case, "type": kind, kind: options, REPEATS: repeats}
    try:
        benchmarks.check(name, case)
        check_gke_sandbox(name, case)
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from None
    check_records(where, name, case)
    return case


def check_gke_sandbox(name: str, case: dict) -> None:
    """Refuses a case on agent-sandbox whose sandboxed pool runs no GKE
    Sandbox, which its pods' gVisor is, and any other on one: substrate's
    atelet does not run there."""
    kind = case["type"]
    gke_sandbox = benchmarks.cluster_config(name, case).gke_sandbox
    if control_plane(case) == AGENT_SANDBOX and not gke_sandbox:
        raise ValueError(
            f"case {name} installs {AGENT_SANDBOX}, whose pods run in gVisor through GKE Sandbox: set its"
            f" `{kind}.infra.cluster.gke-sandbox: true`"
        )
    if control_plane(case) != AGENT_SANDBOX and gke_sandbox:
        raise ValueError(f"case {name}'s `{kind}.infra.cluster.gke-sandbox` is only for cases on {AGENT_SANDBOX}")


def check_records(where: str, name: str, case: dict) -> None:
    """Refuses a case whose runs' names, as each runs under its own, are not
    label values."""
    for record in run_names(name, case):
        if not LABEL_RE.fullmatch(record):
            raise ValueError(f"{where}: case {name} would be recorded as {record}, which is not a valid label value")
