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

# Builds and pushes the images deploy_workloads.sh puts in the workloads' ActorTemplates
# and beside them: the openclaw actor and mock-llm.

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

if [[ -f .env.sh ]]; then
  source .env.sh
fi

source benchmarking/images/lib.sh

require_image_vars OPENCLAW_IMAGE MOCK_LLM_IMAGE

# The actor the openclaw workload drives. Its context is its own
# directory: the Dockerfile installs openclaw from npm and copies one script,
# so it needs nothing from the rest of the repo.
build_and_push "${OPENCLAW_IMAGE}" demos/openclaw/Dockerfile demos/openclaw

echo
build_and_push "${MOCK_LLM_IMAGE}" demos/mock-llm/Dockerfile demos/mock-llm
