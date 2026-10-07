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

# Builds and pushes the images listed in the images.json files it is given:
#
#   benchmarking/locust/build_and_push_images.sh \
#     --images-file benchmarking/locust/images.json \
#     --images-file benchmarking/workloads/images.json
#
# USE_CLOUD_BUILD=1 builds them all in one Cloud Build.

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

if [[ -f .env.sh ]]; then
  source .env.sh
fi

source benchmarking/images/lib.sh

IMAGES_FILES=()

usage() {
  echo "Usage: $0 --images-file PATH [--images-file PATH]..."
  echo ""
  echo "  --images-file PATH  An images.json whose images to build. Repeat it to"
  echo "                      build the images of several in one go."
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --images-file) shift; IMAGES_FILES+=("$1") ;;
    --images-file=*) IMAGES_FILES+=("${1#*=}") ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Error: Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

if [[ "${#IMAGES_FILES[@]}" -eq 0 ]]; then
  echo "Error: no --images-file given." >&2
  usage
  exit 1
fi

build_and_push_images "${IMAGES_FILES[@]}"
