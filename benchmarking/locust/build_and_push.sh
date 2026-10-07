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

# Builds and pushes the locust master and boomer worker images.

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

if [[ -f .env.sh ]]; then
  source .env.sh
fi

source benchmarking/images/lib.sh

require_image_vars LOCUST_IMAGE BOOMER_IMAGE

build_and_push "${LOCUST_IMAGE}" benchmarking/locust/master-image/Dockerfile benchmarking/locust/master-image

# go.mod replaces the substrate module with the submodule, which the build
# reads as a second context.
echo
build_and_push "${BOOMER_IMAGE}" benchmarking/locust/go/Dockerfile benchmarking/locust/go \
  --build-context substrate=dependency-repos/substrate
