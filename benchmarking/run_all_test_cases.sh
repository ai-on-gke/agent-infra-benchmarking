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

# Runs every case in benchmarking/tests.yaml through run_locust_job.sh, one
# after another, under one run id. Each case's reports go to
# reports/<run-id>/<test-case>/, in the bucket and locally.
#
#   ./benchmarking/run_all_test_cases.sh --follow-logs
#
# A failed case does not stop the ones after it; the exit status says whether
# any failed.

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"
BENCHMARKING_DIR="${ROOT}/benchmarking"

if [[ -f .env.sh ]]; then
  source .env.sh
fi

if [[ -z "${LOCUST_IMAGE:-}" ]]; then
  echo "Error: LOCUST_IMAGE must be set; .terraform-env.sh exports it, and .env.sh" >&2
  echo "sources that." >&2
  exit 1
fi

RUN_ID=""
NO_BUILD=0
# Passed to run_locust_job.sh for every case.
JOB_ARGS=()

usage() {
  echo "Usage: $0 [--run-id NAME] [--no-build] [run_locust_job.sh options]"
  echo ""
  echo "Runs every case in benchmarking/tests.yaml with run_locust_job.sh, in name"
  echo "order, under one run id. The images are built and pushed once, up front."
  echo ""
  echo "  --run-id NAME   Names the run (default: a UTC timestamp). Each case's reports"
  echo "                  go to reports/NAME/<test-case>/."
  echo "  --no-build      Use the images already pushed rather than building them."
  echo ""
  echo "Any other option is passed to run_locust_job.sh for every case, except"
  echo "--test-case, --no-wait and --delete."
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --run-id) shift; RUN_ID="$1" ;;
    --run-id=*) RUN_ID="${1#*=}" ;;
    --no-build) NO_BUILD=1 ;;
    # --no-wait would submit every case at once, and each submit deletes the
    # Job before it.
    --test-case|--test-case=*|--no-wait|--delete)
      echo "Error: $1 does not apply to a run of every case." >&2
      exit 1
      ;;
    -h|--help) usage; exit 0 ;;
    *) JOB_ARGS+=("$1") ;;
  esac
  shift
done

# Must match run_locust_job.sh's default.
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dt%H%M%Sz)}"
# run_locust_job.sh checks this too, but only after the build below.
if ! [[ "${RUN_ID}" =~ ^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$ ]]; then
  echo "Error: --run-id must be 1-63 alphanumerics, dashes, underscores or dots," >&2
  echo "starting and ending with an alphanumeric. Got '${RUN_ID}'" >&2
  exit 1
fi

if [[ "${NO_BUILD}" -eq 0 ]]; then
  echo "=== Building and pushing images ==="
  "${BENCHMARKING_DIR}/locust/build_and_push.sh"
fi

# Listed out of the image, which is where run_locust_job.sh resolves each case
# from, so the list and the cases come from the same tests.yaml.
cases="$(docker run --rm --platform "${LOCUST_IMAGE_PLATFORM:-linux/amd64}" \
  --entrypoint /app/testcase-helper "${LOCUST_IMAGE}" --list)"
CASES=()
while IFS= read -r c; do
  if [[ -n "${c}" ]]; then
    CASES+=("${c}")
  fi
done <<<"${cases}"

echo
echo "=== Run ${RUN_ID}: ${#CASES[@]} test case(s) ==="
printf '  %s\n' "${CASES[@]}"

STATUS=()
for c in "${CASES[@]}"; do
  echo
  echo "=== [${RUN_ID}] ${c} ==="
  # ${arr[@]+...} because bash 3.2, macOS's, treats an empty array as unset.
  if "${BENCHMARKING_DIR}/run_locust_job.sh" --run-id "${RUN_ID}" --test-case "${c}" \
    --no-build ${JOB_ARGS[@]+"${JOB_ARGS[@]}"} \
    --follow-logs ; then
    STATUS+=("ok      ${c}")
  else
    STATUS+=("FAILED  ${c}")
  fi
done

echo
echo "=== Run ${RUN_ID} ==="
printf '  %s\n' "${STATUS[@]}"
if printf '%s\n' "${STATUS[@]}" | grep -q '^FAILED'; then
  exit 1
fi
