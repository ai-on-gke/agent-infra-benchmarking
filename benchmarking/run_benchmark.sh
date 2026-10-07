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

# Runs one test case as the locust master and boomer worker Jobs, waits for
# both, and exits with their result. The case is given as JSON rather than
# named in tests.yaml:
#
#   ./benchmarking/run_benchmark.sh --test-case-name glutton \
#     --test-case '{"duration":"3m","users":100,"workload-template":"glutton"}'

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

if [[ -f .env.sh ]]; then
  source .env.sh
fi

MANIFESTS_DIR=benchmarking/locust/manifests

# Have to match metadata.name and namespace in the manifests.
MASTER_JOB=locust-master
WORKER_JOB=boomer-worker
NAMESPACE=benchmarking
MASTER_HOST="${MASTER_JOB}.${NAMESPACE}.svc.cluster.local"

USER_CLASS=runworkload
BENCHMARK_RUNNER_KSA="${BENCHMARK_RUNNER_KSA:-benchmark-runner}"

# Case keys that are not worker flags; every other key becomes `--key=value`.
NON_WORKER_KEYS='["duration","users","spawn-rate","worker-count","sandbox-class","max-failure-ratio","warm-up","repeats"]'

TEMPLATES_DIR=benchmarking/workloads/manifests

POD_START_TIMEOUT_SECS=300
WAIT_TIMEOUT_SECS=3600
PROGRESS_EVERY_SECS=30

TEST_CASE_NAME=""
TEST_CASE_JSON=""
RUN_ID=""
NO_BUILD=0
NO_DEPLOY_WORKLOADS=0

usage() {
  echo "Usage: $0 --test-case-name NAME --test-case JSON [--run-id ID] [--no-build] [--no-deploy-workloads]"
  echo ""
  echo "  --test-case-name NAME  What the run is recorded under: the benchmark-test-case"
  echo "                         label and the test_case label in Prometheus."
  echo "  --test-case JSON       The case's keys as a JSON object, as in tests.yaml."
  echo "  --run-id ID            The run this case is part of (default: a UTC timestamp)."
  echo "  --no-build             Use the images already pushed rather than building them."
  echo "  --no-deploy-workloads  Use the mock-llm and ActorTemplates already deployed rather than"
  echo "                         deploying the case's ActorTemplate and taking its golden snapshot."
  echo "                         The WorkerPool is recreated either way."
  echo ""
  echo "LOCUST_IMAGE and BOOMER_IMAGE must be set; .env.sh is sourced if present."
  echo "USE_CLOUD_BUILD=1 builds the images on Cloud Build instead of local Docker."
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --test-case-name) shift; TEST_CASE_NAME="$1" ;;
    --test-case-name=*) TEST_CASE_NAME="${1#*=}" ;;
    --test-case) shift; TEST_CASE_JSON="$1" ;;
    --test-case=*) TEST_CASE_JSON="${1#*=}" ;;
    --run-id) shift; RUN_ID="$1" ;;
    --run-id=*) RUN_ID="${1#*=}" ;;
    --no-build) NO_BUILD=1 ;;
    --no-deploy-workloads) NO_DEPLOY_WORKLOADS=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Error: Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

die() {
  echo "Error: $*" >&2
  exit 1
}

for var in LOCUST_IMAGE BOOMER_IMAGE; do
  [[ -n "${!var:-}" ]] || die "${var} must be set."
done
command -v jq &>/dev/null || die "jq is required to turn the test case into the Jobs' arguments."

RUN_ID="${RUN_ID:-$(date -u +%Y%m%dt%H%M%Sz)}"
# Both end up as label values, which the API server would reject less clearly.
LABEL_RE='^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$'
[[ "${RUN_ID}" =~ ${LABEL_RE} ]] || die "--run-id '${RUN_ID}' is not a valid label value."
[[ "${TEST_CASE_NAME}" =~ ${LABEL_RE} ]] || die "--test-case-name '${TEST_CASE_NAME}' is not a valid label value."

jq -e 'type == "object"' <<<"${TEST_CASE_JSON:-null}" &>/dev/null ||
  die "--test-case must be a JSON object, got '${TEST_CASE_JSON}'."
