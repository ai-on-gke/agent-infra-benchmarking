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
#   --deploy:  workloads/deploy.sh --deploy -> locust/build_and_push.sh -> locust/deploy.sh --deploy
#   --delete:  locust/deploy.sh --delete -> workloads/deploy.sh --delete (reverse order)
#
# Worker count and sandbox class are forwarded to workloads/deploy.sh.

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
BENCHMARKING_DIR="${ROOT}/benchmarking"

WORKER_COUNT=1
SANDBOX_CLASS=gvisor
SKIP_BUILD=0
OTLP_ENDPOINT=""
# Empty keeps the default in workloads/deploy.sh (256Mi, the microvm minimum).
ACTOR_MEMORY=""
WAIT_TIMEOUT_SECS=""
# The locust user class, the workload a TTFIUser drives and the template it is
# measured on, all forwarded to locust/deploy.sh, which holds the same
# defaults.
USER_CLASS=ttfi
WORKLOAD_NAME=glutton
WORKLOAD_TEMPLATE=glutton

usage() {
  echo "Usage: $0 [options]"
  echo ""
  echo "Options:"
  echo "  --deploy                Deploy workloads, build/push locust image, then deploy locust"
  echo "  --delete                Delete locust and then workloads"
  echo "  --worker-count N        Number of WorkerPool replicas (default: 1)"
  echo "  --sandbox-class CLASS   Sandbox runtime for the WorkerPool: gvisor | microvm (default: gvisor)."
  echo "                          microvm requires dependency-repos/substrate/hack/install-microvm-deps.sh"
  echo "  --otlp-endpoint URL     Forwarded to workloads/deploy.sh. The address to which an"
  echo "                          instrumented actor container sends telemetry."
  echo "  --actor-memory SIZE     Forwarded to workloads/deploy.sh. Memory limit for the"
  echo "                          benchmark ActorTemplates (default: 256Mi, the microvm minimum)."
  echo "  --wait-timeout SECONDS  Forwarded to workloads/deploy.sh. The timeout in seconds for"
  echo "                          waiting for the ateom workers to be ready (default: 300)"
  echo "  --user-class NAME       Forwarded to locust/deploy.sh. Locust user class, lowercase;"
  echo "                          runs tests/NAME.py (default: ttfi)"
  echo "  --workload-name NAME    Forwarded to locust/deploy.sh. The workload a TTFIUser drives:"
  echo "                          glutton | openclaw (default: glutton). openclaw is not in the"
  echo "                          default WORKLOAD_TEMPLATES set, so deploy it with"
  echo "                          WORKLOAD_TEMPLATES=\"... openclaw\"."
  echo "  --workload-template NAME"
  echo "                          Forwarded to locust/deploy.sh. The ActorTemplate the workload"
  echo "                          is measured on (default: glutton). Has to be one the workload"
  echo "                          above can address, so set it whenever --workload-name changes."
  echo "  --skip-build            Skip locust image build/push (use the existing :latest image)"
  echo "  -h|--help               Show this help message"
  echo ""
  echo "Environment:"
  echo "  PROJECT_ID, BUCKET_NAME, etc. are read from .env.sh by the"
  echo "  scripts this wrapper invokes."
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
    --sandbox-class) shift; SANDBOX_CLASS="$1" ;;
    --sandbox-class=*) SANDBOX_CLASS="${1#*=}" ;;
    --otlp-endpoint) shift; OTLP_ENDPOINT="$1" ;;
    --otlp-endpoint=*) OTLP_ENDPOINT="${1#*=}" ;;
    --actor-memory) shift; ACTOR_MEMORY="$1" ;;
    --actor-memory=*) ACTOR_MEMORY="${1#*=}" ;;
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

case "${SANDBOX_CLASS}" in
  gvisor|microvm) ;;
  *)
    echo "Error: --sandbox-class must be gvisor or microvm, got '${SANDBOX_CLASS}'" >&2
    exit 1
    ;;
esac

if [[ -n "${WAIT_TIMEOUT_SECS}" ]] && ! [[ "${WAIT_TIMEOUT_SECS}" =~ ^[0-9]+$ ]]; then
  echo "Error: --wait-timeout must be a whole number of seconds like 300, got '${WAIT_TIMEOUT_SECS}'" >&2
  exit 1
fi

if [[ "${action}" == "deploy" ]]; then
  if [[ "${SKIP_BUILD}" -eq 0 ]]; then
    echo
    echo "=== Building and pushing locust image ==="
    "${BENCHMARKING_DIR}/locust/build_and_push.sh"
  else
    echo
    echo "=== Skipping locust image build/push (--skip-build) ==="
  fi

  echo "=== Deploying benchmark workloads (worker_count=${WORKER_COUNT}, sandbox_class=${SANDBOX_CLASS}) ==="
  # An empty OTLP_ENDPOINT must not become an empty --otlp-endpoint argument,
  # which would overwrite the default in workloads/deploy.sh with an empty
  # string and send the actor telemetry nowhere.
  workload_args=(--deploy --worker-count "${WORKER_COUNT}" --sandbox-class "${SANDBOX_CLASS}")
  if [[ -n "${OTLP_ENDPOINT}" ]]; then
    workload_args+=(--otlp-endpoint "${OTLP_ENDPOINT}")
  fi
  if [[ -n "${ACTOR_MEMORY}" ]]; then
    workload_args+=(--actor-memory "${ACTOR_MEMORY}")
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
