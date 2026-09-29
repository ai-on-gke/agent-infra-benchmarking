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

# Which set of images to build. substrate needs the locust image (boomer-worker
# baked in) and the openclaw actor; agent-sandbox needs the locust image and the
# ping probe (benchmarking/workloads/probe) its SandboxTemplate runs, but not
# openclaw. LOCUST_IMAGE is shared: the boomer-worker binary drives both backends.
BACKEND="substrate"
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --backend) shift; BACKEND="$1" ;;
    --backend=*) BACKEND="${1#*=}" ;;
    -h|--help)
      echo "Usage: $0 [--backend substrate|agent-sandbox]"
      exit 0
      ;;
    *) echo "Error: Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

case "${BACKEND}" in
  substrate)     REQUIRED_IMAGE_VARS=(LOCUST_IMAGE OPENCLAW_IMAGE MOCK_LLM_IMAGE) ;;
  agent-sandbox) REQUIRED_IMAGE_VARS=(LOCUST_IMAGE PROBE_IMAGE) ;;
  *)
    echo "Error: --backend must be substrate or agent-sandbox, got '${BACKEND}'" >&2
    exit 1
    ;;
esac

if [ -z "${PROJECT_ID:-}" ]; then
  echo "Error: PROJECT_ID environment variable must be set." >&2
  exit 1
fi

# The image names come from the environment rather than from here, so this push
# and whatever resolves them name one string. An environment predating that
# move does not set them, and nounset would report only the bare variable name.
for var in "${REQUIRED_IMAGE_VARS[@]}"; do
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
  local host token var
  # One login per host, so two images in one repository do not log in twice.
  # Only the hosts of the images this backend actually builds.
  for host in $(for var in "${REQUIRED_IMAGE_VARS[@]}"; do printf '%s\n' "${!var%%/*}"; done | sort -u); do
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

build_and_push() {
  local image="$1" dockerfile="$2" context="$3"

  echo "Building Docker image: ${image} (platform: ${PLATFORM})"
  docker build --platform "${PLATFORM}" -t "${image}" -f "${dockerfile}" "${context}"

  echo "Pushing ${image}..."
  docker push "${image}"
}

# Build context is the monorepo root because the Dockerfile compiles the
# boomer-worker Go binary alongside the Python install (see Dockerfile). Both
# backends need this image — it carries the boomer-worker that drives either.
build_and_push "${LOCUST_IMAGE}" benchmarking/locust/Dockerfile .

if [[ "${BACKEND}" == "agent-sandbox" ]]; then
  # The ping probe the agent-sandbox SandboxTemplate runs. Its context is its
  # own directory: the Dockerfile copies one stdlib-only script, so it needs
  # nothing from the rest of the repo.
  echo
  build_and_push "${PROBE_IMAGE}" benchmarking/workloads/probe/Dockerfile benchmarking/workloads/probe
else
  # The actor the openclaw workload drives and its mock LLM. Each context is
  # its own directory: the Dockerfiles install from npm / copy one script, so
  # they need nothing from the rest of the repo.
  echo
  build_and_push "${OPENCLAW_IMAGE}" demos/openclaw/Dockerfile demos/openclaw
  echo
  build_and_push "${MOCK_LLM_IMAGE}" demos/mock-llm/Dockerfile demos/mock-llm
fi
echo "Done!"