jq -e 'has("duration") and has("users")' <<<"${TEST_CASE_JSON}" >/dev/null ||
  die "the test case must state \`duration\` and \`users\`."
# spawn-rate defaults to the user count, putting the whole load on at once.
TEST_CASE_JSON="$(jq -c '."spawn-rate" //= .users' <<<"${TEST_CASE_JSON}")"

WORKER_COUNT="$(jq -r '."worker-count" // 1' <<<"${TEST_CASE_JSON}")"
SANDBOX_CLASS="$(jq -r '."sandbox-class" // "gvisor"' <<<"${TEST_CASE_JSON}")"
[[ "${WORKER_COUNT}" =~ ^[0-9]+$ ]] || die "\`worker-count\` must be a whole number, got '${WORKER_COUNT}'."
[[ "${SANDBOX_CLASS}" == gvisor || "${SANDBOX_CLASS}" == microvm ]] ||
  die "\`sandbox-class\` must be gvisor or microvm, got '${SANDBOX_CLASS}'."
TEMPLATE="$(jq -r '."workload-template" // ."workload-name" // empty' <<<"${TEST_CASE_JSON}")"
if [[ "${NO_DEPLOY_WORKLOADS}" -eq 0 ]]; then
  [[ -n "${TEMPLATE}" ]] ||
    die "the test case names no \`workload-template\` or \`workload-name\` to deploy; --no-deploy-workloads runs on what is deployed."
  [[ -f "${TEMPLATES_DIR}/${TEMPLATE}-template.yaml.tmpl" ]] ||
    die "no ${TEMPLATES_DIR}/${TEMPLATE}-template.yaml.tmpl to deploy the case's workload from."
fi

master_args() {
  jq -c --arg file "/app/tests/${USER_CLASS}.py" --arg max_wait "${POD_START_TIMEOUT_SECS}" '
    ["-f", $file, "--headless", "-t", .duration, "-u", .users, "-r", ."spawn-rate",
     "--master", "--expect-workers", "1", "--expect-workers-max-wait", $max_wait]
    | map(tostring)' <<<"${TEST_CASE_JSON}"
}

worker_args() {
  jq -c --arg master "${MASTER_HOST}" --arg class "${USER_CLASS}" --argjson skip "${NON_WORKER_KEYS}" '
    ["--master-host=\($master)", "--user-class=\($class)"]
    + [to_entries[] | select(.key as $k | $skip | index($k) | not) | "--\(.key)=\(.value)"]' \
    <<<"${TEST_CASE_JSON}"
}

# Prints a Job's succeeded and failed counts as "succeeded|failed".
job_status() {
  kubectl get job "$1" --namespace="${NAMESPACE}" \
    -o jsonpath='{.status.succeeded}|{.status.failed}' 2>/dev/null || echo '|'
}

# Boomer tries the master once at startup and does not retry, so the worker is
# only submitted once this returns.
wait_for_master_ready() {
  local deadline=$((SECONDS + POD_START_TIMEOUT_SECS)) ready succeeded failed
  echo "Waiting for the ${MASTER_JOB} pod to be ready..."
  while ((SECONDS < deadline)); do
    ready="$(kubectl get pods --namespace="${NAMESPACE}" -l "job-name=${MASTER_JOB}" \
      -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
    [[ "${ready}" == True ]] && return 0
    IFS='|' read -r succeeded failed < <(job_status "${MASTER_JOB}")
    if [[ "${succeeded:-0}" -ge 1 || "${failed:-0}" -ge 1 ]]; then
      echo "Error: ${MASTER_JOB} ended before it was ready:" >&2
      echo "  kubectl logs job/${MASTER_JOB} -n ${NAMESPACE}" >&2
      return 1
    fi
    sleep 2
  done
  echo "Error: the ${MASTER_JOB} pod was not ready within ${POD_START_TIMEOUT_SECS}s." >&2
  kubectl describe job "${MASTER_JOB}" --namespace="${NAMESPACE}" >&2 || true
  return 1
}

