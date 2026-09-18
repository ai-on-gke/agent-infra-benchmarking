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

# End-to-end deploy/uninstall of the locust benchmarking stack:
#   --deploy:  locust/build_and_push.sh -> workloads/deploy.sh --deploy -> locust/deploy.sh --deploy
#   --delete:  locust/deploy.sh --delete -> workloads/deploy.sh --delete (reverse order)
#
# Worker count and runtime class are forwarded to workloads/deploy.sh.

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
BENCHMARKING_DIR="${ROOT}/benchmarking"

WORKER_COUNT=1
SANDBOX_RUNTIME_CLASS=""
SKIP_BUILD=0
PROBE_MEMORY=""
WAIT_TIMEOUT_SECS=""
# The locust user class, the workload a RunWorkloadUser drives and the template
# it is measured on, all forwarded to locust/deploy.sh, which holds the same
# defaults.
USER_CLASS=runworkload
WORKLOAD_NAME=ping
WORKLOAD_TEMPLATE=ping

usage() {
  echo "Usage: $0 [options]"
  echo ""
  echo "Options:"
  echo "  --deploy                Deploy workloads, build/push images, then deploy locust"
  echo "  --delete                Delete locust and then workloads"
  echo "  --worker-count N        Number of SandboxWarmPool replicas (default: 1)"
  echo "  --runtime-class NAME    RuntimeClass for the probe SandboxTemplate (default: unset,"
  echo "                          i.e. runc). Must already be registered in the cluster."
  echo "  --probe-memory SIZE     Forwarded to workloads/deploy.sh. Memory limit for the"
  echo "                          probe container (default: 64Mi)."
  echo "  --wait-timeout SECONDS  Forwarded to workloads/deploy.sh. Timeout waiting for the"
  echo "                          warm pool to fill (default: 300)"
  echo "  --user-class NAME       Forwarded to locust/deploy.sh. Locust user class, lowercase;"
  echo "                          runs tests/NAME.py (default: runworkload)"
  echo "  --workload-name NAME    Forwarded to locust/deploy.sh. The workload a RunWorkloadUser"
  echo "                          drives (default: ping)."
  echo "  --workload-template NAME"
  echo "                          Forwarded to locust/deploy.sh. The SandboxTemplate the workload"
  echo "                          is measured on (default: ping). Has to be one the workload"
  echo "                          above can address, so set it whenever --workload-name changes."
  echo "  --skip-build            Skip image build/push (use existing :latest images)"
  echo "  -h|--help               Show this help message"
  echo ""
  echo "Environment:"
  echo "  PROJECT_ID etc. are read from .env.sh by the scripts this wrapper invokes."
}

if [[ "$#" -eq 0 ]]; then
  usage
  exit 1
fi

action=""
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --deploy) action="deploy" ;;
    --delete) action="delete" ;;
    --worker-count) shift; WORKER_COUNT="$1" ;;
    --worker-count=*) WORKER_COUNT="${1#*=}" ;;
    --runtime-class) shift; SANDBOX_RUNTIME_CLASS="$1" ;;
    --runtime-class=*) SANDBOX_RUNTIME_CLASS="${1#*=}" ;;
    --probe-memory) shift; PROBE_MEMORY="$1" ;;
    --probe-memory=*) PROBE_MEMORY="${1#*=}" ;;
    --wait-timeout) shift; WAIT_TIMEOUT_SECS="$1" ;;
    --wait-timeout=*) WAIT_TIMEOUT_SECS="${1#*=}" ;;
    --user-class) shift; USER_CLASS="$1" ;;
    --user-class=*) USER_CLASS="${1#*=}" ;;
    --workload-name) shift; WORKLOAD_NAME="$1" ;;
    --workload-name=*) WORKLOAD_NAME="${1#*=}" ;;
    --workload-template) shift; WORKLOAD_TEMPLATE="$1" ;;
    --workload-template=*) WORKLOAD_TEMPLATE="${1#*=}" ;;
    --skip-build) SKIP_BUILD=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Error: Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

if [[ -n "${WAIT_TIMEOUT_SECS}" ]] && ! [[ "${WAIT_TIMEOUT_SECS}" =~ ^[0-9]+$ ]]; then
  echo "Error: --wait-timeout must be a whole number of seconds like 300, got '${WAIT_TIMEOUT_SECS}'" >&2
  exit 1
fi

if [[ "${action}" == "deploy" ]]; then
  if [[ "${SKIP_BUILD}" -eq 0 ]]; then
    echo
    echo "=== Building and pushing images ==="
    "${BENCHMARKING_DIR}/locust/build_and_push.sh"
  else
    echo
    echo "=== Skipping image build/push (--skip-build) ==="
  fi

  echo "=== Deploying benchmark workloads (worker_count=${WORKER_COUNT}, runtime_class=${SANDBOX_RUNTIME_CLASS:-<default>}) ==="
  workload_args=(--deploy --worker-count "${WORKER_COUNT}")
  if [[ -n "${SANDBOX_RUNTIME_CLASS}" ]]; then
    workload_args+=(--runtime-class "${SANDBOX_RUNTIME_CLASS}")
  fi
  if [[ -n "${PROBE_MEMORY}" ]]; then
    workload_args+=(--probe-memory "${PROBE_MEMORY}")
  fi
  if [[ -n "${WAIT_TIMEOUT_SECS}" ]]; then
    workload_args+=(--wait-timeout "${WAIT_TIMEOUT_SECS}")
  fi
  "${BENCHMARKING_DIR}/workloads/deploy.sh" "${workload_args[@]}"

  echo
  echo "=== Deploying locust ==="
  "${BENCHMARKING_DIR}/locust/deploy.sh" --deploy --user-class "${USER_CLASS}" --workload-name "${WORKLOAD_NAME}" --workload-template "${WORKLOAD_TEMPLATE}"
elif [[ "${action}" == "delete" ]]; then
  echo "=== Deleting locust ==="
  "${BENCHMARKING_DIR}/locust/deploy.sh" --delete

  echo
  echo "=== Deleting benchmark workloads ==="
  "${BENCHMARKING_DIR}/workloads/deploy.sh" --delete
else
  usage
  exit 1
fi
