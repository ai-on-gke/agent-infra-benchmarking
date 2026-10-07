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

# Runs a small glutton case through run_benchmark.sh to check the whole path
# end to end. Any option is passed on to run_benchmark.sh:
#
#   ./benchmarking/test_run_benchmark.sh --no-build

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

TEST_CASE='{
  "type": "locust",
  "worker-count": 10,
  "sandbox-class": "gvisor",
  "master": {
    "run-time": "1m",
    "users": 5,
    "spawn-rate": 5
  },
  "worker": {
    "workload-name": "glutton",
    "workload-template": "glutton",
    "resume-mode": "explicit"
  }
}'

exec benchmarking/run_benchmark.sh --test-case-name smoke-glutton --test-case "${TEST_CASE}" "$@"
