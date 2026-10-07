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

# Runs one test case with the benchmarking/<type>/run_benchmark.sh that the
# case's "type" names, passing every option on unchanged:
#
#   ./benchmarking/run_benchmark.sh --test-case-name glutton --test-case '{
#     "type": "locust", "worker-count": 1, "sandbox-class": "gvisor",
#     "master": {"run-time": "3m", "users": 100},
#     "worker": {"workload-name": "glutton", "workload-template": "glutton"}}'

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

usage() {
  echo "Usage: $0 --test-case-name NAME --test-case JSON [options]"
  echo ""
  echo "Runs benchmarking/TYPE/run_benchmark.sh, where TYPE is the case's \"type\" key,"
  echo "with every option given here. Types:"
  local script
  for script in benchmarking/*/run_benchmark.sh; do
    script="${script#benchmarking/}"
    echo "  ${script%/run_benchmark.sh}"
  done
  echo ""
  echo "For a type's options: benchmarking/TYPE/run_benchmark.sh --help"
}

die() {
  echo "Error: $*" >&2
  exit 1
}

TEST_CASE_JSON=""
args=("$@")
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --test-case) shift; TEST_CASE_JSON="${1:-}" ;;
    --test-case=*) TEST_CASE_JSON="${1#*=}" ;;
    -h|--help) usage; exit 0 ;;
  esac
  shift || true
done

command -v jq &>/dev/null || die "jq is required to read the test case's type."
jq -e 'type == "object"' <<<"${TEST_CASE_JSON:-null}" &>/dev/null ||
  die "--test-case must be a JSON object, got '${TEST_CASE_JSON}'."

TYPE="$(jq -r '.type // empty' <<<"${TEST_CASE_JSON}")"
[[ -n "${TYPE}" ]] || die "the test case has no \`type\`, e.g. \"type\": \"locust\"."
# Also keeps the type from naming a path outside benchmarking/.
[[ "${TYPE}" =~ ^[a-z0-9][-a-z0-9_]*$ ]] || die "\`type\` '${TYPE}' is not a benchmark type."
SCRIPT="benchmarking/${TYPE}/run_benchmark.sh"
[[ -x "${SCRIPT}" ]] || die "no ${SCRIPT} for \`type\` '${TYPE}'."

exec "${SCRIPT}" "${args[@]}"
