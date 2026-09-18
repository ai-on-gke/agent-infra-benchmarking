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

# The manifest's only substituted image. Unset would envsubst to an empty
# image field, which kubectl rejects without naming the cause.
if [ -z "${LOCUST_IMAGE:-}" ]; then
  echo "Error: LOCUST_IMAGE must be set; .terraform-env.sh exports it, and .env.sh" >&2
  echo "sources that." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${SCRIPT_DIR}/manifests/locust.yaml"

# Substituted into the boomer container's --user-class argument and the master's -f.
BENCHMARK_USER_CLASS=runworkload

# Substituted into the boomer container's --workload-name argument. Read only
# when the user class is runworkload; the other classes ignore the flag. Not
# validated here: the registry that defines the valid names is compiled into
# the image being deployed, which is not necessarily built from this working
# tree, so a check against the local sources could reject a name the image
# accepts. An unknown name is fatal in the worker, which logs the names it
# does know.
BENCHMARK_WORKLOAD_NAME=glutton

# Substituted into the boomer container's --workload-template argument,
# which overrides the ActorTemplate the workload would otherwise measure. It
# has to name a template the workload above can actually address, so the two
# move together: changing --workload-name without this one measures the new
# workload against the old workload's template. An empty value is valid and
# hands the choice back to the workload, which is the only place that knows
# what it can address.
BENCHMARK_WORKLOAD_TEMPLATE="glutton"

usage() {
  echo "Usage: $0 [options]"
  echo ""
  echo "Options:"
  echo "  --deploy              Deploy the locust workers"
  echo "  --delete              Delete the locust workers"
  echo "  --user-class NAME     Locust user class, lowercase; runs tests/NAME.py (default: runworkload)"
  echo "  --workload-name NAME  Workload a RunWorkloadUser drives: glutton | openclaw (default: glutton)."
  echo "                        Its ActorTemplate has to be deployed already — openclaw is not in"
  echo "                        the default WORKLOAD_TEMPLATES set of workloads/deploy.sh."
  echo "  --workload-template NAME"
  echo "                        ActorTemplate the workload is measured on (default: glutton)."
  echo "                        Has to be one --workload-name can address, so set it whenever"
  echo "                        that changes; an empty value leaves the choice to the workload."
  echo "  -h|--help             Show this help message"
}

deploy() {
  # The locust manifest targets the `benchmarking` namespace (so prometheus
  # can scrape it when that stack is installed). Ensure it exists either way —
  # benchmarking/monitoring.yaml is otherwise optional.
  echo "Ensuring benchmarking namespace exists..."
  kubectl create namespace benchmarking --dry-run=client -o yaml | kubectl apply -f -
  echo "Deploying Locust load (image=${LOCUST_IMAGE}, user_class=${BENCHMARK_USER_CLASS}, workload=${BENCHMARK_WORKLOAD_NAME}, template=${BENCHMARK_WORKLOAD_TEMPLATE:-<the workload default>})..."
  envsubst < "${MANIFEST}" | kubectl apply -f -
}

delete() {
  echo "Deleting Locust load..."
  envsubst < "${MANIFEST}" | kubectl delete --ignore-not-found -f -
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
    --user-class) shift; BENCHMARK_USER_CLASS="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" ;;
    --user-class=*) BENCHMARK_USER_CLASS="$(printf '%s' "${1#*=}" | tr '[:upper:]' '[:lower:]')" ;;
    --workload-name) shift; BENCHMARK_WORKLOAD_NAME="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" ;;
    --workload-name=*) BENCHMARK_WORKLOAD_NAME="$(printf '%s' "${1#*=}" | tr '[:upper:]' '[:lower:]')" ;;
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

if [[ ! -f "${SCRIPT_DIR}/tests/${BENCHMARK_USER_CLASS}.py" ]]; then
  echo "Error: no tests/${BENCHMARK_USER_CLASS}.py; --user-class must name a test file" >&2
  exit 1
fi
# An empty value substitutes as "--workload-name=", which the worker reads
# as unset and answers with its own default. That is the silently-measured-the-
# wrong-workload result the Go side refuses to produce for an unknown name, so
# refuse it here too rather than letting a blank argument through. The template
# gets no such check: empty is how it asks the workload to choose.
if [[ -z "${BENCHMARK_WORKLOAD_NAME}" ]]; then
  echo "Error: --workload-name cannot be empty" >&2
  exit 1
fi
export BENCHMARK_USER_CLASS BENCHMARK_WORKLOAD_NAME BENCHMARK_WORKLOAD_TEMPLATE

if [[ "${action}" == "deploy" ]]; then
  deploy
elif [[ "${action}" == "delete" ]]; then
  delete
fi
