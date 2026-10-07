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

# The image names come from the environment rather than from here, so this
# push and whatever resolves them name one string. An environment predating
# that move does not set them, and nounset would report only the bare variable
# name.
for var in LOCUST_IMAGE BOOMER_IMAGE OPENCLAW_IMAGE MOCK_LLM_IMAGE; do
  if [ -z "${!var:-}" ]; then
    echo "Error: ${var} must be set; .terraform-env.sh exports it, and .env.sh" >&2
    echo "sources that. An .env.sh not built from env.sh.example has to set it itself." >&2
    exit 1
  fi
done

# Target platform must match the cluster's nodes, not the build host.
PLATFORM="${LOCUST_IMAGE_PLATFORM:-linux/amd64}"

# Authenticate to each registry host the images name, rather than assuming
# `gcloud auth configure-docker <host>` has been run for it. Docker consults
# ~/.docker/config.json by host, so a repository in a project or a location
# this machine has never pushed to fails as "Unauthenticated request" even
# though the account has artifactregistry.writer. Set SKIP_DOCKER_LOGIN=1 when
# the environment supplies its own credentials.
docker_login() {
  local host token
  # One login per host, so two images in one repository do not log in twice.
  for host in $(printf '%s\n' "${LOCUST_IMAGE%%/*}" "${BOOMER_IMAGE%%/*}" "${OPENCLAW_IMAGE%%/*}" "${MOCK_LLM_IMAGE%%/*}" | sort -u); do
    # Captured rather than piped straight into docker login: under pipefail a
    # failing gcloud would otherwise surface as docker's complaint about an
    # empty password.
    if ! token="$(gcloud auth print-access-token 2>/dev/null)" || [[ -z "${token}" ]]; then
      echo "Error: cannot get an access token from gcloud for ${host}." >&2
      echo "Run: gcloud auth login" >&2
      exit 1
    fi
    echo "Authenticating to ${host} as $(gcloud config get-value account 2>/dev/null)..."
    printf '%s' "${token}" |
      docker login -u oauth2accesstoken --password-stdin "https://${host}"
  done
}

if [[ "${SKIP_DOCKER_LOGIN:-0}" != "1" ]]; then
  docker_login
fi

# Arguments after the context go to docker build as they are.
build_and_push() {
  local image="$1" dockerfile="$2" context="$3"
  shift 3

  echo "Building Docker image: ${image} (platform: ${PLATFORM})"
  docker build --platform "${PLATFORM}" -t "${image}" -f "${dockerfile}" "$@" "${context}"

  echo "Pushing ${image}..."
  docker push "${image}"
}

build_and_push "${LOCUST_IMAGE}" benchmarking/locust/master-image/Dockerfile benchmarking/locust/master-image

# go.mod replaces the substrate module with the submodule, which the build
# reads as a second context.
echo
build_and_push "${BOOMER_IMAGE}" benchmarking/locust/go/Dockerfile benchmarking/locust/go \
  --build-context substrate=dependency-repos/substrate

# The actor the openclaw workload drives. Its context is its own
# directory: the Dockerfile installs openclaw from npm and copies one script,
# so it needs nothing from the rest of the repo.
echo
build_and_push "${OPENCLAW_IMAGE}" demos/openclaw/Dockerfile demos/openclaw


echo
build_and_push "${MOCK_LLM_IMAGE}" demos/mock-llm/Dockerfile demos/mock-llm
echo "Done!"
