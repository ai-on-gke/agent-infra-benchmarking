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

# OPENCLAW_IMAGE comes from the environment rather than from here, so this push
# and whatever resolves that image name one string. An environment predating
# that move does not set it, and nounset would report only the bare variable
# name.
if [ -z "${OPENCLAW_IMAGE:-}" ]; then
  echo "Error: OPENCLAW_IMAGE must be set; .terraform-env.sh exports it, and .env.sh" >&2
  echo "sources that. An .env.sh not built from env.sh.example has to set it itself." >&2
  exit 1
fi

LOCUST_IMAGE="us-docker.pkg.dev/${PROJECT_ID}/gcr.io/ate-images/locust-test:latest"

# Target platform must match the cluster's nodes, not the build host.
PLATFORM="${LOCUST_IMAGE_PLATFORM:-linux/amd64}"

build_and_push() {
  local image="$1" dockerfile="$2" context="$3"

  echo "Building Docker image: ${image} (platform: ${PLATFORM})"
  docker build --platform "${PLATFORM}" -t "${image}" -f "${dockerfile}" "${context}"

  echo "Pushing ${image}..."
  docker push "${image}"
}

# Build context is the monorepo root because the Dockerfile compiles the
# boomer-worker Go binary alongside the Python install (see Dockerfile).
build_and_push "${LOCUST_IMAGE}" benchmarking/locust/Dockerfile .

# The actor the openclaw workload drives. Its context is its own
# directory: the Dockerfile installs openclaw from npm and copies one script,
# so it needs nothing from the rest of the repo.
echo
build_and_push "${OPENCLAW_IMAGE}" demos/openclaw/Dockerfile demos/openclaw

echo "Done!"
