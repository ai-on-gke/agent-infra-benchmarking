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
# The two ways to run the same benchmark. They share the image, the settings
# below and what they measure, and differ in what ends them: the Deployment is
# held open for the web UI, while the Job runs for a stated duration and
# finishes. See manifests/job.yaml for how the latter ends itself.
DEPLOYMENT_MANIFEST="${SCRIPT_DIR}/manifests/deployment.yaml"
JOB_MANIFEST="${SCRIPT_DIR}/manifests/job.yaml"

# Has to match metadata.name in the Job manifest. Fixed rather than generated,
# so a run can be followed, waited on and deleted by name.
JOB_NAME=locust-job

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

# The Job's run length and user count, which the web UI's form carries for the
# Deployment. Substituted into locust's -t and -u, so the duration takes
# locust's own spelling of one ("30s", "5m", "1h30m").
BENCHMARK_DURATION=5m
BENCHMARK_USERS=10

# Names one run in Prometheus, as the run_id label on every series it
# produces. Empty takes a UTC timestamp below, so that runs are distinct
# without having to be named: a reused id merges two runs into one series and
# is the one mistake here that reads as valid data.
BENCHMARK_RUN_ID=""

usage() {
  echo "Usage: $0 [options]"
  echo ""
  echo "Options:"
  echo "  --deploy              Deploy the locust workers as a Deployment, held open for the web UI"
  echo "  --delete              Delete the locust Deployment"
  echo "  --deploy-job          Create a Job that runs the benchmark once and ends when the"
  echo "                        run does. Returns as soon as it is submitted."
  echo "  --delete-job          Delete the locust Job"
  echo "  --duration TIME       --deploy-job only: how long to run, in locust's spelling (default: 5m)"
  echo "  --users N             --deploy-job only: number of users (default: 10)"
  echo "  --run-id NAME         --deploy-job only: names this run in Prometheus, as the"
  echo "                        run_id label (default: a UTC timestamp). Up to 63"
  echo "                        alphanumerics, dashes, underscores and dots."
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

# Both manifests target the `benchmarking` namespace (so prometheus can scrape
# them when that stack is installed). Ensure it exists either way —
# benchmarking/monitoring.yaml is otherwise optional.
ensure_namespace() {
  echo "Ensuring benchmarking namespace exists..."
  kubectl create namespace benchmarking --dry-run=client -o yaml | kubectl apply -f -
}

# What a run measures, echoed by both deploy paths because it is baked into
# what they apply rather than chosen later.
describe_run() {
  echo "  image:     ${LOCUST_IMAGE}"
  echo "  class:     ${BENCHMARK_USER_CLASS}"
  echo "  workload:  ${BENCHMARK_WORKLOAD_NAME}"
  echo "  template:  ${BENCHMARK_WORKLOAD_TEMPLATE:-<the workload default>}"
}

deploy() {
  ensure_namespace
  echo "Deploying Locust load:"
  describe_run
  envsubst < "${DEPLOYMENT_MANIFEST}" | kubectl apply -f -
}

delete() {
  echo "Deleting Locust load..."
  envsubst < "${DEPLOYMENT_MANIFEST}" | kubectl delete --ignore-not-found -f -
}

# deploy_job submits the run and returns; it does not wait for it. What to do
# with a run in flight is the caller's to decide — follow it, poll it, or
# leave it — and a script that blocked would take that choice away.
deploy_job() {
  ensure_namespace
  # A Job's pod template is immutable, so a second run cannot be an apply over
  # the first. Deleting first also keeps the fixed name unambiguous: the logs
  # and the status under it are the run just submitted, not the previous one.
  # --wait=true so the old pods are gone before the new Job creates its own.
  echo "Deleting any previous ${JOB_NAME}..."
  kubectl delete job "${JOB_NAME}" --namespace=benchmarking \
    --ignore-not-found --wait=true
  echo "Running Locust benchmark as a Job:"
  describe_run
  echo "  duration:  ${BENCHMARK_DURATION}"
  echo "  users:     ${BENCHMARK_USERS}"
  echo "  run_id:    ${BENCHMARK_RUN_ID}"
  envsubst < "${JOB_MANIFEST}" | kubectl apply -f -
  echo
  echo "Submitted. Follow it with:"
  echo "  kubectl logs -f job/${JOB_NAME} -n benchmarking"
  echo "Wait for it with:"
  echo "  kubectl wait --for=condition=complete job/${JOB_NAME} -n benchmarking --timeout=30m"
  echo "Its metrics, where Prometheus is deployed:"
  echo "  locust_request_duration_milliseconds_bucket{run_id=\"${BENCHMARK_RUN_ID}\"}"
}

delete_job() {
  echo "Deleting Locust Job..."
  envsubst < "${JOB_MANIFEST}" | kubectl delete --ignore-not-found -f -
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
    --deploy-job) action="deploy-job" ;;
    --delete-job) action="delete-job" ;;
    --duration) shift; BENCHMARK_DURATION="$1" ;;
    --duration=*) BENCHMARK_DURATION="${1#*=}" ;;
    --users) shift; BENCHMARK_USERS="$1" ;;
    --users=*) BENCHMARK_USERS="${1#*=}" ;;
    --run-id) shift; BENCHMARK_RUN_ID="$1" ;;
    --run-id=*) BENCHMARK_RUN_ID="${1#*=}" ;;
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

# Checked for every action, so that a --users typo is caught before it reaches
# the cluster as a locust argument. The duration is left to locust, which
# knows its own spelling of one and rejects anything else at startup.
if ! [[ "${BENCHMARK_USERS}" =~ ^[0-9]+$ ]] || [[ "${BENCHMARK_USERS}" -lt 1 ]]; then
  echo "Error: --users must be a whole number of at least 1, got '${BENCHMARK_USERS}'" >&2
  exit 1
fi

# A timestamp rather than a random string, so that the ids of a series of runs
# sort into the order the runs happened in. Seconds are enough: the Job it
# names can only exist one at a time.
if [[ -z "${BENCHMARK_RUN_ID}" ]]; then
  BENCHMARK_RUN_ID="$(date -u +%Y%m%dt%H%M%Sz)"
fi
# Checked here rather than left to the API server, which would reject the whole
# manifest for it and say so in terms of label syntax rather than of the flag
# that produced it.
if ! [[ "${BENCHMARK_RUN_ID}" =~ ^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$ ]]; then
  echo "Error: --run-id must be 1-63 alphanumerics, dashes, underscores or dots," >&2
  echo "starting and ending with an alphanumeric. Got '${BENCHMARK_RUN_ID}'" >&2
  exit 1
fi
export BENCHMARK_USER_CLASS BENCHMARK_WORKLOAD_NAME BENCHMARK_WORKLOAD_TEMPLATE
export BENCHMARK_DURATION BENCHMARK_USERS BENCHMARK_RUN_ID

if [[ "${action}" == "deploy" ]]; then
  deploy
elif [[ "${action}" == "delete" ]]; then
  delete
elif [[ "${action}" == "deploy-job" ]]; then
  deploy_job
elif [[ "${action}" == "delete-job" ]]; then
  delete_job
fi
