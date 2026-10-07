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

# Sourced for build_and_push_images, by locust/build_and_push_images.sh, and
# for pin_image. The paths in an images.json are relative to the repo root,
# which the caller has to be in.
#
# USE_CLOUD_BUILD=1 builds and pushes on Cloud Build, for a host without a
# Docker daemon. SKIP_DOCKER_LOGIN=1 leaves the registry credentials of a local
# build to the environment.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "Error: $0 is sourced, not run." >&2
  exit 1
fi

# Target platform must match the cluster's nodes, not the build host.
PLATFORM="${LOCUST_IMAGE_PLATFORM:-linux/amd64}"
USE_CLOUD_BUILD="${USE_CLOUD_BUILD:-0}"

# Registry hosts docker_login has already logged in to.
LOGGED_IN_HOSTS=" "

# require_image_vars stops when any of the named image variables is unset.
require_image_vars() {
  local var
  for var in "$@"; do
    if [[ -z "${!var:-}" ]]; then
      echo "Error: ${var} must be set; .terraform-env.sh exports it, and .env.sh" >&2
      echo "sources that. An .env.sh not built from env.sh.example has to set it itself." >&2
      exit 1
    fi
  done
}

# Authenticate to the registry host an image names, rather than assuming
# `gcloud auth configure-docker <host>` has been run for it. Docker consults
# ~/.docker/config.json by host, so a repository in a project or a location
# this machine has never pushed to fails as "Unauthenticated request" even
# though the account has artifactregistry.writer.
docker_login() {
  local host="${1%%/*}" token
  if [[ "${SKIP_DOCKER_LOGIN:-0}" == "1" || "${LOGGED_IN_HOSTS}" == *" ${host} "* ]]; then
    return 0
  fi
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
  LOGGED_IN_HOSTS+="${host} "
}

