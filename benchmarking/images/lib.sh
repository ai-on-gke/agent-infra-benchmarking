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

# Sourced for build_and_push_images, by locust/run_benchmark.sh, and for
# pin_image. The paths in an images.json are relative to the repo root,
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

# For an image of an images.json, with its key as .name: its full name under
# $repo, tagged $tag. With its .ref and .platform filled in as well: its docker
# build arguments, and the paths that build reads.
IMAGES_JQ='
def ref($repo; $tag): "\($repo)/\(.name):\($tag)";
def build_args:
  ["build", "--platform", .platform, "-t", .ref, "-f", .dockerfile]
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

# Every image is named under DOCKER_REPO, so nothing can build or deploy one
# without it.
require_docker_repo() {
  if [[ -z "${DOCKER_REPO:-}" ]]; then
    echo "Error: DOCKER_REPO must be set to name the images; .terraform-env.sh" >&2
    echo "exports it, and .env.sh sources that." >&2
    exit 1
  fi
}

# read_images_files --images-file PATH [--images-file PATH]... prints the
# images of every images.json named as one JSON array, each with its key as
# .name.
read_images_files() {
  local files=() duplicates
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --images-file) shift; files+=("$1") ;;
      --images-file=*) files+=("${1#*=}") ;;
      *)
        echo "Error: Unknown option: $1" >&2
        exit 1
        ;;
    esac
    shift
  done
  if [[ "${#files[@]}" -eq 0 ]]; then
    echo "Error: no --images-file given." >&2
    exit 1
  fi
  duplicates="$(jq -rs '[.[] | keys[]] | group_by(.) | map(select(length > 1)[0]) | join(" ")' "${files[@]}")"
  if [[ -n "${duplicates}" ]]; then
    echo "Error: ${duplicates} named in more than one of ${files[*]}." >&2
    exit 1
  fi
  jq -cs 'add | to_entries | map(.value + {name: .key})' "${files[@]}"
}

# Every image is tagged with the commit the repo is at, so a manifest names the
# image built from the code that rendered it.
image_tag() {
  git rev-parse HEAD
}

# image_ref FILE NAME prints the image NAME of the images.json FILE as
# ${DOCKER_REPO}/NAME:<commit>.
image_ref() {
  local file="$1" name="$2" tag
  require_docker_repo
  tag="$(image_tag)"
  if ! jq -er --arg name "${name}" --arg repo "${DOCKER_REPO}" --arg tag "${tag}" "${IMAGES_JQ}"'
      .[$name] // empty | .name = $name | ref($repo; $tag)' "${file}"; then
    echo "Error: no image ${name} in ${file}." >&2
    exit 1
  fi
}

# build_and_push_images --images-file PATH [--images-file PATH]... builds and
# pushes the images listed in each images.json, under the names image_ref
# gives them.
build_and_push_images() {
  local images tag
  require_docker_repo
  images="$(read_images_files "$@")"
  tag="$(image_tag)"
  images="$(jq -c --arg repo "${DOCKER_REPO}" --arg tag "${tag}" --arg platform "${PLATFORM}" "${IMAGES_JQ}"'
    map(.ref = ref($repo; $tag) | .platform //= $platform)' <<<"${images}")"

  if [[ "${USE_CLOUD_BUILD}" == "1" ]]; then
    cloud_build_and_push_images "${images}"
  else
    docker_build_and_push_images "${images}"
  fi
}

# Builds and pushes each image of the JSON array $1 in turn.
docker_build_and_push_images() {
  local image_list image ref args_sh argv first=1
  mapfile -t image_list < <(jq -c '.[]' <<<"$1")
  for image in "${image_list[@]}"; do
    ref="$(jq -r '.ref' <<<"${image}")"
    # @sh quotes each argument for eval, whatever characters it holds.
    args_sh="$(jq -r "${IMAGES_JQ}"'build_args | @sh' <<<"${image}")"
    eval "argv=(${args_sh})"

    [[ "${first}" -eq 1 ]] || echo
    first=0
    docker_login "${ref}"
    echo "Building Docker image: ${ref} (platform: $(jq -r '.platform' <<<"${image}"))"
    docker "${argv[@]}"

    echo "Pushing ${ref}..."
    docker push "${ref}"
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
      id: .name,
      name: "gcr.io/cloud-builders/docker:24.0.9",
      env: ["DOCKER_BUILDKIT=1"],
      args: build_args,
      waitFor: ["-"]
    }),
    images: map(.ref),
    timeout: "3600s",
    options: {machineType: "E2_HIGHCPU_8"}
  }' <<<"${images}" >"${dir}/cloudbuild.json"

  echo "Building on Cloud Build:"
  jq -r '.[] | "  \(.ref) (platform: \(.platform))"' <<<"${images}"
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
    echo "Error: no image to pin; image_ref names one from its images.json." >&2
    return 1
  fi
  # An unpushed tag and a registry that refused the request both leave this
  # empty.
  digest="$(gcloud artifacts docker images describe "${image}" \
    --format='value(image_summary.digest)' 2>/dev/null || true)"
  if [[ "${digest}" != sha256:* ]]; then
    echo "Error: cannot resolve ${image} to a digest. Push it first, which" >&2
    echo "run_benchmark.sh does without --no-build, and check that the active" >&2
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
