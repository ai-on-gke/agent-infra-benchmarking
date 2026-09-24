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

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

# Source the environment variables if configured
if [[ -f .env.sh ]]; then
  source .env.sh
fi

if [ -z "${PROJECT_ID:-}" ]; then
  echo "Error: PROJECT_ID environment variable must be set." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BACKEND="substrate"

# Substituted into the boomer container's --user-class argument and the master's -f.
BENCHMARK_USER_CLASS=runworkload

# Substituted into the boomer container's --workload-name argument. Read only
# when the user class is runworkload; the other classes ignore the flag. Not
# validated here: the registry that defines the valid names is compiled into
# the image being deployed, which is not necessarily built from this working
# tree, so a check against the local sources could reject a name the image
# accepts. An unknown name is fatal in the worker, which logs the names it
# does know. Left unset here and defaulted per --backend below, so a caller
# who only sets --backend gets that backend's own default workload.
BENCHMARK_WORKLOAD_NAME=""

# Substituted into the boomer container's --workload-template argument,
# which overrides the ActorTemplate/SandboxTemplate the workload would
# otherwise measure. It has to name a template the workload above can
# actually address, so the two move together: changing --workload-name
# without this one measures the new workload against the old workload's
# template. An empty value is valid and hands the choice back to the
# workload, which is the only place that knows what it can address.
BENCHMARK_WORKLOAD_TEMPLATE=""

# Optional: an OTLP gRPC endpoint reachable from the cluster. Only used by
# the --backend agent-sandbox manifest (substrate's own points at the
# GKE-managed collector directly). Left blank by default — the boomer worker
# still reports every window through Prometheus and the locust stats CSV
# without it.
OTEL_COLLECTOR_ENDPOINT="${OTEL_COLLECTOR_ENDPOINT:-}"

usage() {
  echo "Usage: $0 [options]"
  echo ""
  echo "Options:"
  echo "  --deploy              Deploy the locust workers"
  echo "  --delete              Delete the locust workers"
  echo "  --backend NAME        substrate (default) | agent-sandbox"
  echo "  --user-class NAME     Locust user class, lowercase; runs tests/NAME.py (default: runworkload)"
  echo "  --workload-name NAME  Workload a RunWorkloadUser drives. substrate: glutton (default) | openclaw."
  echo "                        agent-sandbox: ping (default, the only one shipped)."
  echo "                        Its ActorTemplate/SandboxTemplate has to be deployed already."
  echo "  --workload-template NAME"
  echo "                        ActorTemplate/SandboxTemplate the workload is measured on"
  echo "                        (default: the workload's own name). Has to be one --workload-name"
  echo "                        can address, so set it whenever that changes; an empty value"
  echo "                        leaves the choice to the workload."
  echo "  -h|--help             Show this help message"
}

deploy() {
  local manifest="$1"
  # The locust manifest targets the `benchmarking` namespace (so prometheus
  # can scrape it when that stack is installed). Ensure it exists either way —
  # benchmarking/monitoring.yaml is otherwise optional.
  echo "Ensuring benchmarking namespace exists..."
  kubectl create namespace benchmarking --dry-run=client -o yaml | kubectl apply -f -
  echo "Deploying Locust load (backend=${BACKEND}, PROJECT_ID=${PROJECT_ID}, user_class=${BENCHMARK_USER_CLASS}, workload=${BENCHMARK_WORKLOAD_NAME}, template=${BENCHMARK_WORKLOAD_TEMPLATE:-<the workload default>})..."
  envsubst < "${manifest}" | kubectl apply -f -
}

delete() {
  local manifest="$1"
  echo "Deleting Locust load (backend=${BACKEND})..."
  envsubst < "${manifest}" | kubectl delete --ignore-not-found -f -
}

# require_workload_name rejects an explicitly empty --workload-name. It has to
# run at parse time, while an unset flag is still distinguishable from one
# passed as "": by the time the per-backend `:=` defaults below run, an empty
# value has already been filled with the backend default, so a check there
# cannot tell "caller passed nothing" (default is correct) from "caller passed
# ''" (an error). An empty name would substitute as "--workload-name=", which
# the worker reads as unset and silently answers with its own default -- the
# measured-the-wrong-workload result the Go side refuses for an unknown name.
require_workload_name() {
  if [[ -z "${BENCHMARK_WORKLOAD_NAME}" ]]; then
    echo "Error: --workload-name cannot be empty" >&2
    exit 1
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
    --backend) shift; BACKEND="$1" ;;
    --backend=*) BACKEND="${1#*=}" ;;
    --user-class) shift; BENCHMARK_USER_CLASS="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" ;;
    --user-class=*) BENCHMARK_USER_CLASS="$(printf '%s' "${1#*=}" | tr '[:upper:]' '[:lower:]')" ;;
    --workload-name) shift; BENCHMARK_WORKLOAD_NAME="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"; require_workload_name ;;
    --workload-name=*) BENCHMARK_WORKLOAD_NAME="$(printf '%s' "${1#*=}" | tr '[:upper:]' '[:lower:]')"; require_workload_name ;;
    --workload-template) shift; BENCHMARK_WORKLOAD_TEMPLATE="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" ;;
    --workload-template=*) BENCHMARK_WORKLOAD_TEMPLATE="$(printf '%s' "${1#*=}" | tr '[:upper:]' '[:lower:]')" ;;
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

if [[ ! -f "${SCRIPT_DIR}/tests/${BENCHMARK_USER_CLASS}.py" ]]; then
  echo "Error: no tests/${BENCHMARK_USER_CLASS}.py; --user-class must name a test file" >&2
  exit 1
fi

MANIFEST="${SCRIPT_DIR}/manifests/locust.yaml"
if [[ "${BACKEND}" == "agent-sandbox" ]]; then
  MANIFEST="${SCRIPT_DIR}/manifests/locust-agent-sandbox.yaml"
  # Must match the LOCUST_IMAGE default in locust/build_and_push.sh: that
  # script builds and pushes this one image, and the agent-sandbox manifest
  # pulls it by this path. A divergent default here is an ImagePullBackOff
  # (the built image lives at the other path). The substrate manifest hardcodes
  # the same path.
  LOCUST_IMAGE="${LOCUST_IMAGE:-us-docker.pkg.dev/${PROJECT_ID}/gcr.io/ate-images/locust-test:latest}"
  : "${BENCHMARK_WORKLOAD_NAME:=ping}"
  : "${BENCHMARK_WORKLOAD_TEMPLATE:=ping}"
else
  : "${BENCHMARK_WORKLOAD_NAME:=glutton}"
  : "${BENCHMARK_WORKLOAD_TEMPLATE:=glutton}"
fi

# BENCHMARK_WORKLOAD_NAME is now guaranteed non-empty: require_workload_name
# rejected an explicit "" at parse time, and the per-backend `:=` above filled
# the unset case with the backend default. The template gets no such check:
# empty is how it asks the workload to choose its own.
export BENCHMARK_USER_CLASS BENCHMARK_WORKLOAD_NAME BENCHMARK_WORKLOAD_TEMPLATE
export LOCUST_IMAGE OTEL_COLLECTOR_ENDPOINT

if [[ "${action}" == "deploy" ]]; then
  deploy "${MANIFEST}"
elif [[ "${action}" == "delete" ]]; then
  delete "${MANIFEST}"
fi
