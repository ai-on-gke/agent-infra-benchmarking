#!/usr/bin/env bash

# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Prints the substrate build version as JSON, for Terraform's external data
# source (see main.tf). The value becomes the ate.dev/substrate-version node
# label, and it has to be the same string hack/install-ate.sh stamps on the
# atelet DaemonSet: a worker pod schedules only onto a node whose label matches
# the dataplane's, so a version derived differently here means a WorkerPool
# whose pods never get placed.
#
# Mirrored from ko.BuildVersion in the submodule
# (cmd/ate-setup/internal/ko/ko.go), which ate-setup calls to resolve the same
# value: VERSION wins if set, else `git describe --tags --always --dirty` in the
# substrate root, else "dev". Keep the two in step.

set -o errexit -o nounset -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUBSTRATE_DIR="${SCRIPT_DIR}/../../dependency-repos/substrate"

if [[ ! -d "${SUBSTRATE_DIR}/.git" && ! -f "${SUBSTRATE_DIR}/.git" ]]; then
  echo "Error: substrate submodule not initialized at ${SUBSTRATE_DIR}." >&2
  echo "Run: git submodule update --init dependency-repos/substrate" >&2
  exit 1
fi

version="${VERSION:-}"
if [[ -z "${version}" ]]; then
  version="$(git -C "${SUBSTRATE_DIR}" describe --tags --always --dirty 2>/dev/null || true)"
fi
# ko.BuildVersion's own fallback, for a checkout git cannot describe.
version="${version:-dev}"

# ate-setup refuses a version that is not already a valid label value rather
# than sanitizing it (steps/version.go), so the same string would fail at
# install time. Failing here instead keeps the mismatch out of the cluster, and
# keeps the JSON below from having to quote anything exotic.
if [[ ! "${version}" =~ ^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$ ]]; then
  echo "Error: substrate build version '${version}' is not a valid Kubernetes" >&2
  echo "label value, so ate-setup would reject it too. Pin a label-safe one" >&2
  echo "with the VERSION environment variable." >&2
  exit 1
fi

printf '{"version":"%s"}\n' "${version}"
