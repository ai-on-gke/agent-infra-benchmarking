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
# --backend selects which of two unrelated sandbox control planes the whole
# stack targets: substrate (default, for backward compatibility) or
# agent-sandbox. It is forwarded to every stage below; the backend-specific
# options (--sandbox-class/--actor-memory/--otlp-endpoint for substrate,
# --runtime-class/--probe-memory for agent-sandbox) are ignored by
# workloads/deploy.sh when the other backend is selected.

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
BENCHMARKING_DIR="${ROOT}/benchmarking"

BACKEND="substrate"
WORKER_COUNT=1
SKIP_BUILD=0
WAIT_TIMEOUT_SECS=""

# substrate-only, forwarded to workloads/deploy.sh.
SANDBOX_CLASS=gvisor
OTLP_ENDPOINT=""
# Empty keeps the default in workloads/deploy.sh (256Mi, the microvm minimum).
ACTOR_MEMORY=""

# agent-sandbox-only, forwarded to workloads/deploy.sh.
RUNTIME_CLASS=""
PROBE_MEMORY=""

# The locust user class, the workload a RunWorkloadUser drives and the
# template it is measured on, all forwarded to locust/deploy.sh. Left unset
# here so that script's own per-backend defaults apply (glutton for
# substrate, ping for agent-sandbox) unless overridden.
USER_CLASS=runworkload
WORKLOAD_NAME=""
WORKLOAD_TEMPLATE=""

usage() {
  echo "Usage: $0 [options]"
  echo ""
  echo "Options:"
  echo "  --deploy                Deploy workloads, build/push images, then deploy locust"
  echo "  --delete                Delete locust and then workloads"
  echo "  --backend NAME          substrate (default) | agent-sandbox"
  echo "  --worker-count N        Pool size forwarded to workloads/deploy.sh (default: 1)"
  echo "  --wait-timeout SECONDS  Forwarded to workloads/deploy.sh. Timeout waiting for the"
  echo "                          pool to be ready (default: 300)"
  echo "  --user-class NAME       Forwarded to locust/deploy.sh. Locust user class, lowercase;"
  echo "                          runs tests/NAME.py (default: runworkload)"
  echo "  --workload-name NAME    Forwarded to locust/deploy.sh. The workload a RunWorkloadUser"
  echo "                          drives. Default: the chosen --backend's own (glutton for"
  echo "                          substrate, ping for agent-sandbox)."
  echo "  --workload-template NAME"
  echo "                          Forwarded to locust/deploy.sh. The ActorTemplate/SandboxTemplate"
  echo "                          the workload is measured on. Has to be one the workload above"
  echo "                          can address, so set it whenever --workload-name changes."
  echo "  --skip-build            Skip image build/push (use existing :latest images)"
  echo ""
  echo "substrate-only options (forwarded to workloads/deploy.sh):"
  echo "  --sandbox-class CLASS   Sandbox runtime for the WorkerPool: gvisor | microvm (default: gvisor)."
  echo "                          microvm requires dependency-repos/substrate/hack/install-microvm-deps.sh"
  echo "  --otlp-endpoint URL     The address to which an instrumented actor container sends telemetry."
  echo "  --actor-memory SIZE     Memory limit for the benchmark ActorTemplates (default: 256Mi,"
  echo "                          the microvm minimum)."
  echo ""
  echo "agent-sandbox-only options (forwarded to workloads/deploy.sh):"
  echo "  --runtime-class NAME    RuntimeClass for the probe SandboxTemplate (default: unset, i.e. runc)."
  echo "  --probe-memory SIZE     Memory limit for the probe container (default: 64Mi)."
  echo ""
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
    --backend) shift; BACKEND="$1" ;;
    --backend=*) BACKEND="${1#*=}" ;;
    --worker-count) shift; WORKER_COUNT="$1" ;;
    --worker-count=*) WORKER_COUNT="${1#*=}" ;;
    --sandbox-class) shift; SANDBOX_CLASS="$1" ;;
    --sandbox-class=*) SANDBOX_CLASS="${1#*=}" ;;
    --otlp-endpoint) shift; OTLP_ENDPOINT="$1" ;;
    --otlp-endpoint=*) OTLP_ENDPOINT="${1#*=}" ;;
    --actor-memory) shift; ACTOR_MEMORY="$1" ;;
    --actor-memory=*) ACTOR_MEMORY="${1#*=}" ;;
    --runtime-class) shift; RUNTIME_CLASS="$1" ;;
    --runtime-class=*) RUNTIME_CLASS="${1#*=}" ;;
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

case "${BACKEND}" in
  substrate|agent-sandbox) ;;
  *)
    echo "Error: --backend must be substrate or agent-sandbox, got '${BACKEND}'" >&2
    exit 1
    ;;
esac

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
    echo "=== Building and pushing images ==="
    "${BENCHMARKING_DIR}/locust/build_and_push.sh"
  else
    echo
    echo "=== Skipping image build/push (--skip-build) ==="
  fi

  echo "=== Deploying benchmark workloads (backend=${BACKEND}, worker_count=${WORKER_COUNT}) ==="
  # An empty *_ENDPOINT/etc. must not become an empty flag argument, which
  # would overwrite workloads/deploy.sh's own default with an empty string.
  workload_args=(--deploy --backend "${BACKEND}" --worker-count "${WORKER_COUNT}")
  if [[ "${BACKEND}" == "agent-sandbox" ]]; then
    if [[ -n "${RUNTIME_CLASS}" ]]; then
      workload_args+=(--runtime-class "${RUNTIME_CLASS}")
    fi
    if [[ -n "${PROBE_MEMORY}" ]]; then
      workload_args+=(--probe-memory "${PROBE_MEMORY}")
    fi
  else
    workload_args+=(--sandbox-class "${SANDBOX_CLASS}")
    if [[ -n "${OTLP_ENDPOINT}" ]]; then
      workload_args+=(--otlp-endpoint "${OTLP_ENDPOINT}")
    fi
    if [[ -n "${ACTOR_MEMORY}" ]]; then
      workload_args+=(--actor-memory "${ACTOR_MEMORY}")
    fi
  fi
  if [[ -n "${WAIT_TIMEOUT_SECS}" ]]; then
    workload_args+=(--wait-timeout "${WAIT_TIMEOUT_SECS}")
  fi
  "${BENCHMARKING_DIR}/workloads/deploy.sh" "${workload_args[@]}"

  echo
  echo "=== Deploying locust ==="
  locust_args=(--deploy --backend "${BACKEND}" --user-class "${USER_CLASS}")
  if [[ -n "${WORKLOAD_NAME}" ]]; then
    locust_args+=(--workload-name "${WORKLOAD_NAME}")
  fi
  if [[ -n "${WORKLOAD_TEMPLATE}" ]]; then
    locust_args+=(--workload-template "${WORKLOAD_TEMPLATE}")
  fi
  "${BENCHMARKING_DIR}/locust/deploy.sh" "${locust_args[@]}"
elif [[ "${action}" == "delete" ]]; then
  echo "=== Deleting locust ==="
  "${BENCHMARKING_DIR}/locust/deploy.sh" --delete --backend "${BACKEND}"

  echo
  echo "=== Deleting benchmark workloads ==="
  "${BENCHMARKING_DIR}/workloads/deploy.sh" --delete --backend "${BACKEND}"
else
  usage
  exit 1
fi