# For an image of an images.json, with its .tag and .platform filled in: its
# docker build arguments, and the paths that build reads.
IMAGES_JQ='
def build_args:
  ["build", "--platform", .platform, "-t", .tag, "-f", .dockerfile]
  + (.build_contexts // {} | to_entries | map("--build-context", "\(.key)=\(.value)"))
  + (.build_args // {} | to_entries | map("--build-arg", "\(.key)=\(.value)"))
  + (if .target then ["--target", .target] else [] end)
  + (.args // [])
  + [.context];
def sources:
  . as $i
  | [$i.context]
  + (if $i.dockerfile | startswith($i.context + "/") then [] else [$i.dockerfile] end)
  + ($i.build_contexts // {} | [.[]]);
'

# build_and_push_images DIR... builds and pushes the images listed in each
# DIR/images.json, each image with the variable that holds its tag.
build_and_push_images() {
  local dir files=() images vars var named=() tags
  if [[ "$#" -eq 0 ]]; then
    echo "Error: name a directory whose images.json to build." >&2
    exit 1
  fi
  for dir in "$@"; do
    if [[ ! -f "${dir}/images.json" ]]; then
      echo "Error: no images.json in ${dir}." >&2
      exit 1
    fi
    files+=("${dir}/images.json")
  done
  images="$(jq -cs 'add' "${files[@]}")"

  mapfile -t vars < <(jq -r '.[].image_var' <<<"${images}")
  require_image_vars "${vars[@]}"
  for var in "${vars[@]}"; do
    named+=(--arg "${var}" "${!var}")
  done
  tags="$(jq -cn '$ARGS.named' "${named[@]}")"
  images="$(jq -c --argjson tags "${tags}" --arg platform "${PLATFORM}" \
    'map(.tag = $tags[.image_var] | .platform //= $platform)' <<<"${images}")"

  if [[ "${USE_CLOUD_BUILD}" == "1" ]]; then
    cloud_build_and_push_images "${images}"
  else
    docker_build_and_push_images "${images}"
  fi
}

# Builds and pushes each image of the JSON array $1 in turn.
docker_build_and_push_images() {
  local image_list image tag args_sh argv first=1
  mapfile -t image_list < <(jq -c '.[]' <<<"$1")
  for image in "${image_list[@]}"; do
    tag="$(jq -r '.tag' <<<"${image}")"
    # @sh quotes each argument for eval, whatever characters it holds.
    args_sh="$(jq -r "${IMAGES_JQ}"'build_args | @sh' <<<"${image}")"
    eval "argv=(${args_sh})"

    [[ "${first}" -eq 1 ]] || echo
    first=0
    docker_login "${tag}"
    echo "Building Docker image: ${tag} (platform: $(jq -r '.platform' <<<"${image}"))"
    docker "${argv[@]}"

    echo "Pushing ${tag}..."
    docker push "${tag}"
  done
}

# Runs the same docker builds as one Cloud Build, which pushes with its own
# service account. The source uploaded is only the paths the builds read, at
# their repo-relative paths, so the build arguments are the local ones
# unchanged.
cloud_build_and_push_images() {
  local images="$1" sources_sh sources dir status=0
  if [[ -z "${PROJECT_ID:-}" ]]; then
    echo "Error: PROJECT_ID must be set to build on Cloud Build." >&2
    exit 1
  fi
  sources_sh="$(jq -r "${IMAGES_JQ}"'[.[] | sources[]] | unique | @sh' <<<"${images}")"
  eval "sources=(${sources_sh})"

  dir="$(mktemp -d)"
  tar -czf "${dir}/source.tgz" --exclude-vcs "${sources[@]}"
  # The builds start together, and the images are pushed only once all of them
  # have succeeded. --build-context needs BuildKit, which docker 24 does not
  # default to.
  jq "${IMAGES_JQ}"'{
    steps: map({
      id: .image_var,
      name: "gcr.io/cloud-builders/docker:24.0.9",
      env: ["DOCKER_BUILDKIT=1"],
      args: build_args,
      waitFor: ["-"]
    }),
    images: map(.tag),
    timeout: "3600s",
    options: {machineType: "E2_HIGHCPU_8"}
  }' <<<"${images}" >"${dir}/cloudbuild.json"

  echo "Building on Cloud Build:"
  jq -r '.[] | "  \(.tag) (platform: \(.platform))"' <<<"${images}"
  # gcloud streams the build log, but Python holds it back until the build ends
  # when stdout is not a terminal.
  PYTHONUNBUFFERED=1 gcloud builds submit "${dir}/source.tgz" \
    --config="${dir}/cloudbuild.json" --project="${PROJECT_ID}" || status=$?
  rm -rf "${dir}"
  return "${status}"
}

# pin_image IMAGE prints IMAGE pinned to the digest its registry serves for its
# tag right now, as repo@sha256:..., so what reads the image and what runs it
# are the same bits even if the tag moves in between. The images are all in
# Artifact Registry, so gcloud can answer without Docker.
pin_image() {
  local image="$1" digest repo
  if [[ -z "${image}" ]]; then
    echo "Error: no image to pin; the image names come from .terraform-env.sh," >&2
    echo "which .env.sh sources." >&2
    return 1
  fi
  # An unpushed tag and a registry that refused the request both leave this
  # empty.
  digest="$(gcloud artifacts docker images describe "${image}" \
    --format='value(image_summary.digest)' 2>/dev/null || true)"
  if [[ "${digest}" != sha256:* ]]; then
    echo "Error: cannot resolve ${image} to a digest. Push it first with" >&2
    echo "benchmarking/locust/build_and_push_images.sh, and check that the active" >&2
    echo "gcloud account can read the repository." >&2
    return 1
  fi
  # The configured path rather than the registry's spelling of it. Only the
  # last path segment can carry a tag, which leaves a registry port alone.
  repo="${image%@*}"
  if [[ "${repo##*/}" == *:* ]]; then
    repo="${repo%:*}"
  fi
  echo "${repo}@${digest}"
}
