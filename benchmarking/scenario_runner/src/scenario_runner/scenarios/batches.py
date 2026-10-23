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

"""Which of a case's variants share a cluster, and what each cluster applied
for the cases is recorded as."""

from __future__ import annotations

import yaml

from .. import benchmarks
from .bare_metal import BARE_METAL
from .install import installs
from .repeats import record_names, shares_cluster
from .variants import grouped


def cluster_setup(name: str, case: dict) -> tuple:
    """What the case's cluster is applied with and has installed, which a
    case's variants alike in share."""
    return benchmarks.cluster_config(name, case), case.get(BARE_METAL, False), yaml.safe_dump(installs(case))


def batches(names: list[str], cases: dict[str, dict]) -> list[list[str]]:
    """A case's variants by the cluster they can share, alike in its setup, in
    the order the first of each comes; one whose repeats have a cluster each
    alone."""
    out = {}
    for name in names:
        out.setdefault(cluster_setup(name, cases[name]) if shares_cluster(cases[name]) else name, []).append(name)
    return list(out.values())


def batch_key(group: str, members: list[str], batch: list[str]) -> str:
    """What the batch's cluster is recorded as: its case's, or its group's
    for all of the group, or else its first's."""
    return group if len(batch) > 1 and batch == members else batch[0]


def cluster_keys(cases: dict[str, dict]) -> list[str]:
    """What every cluster the cases run on applied for them is recorded as."""
    keys = []
    for group, members in grouped(cases).items():
        for batch in batches(members, cases):
            first = cases[batch[0]]
            keys += [batch_key(group, members, batch)] if shares_cluster(first) else record_names(batch[0], first)
    return keys