# Waits for both Jobs to end rather than the first to fail: each one's log is
# part of the run.
wait_for_jobs() {
  local deadline=$((SECONDS + WAIT_TIMEOUT_SECS)) started=${SECONDS}
  local next_report=$((SECONDS + PROGRESS_EVERY_SECS))
  local job succeeded failed done_count failed_jobs
  echo "Waiting for the run to finish..."
  while ((SECONDS < deadline)); do
    done_count=0
    failed_jobs=()
    for job in "${MASTER_JOB}" "${WORKER_JOB}"; do
      IFS='|' read -r succeeded failed < <(job_status "${job}")
      if [[ "${succeeded:-0}" -ge 1 ]]; then
        done_count=$((done_count + 1))
      elif [[ "${failed:-0}" -ge 1 ]]; then
        done_count=$((done_count + 1))
        failed_jobs+=("${job}")
      fi
    done
    if [[ "${done_count}" -eq 2 ]]; then
      if [[ "${#failed_jobs[@]}" -eq 0 ]]; then
        echo "${MASTER_JOB} and ${WORKER_JOB} completed."
        echo "  kubectl logs job/${MASTER_JOB} -n ${NAMESPACE}"
        return 0
      fi
      echo "Error: ${failed_jobs[*]} failed:" >&2
      for job in "${failed_jobs[@]}"; do
        echo "  kubectl logs job/${job} -n ${NAMESPACE}" >&2
      done
      return 1
    fi
    if ((SECONDS >= next_report)); then
      echo "  $((SECONDS - started))s/${WAIT_TIMEOUT_SECS}s — still running"
      next_report=$((SECONDS + PROGRESS_EVERY_SECS))
    fi
    sleep 5
  done
  echo "Error: timed out after ${WAIT_TIMEOUT_SECS}s; the Jobs are left running." >&2
  return 1
}

if [[ "${NO_BUILD}" -eq 0 ]]; then
  echo "=== Building and pushing images ==="
  benchmarking/locust/build_and_push.sh
  # The workload images are only read when the workloads are deployed.
  if [[ "${NO_DEPLOY_WORKLOADS}" -eq 0 ]]; then
    echo
    benchmarking/workloads/build_and_push.sh
  fi
  echo
fi

# Read by envsubst from the environment.
export LOCUST_IMAGE BOOMER_IMAGE BENCHMARK_RUNNER_KSA
export BENCHMARK_RUN_ID="${RUN_ID}"
export BENCHMARK_RECORD_NAME="${TEST_CASE_NAME}"
export BENCHMARK_ONLY_SUMMARY=false
LOCUST_MASTER_ARGS="$(master_args)"
BOOMER_WORKER_ARGS="$(worker_args)"
export LOCUST_MASTER_ARGS BOOMER_WORKER_ARGS

kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

# A Job's pod template is immutable, so the previous run's Jobs go first.
# Foreground, so their pods are gone too and the job-name lookup only sees
# this run's.
echo "Deleting any previous ${MASTER_JOB} and ${WORKER_JOB}..."
kubectl delete job "${MASTER_JOB}" "${WORKER_JOB}" --namespace="${NAMESPACE}" \
  --ignore-not-found --cascade=foreground --wait=true

echo
echo "=== Deploying the worker pool ==="
benchmarking/workloads/deploy_worker_pool.sh --deploy --worker-count "${WORKER_COUNT}" --sandbox-class "${SANDBOX_CLASS}"
echo

# The golden snapshot is taken on the pool just deployed.
if [[ "${NO_DEPLOY_WORKLOADS}" -eq 0 ]]; then
  echo "=== Deploying workloads ==="
  benchmarking/workloads/deploy_workloads.sh --deploy --workload-template "${TEMPLATE}" --sandbox-class "${SANDBOX_CLASS}"
  echo
fi

echo "Running ${TEST_CASE_NAME} (run ${RUN_ID}):"
echo "  master: ${LOCUST_IMAGE} ${LOCUST_MASTER_ARGS}"
echo "  worker: ${BOOMER_IMAGE} ${BOOMER_WORKER_ARGS}"
envsubst <"${MANIFESTS_DIR}/master-job.yaml" | kubectl apply -f -
wait_for_master_ready
envsubst <"${MANIFESTS_DIR}/worker-job.yaml" | kubectl apply -f -

wait_for_jobs
