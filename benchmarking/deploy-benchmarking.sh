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

# Stands the benchmarking stack up, and runs nothing. run_locust_job.sh is the
# counterpart that measures what this deployed.
#
# The split matters for a series of runs: redeploying the workloads retakes
# their golden snapshots, so runs either side of it are not comparable. Deploy
# once here, then run as many times as wanted.

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
BENCHMARKING_DIR="${ROOT}/benchmarking"

NAMESPACE=benchmarking

MONITORING_MANIFEST="${BENCHMARKING_DIR}/monitoring/prometheus.yaml"

DEPLOY=0
DELETE=0

# Forwarded to workloads/deploy.sh. Empty keeps that script's own defaults.
WORKER_COUNT=""
SANDBOX_CLASS=""
ACTOR_MEMORY=""
OTLP_ENDPOINT=""
WAIT_TIMEOUT_SECS=""

usage() {
  echo "Usage: $0 --deploy [options] | --delete"
  echo ""
  echo "Deploys the benchmarking stack. Runs no benchmark; run_locust_job.sh does that."
  echo ""
  echo "  --deploy                Build and push the locust and openclaw images, deploy"
  echo "                          Prometheus into the ${NAMESPACE} namespace, then deploy"
  echo "                          the WorkerPool and the benchmark ActorTemplates and wait"
  echo "                          for their golden snapshots. All three, in that order, so"
  echo "                          Prometheus is watching before there is anything to watch."
  echo ""
  echo "With --deploy, forwarded to workloads/deploy.sh:"
  echo "  --worker-count N        Number of WorkerPool replicas (default: 1)"
  echo "  --sandbox-class CLASS   Sandbox runtime for the WorkerPool: gvisor | microvm"
  echo "                          (default: gvisor). microvm requires"
  echo "                          dependency-repos/substrate/hack/install-microvm-deps.sh"
  echo "  --actor-memory SIZE     Memory limit for the benchmark ActorTemplates"
  echo "                          (default: 256Mi, the microvm minimum)"
  echo "  --otlp-endpoint URL     The address an instrumented actor container sends"
  echo "                          telemetry to (default: the ate-otel-config ConfigMap's)"
  echo "  --wait-timeout SECONDS  Timeout waiting for the ateom workers to be ready"
  echo "                          (default: 300)"
  echo ""
  echo "  --delete                Undo all of it: the workloads, then Prometheus — and"
  echo "                          with Prometheus every run it recorded, so save anything"
  echo "                          worth keeping first. A Job is left alone, so a finished"
  echo "                          run keeps its log; run_locust_job.sh --delete removes"
  echo "                          one, and a run still in flight should go first."
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

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --deploy) DEPLOY=1 ;;
    --worker-count) shift; WORKER_COUNT="$1" ;;
    --worker-count=*) WORKER_COUNT="${1#*=}" ;;
    --sandbox-class) shift; SANDBOX_CLASS="$1" ;;
    --sandbox-class=*) SANDBOX_CLASS="${1#*=}" ;;
    --actor-memory) shift; ACTOR_MEMORY="$1" ;;
    --actor-memory=*) ACTOR_MEMORY="${1#*=}" ;;
    --otlp-endpoint) shift; OTLP_ENDPOINT="$1" ;;
    --otlp-endpoint=*) OTLP_ENDPOINT="${1#*=}" ;;
    --wait-timeout) shift; WAIT_TIMEOUT_SECS="$1" ;;
    --wait-timeout=*) WAIT_TIMEOUT_SECS="${1#*=}" ;;
    --delete) DELETE=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Error: Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

if [[ "${DEPLOY}" -eq 1 && "${DELETE}" -eq 1 ]]; then
  echo "Error: --deploy and --delete in one command would undo each other." >&2
  exit 1
fi

if [[ -n "${SANDBOX_CLASS}" ]]; then
  case "${SANDBOX_CLASS}" in
    gvisor|microvm) ;;
    *)
      echo "Error: --sandbox-class must be gvisor or microvm, got '${SANDBOX_CLASS}'" >&2
      exit 1
      ;;
  esac
fi

for pair in "--worker-count:${WORKER_COUNT}" "--wait-timeout:${WAIT_TIMEOUT_SECS}"; do
  if [[ -n "${pair#*:}" ]] && ! [[ "${pair#*:}" =~ ^[0-9]+$ ]]; then
    echo "Error: ${pair%%:*} must be a whole number, got '${pair#*:}'" >&2
    exit 1
  fi
done

# A workload option on a teardown reads as having shaped something, so it is
# named rather than dropped.
if [[ "${DEPLOY}" -eq 0 ]]; then
  for pair in "--worker-count:${WORKER_COUNT}" \
              "--sandbox-class:${SANDBOX_CLASS}" \
              "--actor-memory:${ACTOR_MEMORY}" \
              "--otlp-endpoint:${OTLP_ENDPOINT}" \
              "--wait-timeout:${WAIT_TIMEOUT_SECS}"; do
    if [[ -n "${pair#*:}" ]]; then
      echo "Error: ${pair%%:*} only applies with --deploy, which was not given." >&2
      exit 1
    fi
  done
fi

if [[ "${DELETE}" -eq 1 ]]; then
  echo "=== Deleting benchmark workloads ==="
  "${BENCHMARKING_DIR}/workloads/deploy.sh" --delete

  echo
  echo "=== Deleting Prometheus ==="
  kubectl delete -f "${MONITORING_MANIFEST}" --ignore-not-found
  exit 0
fi

if [[ "${DEPLOY}" -eq 0 ]]; then
  echo "Error: nothing to do. Pass --deploy or --delete." >&2
  exit 1
fi

echo
echo "=== Building and pushing images ==="
"${BENCHMARKING_DIR}/workloads/build_and_push.sh"

# Before the workloads, so Prometheus is already watching while they come up.
echo
echo "=== Deploying Prometheus ==="
if [[ ! -f "${MONITORING_MANIFEST}" ]]; then
  echo "Error: ${MONITORING_MANIFEST} not found." >&2
  exit 1
fi
kubectl apply -f "${MONITORING_MANIFEST}"
kubectl rollout status deployment/prometheus --namespace="${NAMESPACE}" --timeout=300s
echo "Prometheus is up. Reach it with:"
echo "  kubectl port-forward -n ${NAMESPACE} svc/prometheus 9090:9090"

echo
echo "=== Deploying benchmark workloads ==="
# An empty value must not become an empty argument, which would overwrite the
# default in workloads/deploy.sh rather than leave it in place.
workload_args=(--deploy)
[[ -n "${WORKER_COUNT}" ]] && workload_args+=(--worker-count "${WORKER_COUNT}")
[[ -n "${SANDBOX_CLASS}" ]] && workload_args+=(--sandbox-class "${SANDBOX_CLASS}")
[[ -n "${ACTOR_MEMORY}" ]] && workload_args+=(--actor-memory "${ACTOR_MEMORY}")
[[ -n "${OTLP_ENDPOINT}" ]] && workload_args+=(--otlp-endpoint "${OTLP_ENDPOINT}")
[[ -n "${WAIT_TIMEOUT_SECS}" ]] && workload_args+=(--wait-timeout "${WAIT_TIMEOUT_SECS}")
"${BENCHMARKING_DIR}/workloads/deploy.sh" "${workload_args[@]}"

echo
echo "Done. Start a run with:"
echo "  ${BENCHMARKING_DIR#"${ROOT}/"}/run_locust_job.sh --test-case glutton"
