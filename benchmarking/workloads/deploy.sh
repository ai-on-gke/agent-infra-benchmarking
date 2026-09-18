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

# Deploys (or deletes) the benchmark's Namespace, SandboxTemplate, and
# SandboxWarmPool — plain kubectl apply against the agent-sandbox extension
# CRDs. Unlike substrate's workloads/deploy.sh, there is no separate control
# plane to talk to (kubectl-ate, ko image resolution, golden-snapshot wait):
# agent-sandbox's SandboxTemplate/SandboxWarmPool are ordinary namespaced
# CRDs the agent-sandbox controller already watches once it's installed.

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

MANIFEST_DIR="benchmarking/workloads/manifests"

# Source the environment variables if configured.
if [[ -f .env.sh ]]; then
  source .env.sh
fi

if [ -z "${PROJECT_ID:-}" ]; then
  echo "Error: PROJECT_ID environment variable must be set." >&2
  exit 1
fi

# Derived the same way locust/build_and_push.sh derives it, so the two never
# need to agree on a value passed between them: given the same PROJECT_ID,
# both compute the same string. Override by exporting PROBE_IMAGE (e.g. in
# .env.sh) before calling this script, for a custom registry path.
PROBE_IMAGE="${PROBE_IMAGE:-us-docker.pkg.dev/${PROJECT_ID}/agent-infra-benchmarking/ping-probe:latest}"

BENCHMARK_NAMESPACE="benchmarking"
WORKER_COUNT=1
# Unset (runc, the default container runtime) unless the caller asks for
# stronger isolation. Set to "gvisor" or another registered RuntimeClass name
# to run the probe sandboxed, matching --sandbox-class in the substrate flow
# this replaces — though here it names a Kubernetes RuntimeClass directly
# rather than one of substrate's two fixed choices.
SANDBOX_RUNTIME_CLASS=""
# The probe holds no meaningful memory of its own (unlike a real actor
# payload), so this has none of substrate's microvm-floor reasoning behind
# it; raise it only if a custom probe image needs more.
PING_MEMORY="64Mi"
WAIT_TIMEOUT_SECS=300

usage() {
  echo "Usage: $0 [options]"
  echo ""
  echo "Options:"
  echo "  --deploy                     Substitute env vars and deploy the namespace,"
  echo "                                SandboxTemplate, and SandboxWarmPool"
  echo "  --delete                     Delete them"
  echo "  --worker-count N             SandboxWarmPool replicas (default: 1)"
  echo "  --runtime-class NAME         RuntimeClass for the probe SandboxTemplate"
  echo "                                (default: unset, i.e. runc). Must already be"
  echo "                                registered in the cluster (e.g. gvisor)."
  echo "  --probe-memory SIZE          Memory limit for the probe container (default: 64Mi)"
  echo "  --wait-timeout SECONDS       Timeout waiting for the warm pool to fill (default: 300)"
  echo "  -h|--help                    Show this help message"
}

substitute() {
  local manifest="$1"
  local runtime_class_line=""
  if [[ -n "${SANDBOX_RUNTIME_CLASS}" ]]; then
    runtime_class_line="runtimeClassName: ${SANDBOX_RUNTIME_CLASS}"
  fi
  sed -e "s|\${BENCHMARK_NAMESPACE}|${BENCHMARK_NAMESPACE}|g" \
      -e "s|\${WORKER_COUNT}|${WORKER_COUNT}|g" \
      -e "s|\${RUNTIME_CLASS_LINE}|${runtime_class_line}|g" \
      -e "s|\${PING_MEMORY}|${PING_MEMORY}|g" \
      -e "s|\${PROBE_IMAGE}|${PROBE_IMAGE}|g" \
      "${manifest}"
}

deploy() {
  echo "Deploying:"
  echo "  namespace:      ${BENCHMARK_NAMESPACE}"
  echo "  worker_count:   ${WORKER_COUNT}"
  echo "  runtime_class:  ${SANDBOX_RUNTIME_CLASS:-<default>}"
  echo "  probe_memory:   ${PING_MEMORY}"
  echo "  probe_image:    ${PROBE_IMAGE}"

  substitute "${MANIFEST_DIR}/namespace.yaml.tmpl" | kubectl apply -f -
  substitute "${MANIFEST_DIR}/ping-template.yaml.tmpl" | kubectl apply -f -
  substitute "${MANIFEST_DIR}/ping-warmpool.yaml.tmpl" | kubectl apply -f -

  echo "Waiting for the warm pool to fill (timeout: ${WAIT_TIMEOUT_SECS}s)..."
  kubectl wait sandboxwarmpool/ping-warmpool --namespace "${BENCHMARK_NAMESPACE}" \
    --for=jsonpath='{.status.readyReplicas}'="${WORKER_COUNT}" --timeout="${WAIT_TIMEOUT_SECS}s"
}

delete() {
  echo "Deleting..."
  substitute "${MANIFEST_DIR}/ping-warmpool.yaml.tmpl" | kubectl delete --ignore-not-found -f -
  substitute "${MANIFEST_DIR}/ping-template.yaml.tmpl" | kubectl delete --ignore-not-found -f -
  # The namespace is left in place deliberately: deleting it would also
  # cascade-delete the locust Deployment/RBAC that
  # benchmarking/locust/deploy.sh puts in the same namespace, coupling two
  # otherwise-independent teardown steps. Delete it by hand if it should go
  # too: kubectl delete namespace "${BENCHMARK_NAMESPACE}"
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
    --probe-memory) shift; PING_MEMORY="$1" ;;
    --probe-memory=*) PING_MEMORY="${1#*=}" ;;
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

if ! [[ "${WAIT_TIMEOUT_SECS}" =~ ^[0-9]+$ ]]; then
  echo "Error: --wait-timeout must be a whole number of seconds like 300, got '${WAIT_TIMEOUT_SECS}'" >&2
  exit 1
fi

if [[ "${action}" == "deploy" ]]; then
  deploy
elif [[ "${action}" == "delete" ]]; then
  delete
fi
