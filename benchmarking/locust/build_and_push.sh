#!/usr/bin/env bash

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

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

# Source the environment variables if configured
if [[ -f .env.sh ]]; then
  source .env.sh
fi

if [ -z "${PROJECT_ID:-}" ]; then
  echo "Error: PROJECT_ID environment variable must be set." >&2
  exit 1
fi

LOCUST_IMAGE="${LOCUST_IMAGE:-us-docker.pkg.dev/${PROJECT_ID}/agent-infra-benchmarking/locust-runworkload:latest}"
PROBE_IMAGE="${PROBE_IMAGE:-us-docker.pkg.dev/${PROJECT_ID}/agent-infra-benchmarking/ping-probe:latest}"

# Target platform must match the cluster's nodes, not the build host.
PLATFORM="${LOCUST_IMAGE_PLATFORM:-linux/amd64}"

build_and_push() {
  local image="$1" dockerfile="$2" context="$3"

  echo "Building Docker image: ${image} (platform: ${PLATFORM})"
  docker build --platform "${PLATFORM}" -t "${image}" -f "${dockerfile}" "${context}"

  echo "Pushing ${image}..."
  docker push "${image}"
}

# Build context is the module root because the Dockerfile compiles the
# boomer-worker Go binary alongside the Python install (see Dockerfile).
build_and_push "${LOCUST_IMAGE}" benchmarking/locust/Dockerfile .

# The ping workload's target. Its context is its own directory: the
# Dockerfile installs nothing beyond the stdlib, so it needs nothing from the
# rest of the repo.
echo
build_and_push "${PROBE_IMAGE}" benchmarking/workloads/probe/Dockerfile benchmarking/workloads/probe

echo "Done!"
echo "  LOCUST_IMAGE=${LOCUST_IMAGE}"
echo "  PROBE_IMAGE=${PROBE_IMAGE}"
