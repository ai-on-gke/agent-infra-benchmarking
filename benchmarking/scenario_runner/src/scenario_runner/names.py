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

"""The labels and names of an execution's infra, or of a dev infra. Its
resources are labeled with what it is, and named after a hash of those labels,
each in the longest form its name's limit takes:

    short    run-<hash>                       25, the node SA's <name>-node taking 30
    medium   <run-id>-<hash8>                 40, a GKE cluster's
    bucket   <project-id>-<run-id>-<hash8>    63, unique across GCS

with <run-id> and <project-id> cut to fit. A dev infra's are dev-<hash>,
<name>-<hash8> and <project-id>-<name>-<hash8>, its labels only what stays the
same between the executions it outlives, so that its names do too."""

from __future__ import annotations

import hashlib
import os
import re

from . import images
from .deployment import tf_vars

SHORT_LENGTH = 25
MEDIUM_LENGTH = 40
BUCKET_LENGTH = 63
HASH_LENGTH = 8
# What a GCP resource name takes.
NAME_RE = re.compile(r"[a-z]([-a-z0-9]*[a-z0-9])?")
# What a GCP label value takes.
LABEL_VALUE_RE = re.compile(r"[a-z0-9_-]{0,63}")


class NamingError(Exception):
    pass


def checked(values: dict[str, str]) -> dict[str, str]:
    invalid = [f"{key}={value!r}" for key, value in values.items() if not LABEL_VALUE_RE.fullmatch(value)]
    if invalid:
        raise NamingError(
            f"not valid GCP label values, which take at most 63 lowercase letters, digits, '-' and '_': {', '.join(invalid)}"
        )
    return values


def labels(run_id: str, test_case: str | None = None) -> dict[str, str]:
    """The labels of the execution's infra, and with test_case of that case's
    cluster."""
    values = {
        "benchmark-run-id": run_id,
        "benchmark-deployment": os.environ.get("CLOUD_RUN_JOB", "local"),
        "benchmark-trigger": os.environ.get("BENCHMARK_TRIGGER", "local"),
        "benchmark-commit": images.tag(),
    }
    if "CLOUD_RUN_EXECUTION" in os.environ:
        values["benchmark-execution"] = os.environ["CLOUD_RUN_EXECUTION"]
    if test_case is not None:
        values["benchmark-test-case"] = test_case
    return checked(values)


def dev_infra_labels(name: str) -> dict[str, str]:
    return checked({"benchmark-dev-infra": name, "benchmark-deployment": "local", "benchmark-trigger": "local"})


def digest(labels: dict[str, str]) -> str:
    text = "\n".join(f"{key}={value}" for key, value in sorted(labels.items()))
    return hashlib.sha256(text.encode()).hexdigest()


def hashed(prefix: str, hexdigest: str, length: int, hash_length: int = HASH_LENGTH) -> str:
    """prefix, cut so the name fits length, then -<hash>."""
    return f"{prefix[: length - hash_length - 1].rstrip('-')}-{hexdigest[:hash_length]}"


def named_vars(project_id: str, region: str, prefix: str, short_prefix: str, values: dict[str, str], cluster: bool) -> dict:
    """The variables of a scenario_runner/infra, or with cluster of a case's
    cluster: its labels, the medium name as default_resource_name, and for a
    cluster the short name and the bucket's."""
    if not NAME_RE.fullmatch(prefix):
        raise NamingError(
            f"{prefix!r} starts the infra's names, so use lowercase letters, digits and '-', starting with a letter and"
            " ending with a letter or digit"
        )
    hexdigest = digest(values)
    variables = {**tf_vars(project_id, region, hashed(prefix, hexdigest, MEDIUM_LENGTH)), "labels": values}
    if cluster:
        short_hash_length = SHORT_LENGTH - len(short_prefix) - 1
        variables["short_resource_name"] = hashed(short_prefix, hexdigest, SHORT_LENGTH, short_hash_length)
        variables["bucket_name"] = hashed(f"{project_id}-{prefix}", hexdigest, BUCKET_LENGTH)
    return variables


def infra_vars(project_id: str, region: str, run_id: str, test_case: str | None = None) -> dict:
    """The variables of the execution's scenario_runner/infra, and with
    test_case of that case's cluster."""
    return named_vars(project_id, region, run_id, "run", labels(run_id, test_case), test_case is not None)


def dev_infra_vars(project_id: str, region: str, name: str, cluster: bool) -> dict:
    """The variables of the dev infra's scenario_runner/infra, and with cluster
    of its cluster."""
    return named_vars(project_id, region, name, "dev", dev_infra_labels(name), cluster)
