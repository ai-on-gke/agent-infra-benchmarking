#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

# ko resolves the pool's ko:// workerImage, and only works inside the submodule.
SUBSTRATE_DIR="${ROOT}/dependency-repos/substrate"
if [[ ! -f "${SUBSTRATE_DIR}/go.mod" ]]; then
  echo "Error: substrate submodule not initialized at ${SUBSTRATE_DIR}." >&2
  echo "Run: git submodule update --init dependency-repos/substrate" >&2
  exit 1
fi

if [[ -f .env.sh ]]; then
  source .env.sh
fi

# Have to match the WorkerPool in the manifest.
POOL_MANIFEST="benchmarking/workloads/manifests/worker-pool.yaml.tmpl"
POOL_NAME=benchmark-ateom
POOL_NAMESPACE=benchmark-workloads

WORKER_COUNT=1
SANDBOX_CLASS="gvisor"
WAIT_TIMEOUT_SECS=300

usage() {
  echo "Usage: $0 --deploy|--delete [options]"
  echo ""
  echo "Options:"
  echo "  --deploy                    Recreate the WorkerPool and wait for its workers to be ready"
  echo "  --delete                    Delete the WorkerPool"
  echo "  --worker-count N            Number of WorkerPool replicas (default: 1)"
  echo "  --sandbox-class CLASS       Sandbox runtime for the WorkerPool: gvisor | microvm (default: gvisor)."
  echo "                              microvm requires dependency-repos/substrate/hack/install-microvm-deps.sh --install to have run."
  echo "  --wait-timeout SECONDS      The timeout in seconds for waiting for the ateom workers to be ready (default: 300)"
  echo "  -h, --help                  Show this help message"
}

# Deleted by name rather than through the manifest, which also carries the
# namespace the workloads live in.
delete() {
  echo "Deleting any previous ${POOL_NAME} WorkerPool..."
  kubectl delete workerpool "${POOL_NAME}" --namespace="${POOL_NAMESPACE}" \
    --ignore-not-found --wait=true
  # The Deployment is garbage-collected after the pool; a new one of the same
  # name would race it.
  kubectl wait --for=delete "deployment/${POOL_NAME}" \
    --namespace="${POOL_NAMESPACE}" --timeout="${WAIT_TIMEOUT_SECS}s" 2>/dev/null || true
}

# Always a new pool, so no run starts on workers the last one warmed.
deploy() {
  delete

  echo "Deploying ${POOL_NAME} WorkerPool (replicas=${WORKER_COUNT}, sandbox_class=${SANDBOX_CLASS})..."
  sed -e "s|\${WORKER_COUNT}|${WORKER_COUNT}|g" \
      -e "s|\${SANDBOX_CLASS}|${SANDBOX_CLASS}|g" \
      "${POOL_MANIFEST}" | (cd "${SUBSTRATE_DIR}" && hack/run-tool.sh ko apply -f -)

  echo "Waiting for the workers to be ready (timeout: ${WAIT_TIMEOUT_SECS}s)..."
  kubectl wait --for=create "deployment/${POOL_NAME}" \
    --namespace="${POOL_NAMESPACE}" --timeout="${WAIT_TIMEOUT_SECS}s"
  if ! kubectl rollout status "deployment/${POOL_NAME}" \
      --namespace="${POOL_NAMESPACE}" --timeout="${WAIT_TIMEOUT_SECS}s"; then
    echo "Error: ${POOL_NAME} was not ready within ${WAIT_TIMEOUT_SECS}s. Pending pods:" >&2
    kubectl get pods --namespace="${POOL_NAMESPACE}" --field-selector=status.phase=Pending >&2 || true
    echo "Scheduling events:" >&2
    kubectl get events --namespace="${POOL_NAMESPACE}" \
      --field-selector reason=FailedScheduling --sort-by=.lastTimestamp >&2 || true
    return 1
  fi
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
    --wait-timeout) shift; WAIT_TIMEOUT_SECS="$1" ;;
    --wait-timeout=*) WAIT_TIMEOUT_SECS="${1#*=}" ;;
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

if ! [[ "${WORKER_COUNT}" =~ ^[0-9]+$ ]]; then
  echo "Error: --worker-count must be a whole number, got '${WORKER_COUNT}'" >&2
  exit 1
fi

if ! [[ "${WAIT_TIMEOUT_SECS}" =~ ^[0-9]+$ ]]; then
  echo "Error: --wait-timeout must be a whole number of seconds like 300, got '${WAIT_TIMEOUT_SECS}'" >&2
  exit 1
fi

case "${action}" in
  deploy) deploy ;;
  delete) delete ;;
  *) usage; exit 1 ;;
esac
