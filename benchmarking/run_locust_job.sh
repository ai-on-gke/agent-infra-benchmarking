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

# Runs the benchmark as a Job, streams its logs, and exits with its result.
# The counterpart of deploy_locust.sh, which deploys the same benchmark as a
# Deployment and leaves the web UI holding it open.
#
# Submitting and watching are separate concerns and separate scripts:
# locust/deploy.sh --deploy-job creates the Job and returns, and everything
# below is this script waiting on what that created. --no-wait stops at the
# same place deploy.sh does.
#
# It assumes the thing being measured is already in the cluster. That is the
# difference worth knowing between the two scripts: deploy_locust.sh stands a
# whole stack up because a session at the web UI is a thing that is set up
# once and then used, while a Job is a single run and is usually one of
# several against workloads that must not change between them. Redeploying
# the workloads per run would reset the golden snapshots underneath, so the
# runs either side of it would not be comparable.
#
# --build and --deploy-workloads are for when nothing is deployed yet. They
# set up and exit, rather than going on to run something, so that no run
# measures the deployment it just made — the first run against fresh golden
# snapshots pays for warming what the rest inherit:
#
#   ./benchmarking/run_locust_job.sh --build --deploy-workloads
#   ./benchmarking/run_locust_job.sh --users 50
#   ./benchmarking/run_locust_job.sh --users 100
#
# Each run there blocks until it is over and fails if it did, so a series like
# that is a script rather than three things to sit and watch.
#
# Deleting is left to deploy_locust.sh --delete, which owns the workloads;
# --delete here removes only the Job.

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
BENCHMARKING_DIR="${ROOT}/benchmarking"

# Have to match what locust/deploy.sh creates, which is what this waits on.
# Mirrored rather than shared because two scripts and a manifest is not enough
# to justify a file that exists only to hold two strings.
JOB_NAME=locust-job
NAMESPACE=benchmarking

# How often to report that the pod has not started yet. Once it has, the logs
# are the progress report.
PROGRESS_EVERY_SECS=30

# Every setting that belongs to a run rather than to a setup action. Empty
# means the caller did not give it, which is what the check below reads to
# tell a run option apart from an unset one, and what leaves each default
# where it is defined rather than restating it in a second place. The two
# timeouts are the exception in having a default here, since nothing further
# down the chain holds one.
DURATION=""
USERS=""
RUN_ID=""
USER_CLASS=""
WORKLOAD_NAME=""
WORKLOAD_TEMPLATE=""
WAIT_TIMEOUT_SECS=""
POD_START_TIMEOUT_SECS=""
NO_WAIT=0
NO_METRICS=0

# The lookback for the queries at the end. It has only to reach back to the
# run's last scrape, which was seconds before the query, so it is generous
# rather than tuned: last_over_time takes the final sample in the window, and
# a counter that started at zero with this run makes that sample the run's own
# total however long the run was.
METRICS_WINDOW=1h

# Ceiling on the wait for the run itself, generous next to a default duration
# of 5m because a run is free to ask for far longer. It bounds only the log
# stream and the status poll; the Job is left alone when it is reached, and
# the message says so.
DEFAULT_WAIT_TIMEOUT_SECS=3600
# Ceiling on the wait for the pod to start, which is the image pull and the
# scheduling rather than the run.
DEFAULT_POD_START_TIMEOUT_SECS=300

# The setup actions. Each one exits when its work is done rather than going on
# to run something: setting the cluster up and measuring it are separate
# steps, because a series of runs has to measure one unchanging deployment.
BUILD=0
DEPLOY_WORKLOADS=0
DEPLOY_MONITORING=0
DELETE=0

# Prometheus, deployed by --deploy-monitoring. Grafana is not deployed: it
# sits in monitoring/grafana.yaml next to this, for when it is wanted.
MONITORING_MANIFEST="${BENCHMARKING_DIR}/monitoring/prometheus.yaml"

# Forwarded to workloads/deploy.sh, and only meaningful with
# --deploy-workloads. Empty keeps that script's own defaults.
WORKER_COUNT=""
SANDBOX_CLASS=""
OTLP_ENDPOINT=""
ACTOR_MEMORY=""
WORKLOAD_WAIT_TIMEOUT=""

usage() {
  echo "Usage: $0 [options]"
  echo ""
  echo "Runs the benchmark as a Job, streams its logs, and exits with its result."
  echo "The workloads it measures are assumed to be deployed already."
  echo ""
  echo "The run:"
  echo "  --duration TIME         How long to run, in locust's spelling of a duration"
  echo "                          like 30s, 5m or 1h30m (default: 5m)"
  echo "  --users N               Number of users (default: 10)"
  echo "  --run-id NAME           Names this run in Prometheus, as the run_id label on"
  echo "                          every series it produces (default: a UTC timestamp)"
  echo "  --user-class NAME       Locust user class, lowercase; runs tests/NAME.py"
  echo "                          (default: runworkload)"
  echo "  --workload-name NAME    The workload a RunWorkloadUser drives: glutton | openclaw"
  echo "                          (default: glutton)"
  echo "  --workload-template NAME"
  echo "                          The ActorTemplate the workload is measured on (default:"
  echo "                          glutton). Has to be one --workload-name can address, so"
  echo "                          set it whenever that changes."
  echo ""
  echo "Waiting:"
  echo "  --no-wait               Submit the Job and return, instead of following it."
  echo "                          The run continues in the cluster either way."
  echo "  --wait-timeout SECONDS  Ceiling on the wait for the run (default: 3600). Reaching"
  echo "                          it stops the waiting, not the Job."
  echo "  --pod-start-timeout SECONDS"
  echo "                          Ceiling on the wait for the pod to start, which covers the"
  echo "                          image pull and the scheduling (default: 300)"
  echo "  --no-metrics            Skip the Prometheus summary printed after the run. The"
  echo "                          summary is skipped anyway where Prometheus is not"
  echo "                          deployed, or where jq or curl is missing."
  echo ""
  echo "Setup, which runs nothing. Either one ends the command when its work is done,"
  echo "so a run is started separately afterwards:"
  echo "  --build                 Build and push the locust image, then exit"
  echo "  --deploy-workloads      Deploy the benchmark workloads, then exit. Redeploying"
  echo "                          retakes the golden snapshots, so runs that straddle it"
  echo "                          are not comparable; do it once, before a series."
  echo "  --deploy-monitoring     Deploy Prometheus into the benchmarking namespace, then"
  echo "                          exit. It scrapes every run that starts after it, so"
  echo "                          deploy it before the runs it is meant to record — a"
  echo "                          run already finished cannot be scraped retroactively,"
  echo "                          and its storage does not outlive its pod. Grafana is"
  echo "                          not deployed; see monitoring/grafana.yaml."
  echo ""
  echo "With --deploy-workloads, forwarded to workloads/deploy.sh:"
  echo "  --worker-count N        Number of WorkerPool replicas (default: 1)"
  echo "  --sandbox-class CLASS   Sandbox runtime for the WorkerPool: gvisor | microvm"
  echo "                          (default: gvisor)"
  echo "  --actor-memory SIZE     Memory limit for the benchmark ActorTemplates"
  echo "                          (default: 256Mi, the microvm minimum)"
  echo "  --otlp-endpoint URL     The address an instrumented actor container sends"
  echo "                          telemetry to"
  echo "  --workload-wait-timeout SECONDS"
  echo "                          Timeout waiting for the ateom workers to be ready"
  echo "                          (default: 300). It bounds this deploy; --wait-timeout"
  echo "                          above bounds the run that follows it."
  echo ""
  echo "Other:"
  echo "  --delete                Delete the Job and exit. The workloads are left alone;"
  echo "                          deploy_locust.sh --delete removes those."
  echo "  -h|--help               Show this help message"
  echo ""
  echo "Environment:"
  echo "  PROJECT_ID, BUCKET_NAME, etc. are read from .env.sh by the"
  echo "  scripts this wrapper invokes."
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --duration) shift; DURATION="$1" ;;
    --duration=*) DURATION="${1#*=}" ;;
    --users) shift; USERS="$1" ;;
    --users=*) USERS="${1#*=}" ;;
    --run-id) shift; RUN_ID="$1" ;;
    --run-id=*) RUN_ID="${1#*=}" ;;
    --user-class) shift; USER_CLASS="$1" ;;
    --user-class=*) USER_CLASS="${1#*=}" ;;
    --workload-name) shift; WORKLOAD_NAME="$1" ;;
    --workload-name=*) WORKLOAD_NAME="${1#*=}" ;;
    --workload-template) shift; WORKLOAD_TEMPLATE="$1" ;;
    --workload-template=*) WORKLOAD_TEMPLATE="${1#*=}" ;;
    --no-wait) NO_WAIT=1 ;;
    --no-metrics) NO_METRICS=1 ;;
    --wait-timeout) shift; WAIT_TIMEOUT_SECS="$1" ;;
    --wait-timeout=*) WAIT_TIMEOUT_SECS="${1#*=}" ;;
    --pod-start-timeout) shift; POD_START_TIMEOUT_SECS="$1" ;;
    --pod-start-timeout=*) POD_START_TIMEOUT_SECS="${1#*=}" ;;
    --build) BUILD=1 ;;
    --deploy-workloads) DEPLOY_WORKLOADS=1 ;;
    --deploy-monitoring) DEPLOY_MONITORING=1 ;;
    --worker-count) shift; WORKER_COUNT="$1" ;;
    --worker-count=*) WORKER_COUNT="${1#*=}" ;;
    --sandbox-class) shift; SANDBOX_CLASS="$1" ;;
    --sandbox-class=*) SANDBOX_CLASS="${1#*=}" ;;
    --otlp-endpoint) shift; OTLP_ENDPOINT="$1" ;;
    --otlp-endpoint=*) OTLP_ENDPOINT="${1#*=}" ;;
    --actor-memory) shift; ACTOR_MEMORY="$1" ;;
    --actor-memory=*) ACTOR_MEMORY="${1#*=}" ;;
    --workload-wait-timeout) shift; WORKLOAD_WAIT_TIMEOUT="$1" ;;
    --workload-wait-timeout=*) WORKLOAD_WAIT_TIMEOUT="${1#*=}" ;;
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

# job_status prints the Job's succeeded and failed counts as one pipe-joined
# pair. One get for both, so they cannot come from different reads of a Job
# that finished in between them, and a pipe rather than a space because the
# default IFS collapses a leading empty field — "not succeeded, failed" would
# otherwise be read as "succeeded".
job_status() {
  kubectl get job "${JOB_NAME}" --namespace="${NAMESPACE}" \
    -o jsonpath='{.status.succeeded}|{.status.failed}' 2>/dev/null || echo '|'
}

# wait_for_pod blocks until the Job's pod has left Pending, because there are
# no logs to follow until it has. A pod that went straight to Succeeded or
# Failed counts as started: its logs are still there to read.
wait_for_pod() {
  local started=${SECONDS}
  local deadline=$((SECONDS + POD_START_TIMEOUT_SECS))
  local next_report=$((SECONDS + PROGRESS_EVERY_SECS))
  local phase

  echo "Waiting for the pod to start..."
  while ((SECONDS < deadline)); do
    # The app label is this manifest's own, so it cannot match a pod from the
    # Deployment, which labels its own `app: locust`.
    phase="$(kubectl get pods --namespace="${NAMESPACE}" -l app="${JOB_NAME}" \
      -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true)"
    case "${phase}" in
      Running|Succeeded|Failed) return 0 ;;
    esac
    if ((SECONDS >= next_report)); then
      echo "  $((SECONDS - started))s/${POD_START_TIMEOUT_SECS}s — pod is ${phase:-not created yet}"
      next_report=$((SECONDS + PROGRESS_EVERY_SECS))
    fi
    sleep 2
  done

  echo "Error: the pod did not start within ${POD_START_TIMEOUT_SECS}s." >&2
  kubectl describe job "${JOB_NAME}" --namespace="${NAMESPACE}" >&2 || true
  return 1
}

# follow_run streams the run's logs and returns what the Job did.
#
# The stream is the wait: kubectl logs -f returns when the container exits, so
# nothing polls while the benchmark is running and the output arrives as it is
# produced. The status is then read from the Job, because the stream ending
# says the run is over and not how it went — and a stream that broke early
# says nothing at all, which is why the result is polled for rather than
# assumed to be there the moment the logs stop.
follow_run() {
  local deadline=$((SECONDS + WAIT_TIMEOUT_SECS))
  local succeeded failed

  wait_for_pod || return 1

  echo
  echo "=== Run log ==="
  # Not fatal on its own: a dropped stream is a lost log, not a failed run,
  # and the status below is what decides.
  kubectl logs -f "job/${JOB_NAME}" --namespace="${NAMESPACE}" || true
  echo "=== End of run log ==="
  echo

  while ((SECONDS < deadline)); do
    IFS='|' read -r succeeded failed < <(job_status)
    if [[ "${succeeded:-0}" -ge 1 ]]; then
      echo "${JOB_NAME} completed."
      return 0
    fi
    if [[ "${failed:-0}" -ge 1 ]]; then
      echo "Error: ${JOB_NAME} failed. The log above is the run." >&2
      return 1
    fi
    sleep 5
  done

  echo "Error: timed out after ${WAIT_TIMEOUT_SECS}s waiting for ${JOB_NAME}." >&2
  echo "It is still in the cluster; --wait-timeout raises the ceiling." >&2
  echo "  kubectl logs -f job/${JOB_NAME} -n ${NAMESPACE}" >&2
  return 1
}

# PROM_PORT is the local end of the port-forward report_metrics opens, chosen
# by kubectl rather than fixed here so that a port-forward the operator is
# already running cannot collide with this one.
PROM_PORT=""
PROM_PF_PID=""
PROM_PF_LOG=""

stop_port_forward() {
  [[ -n "${PROM_PF_PID}" ]] && kill "${PROM_PF_PID}" 2>/dev/null || true
  [[ -n "${PROM_PF_PID}" ]] && wait "${PROM_PF_PID}" 2>/dev/null || true
  [[ -n "${PROM_PF_LOG}" ]] && rm -f "${PROM_PF_LOG}" || true
  PROM_PF_PID=""
  PROM_PF_LOG=""
  PROM_PORT=""
}

# start_port_forward opens a forward to Prometheus on a local port kubectl
# picks, and reads back which one that was. Returns non-zero when the forward
# does not come up, which is not fatal anywhere it is called: a run that
# finished is still a run, whether or not its metrics can be read afterwards.
start_port_forward() {
  PROM_PF_LOG="$(mktemp)"
  kubectl port-forward --namespace="${NAMESPACE}" svc/prometheus :9090 \
    >"${PROM_PF_LOG}" 2>&1 &
  PROM_PF_PID=$!

  # kubectl announces the port it chose once the listener is accepting, so
  # that line is both the port and the readiness signal.
  local i
  for ((i = 0; i < 50; i++)); do
    if ! kill -0 "${PROM_PF_PID}" 2>/dev/null; then
      break
    fi
    PROM_PORT="$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9][0-9]*\).*/\1/p' \
      "${PROM_PF_LOG}" 2>/dev/null | head -1)"
    [[ -n "${PROM_PORT}" ]] && return 0
    sleep 0.2
  done

  echo "  could not port-forward to Prometheus:" >&2
  sed 's/^/    /' "${PROM_PF_LOG}" >&2 2>/dev/null || true
  stop_port_forward
  return 1
}

# promql runs one instant query and prints its result rows as TSV, formatted
# by the jq expression in $2. A query that Prometheus rejected is reported
# with what it said, rather than as an empty result that reads as "the run
# produced nothing".
promql() {
  local query="$1" row="$2" json status
  if ! json="$(curl -sS --get --max-time 20 \
    --data-urlencode "query=${query}" \
    "http://127.0.0.1:${PROM_PORT}/api/v1/query" 2>&1)"; then
    echo "  query failed: ${json}" >&2
    return 1
  fi
  status="$(jq -r '.status // "error"' <<<"${json}" 2>/dev/null || echo error)"
  if [[ "${status}" != "success" ]]; then
    echo "  query rejected: $(jq -r '.error // "unparseable response"' <<<"${json}" 2>/dev/null)" >&2
    return 1
  fi
  jq -r "${row}" <<<"${json}"
}

# report_metrics prints what Prometheus recorded for one run. Everything here
# is best-effort: it runs after the result of the run is already decided, so
# no failure in it changes that result, and each way it can come up empty is
# reported as itself rather than as an absence of data.
report_metrics() {
  local run_id="$1"

  for tool in jq curl; do
    if ! command -v "${tool}" &>/dev/null; then
      echo "Skipping the metrics summary: ${tool} is not installed."
      return 0
    fi
  done
  if ! kubectl get deployment prometheus --namespace="${NAMESPACE}" &>/dev/null; then
    echo "Skipping the metrics summary: Prometheus is not deployed."
    echo "  $0 --deploy-monitoring   deploys it, for the runs after it"
    return 0
  fi
  start_port_forward || return 0

  echo
  echo "=== Metrics for run_id=${run_id} ==="

  local counts quantiles
  # The counters are cumulative and the pod was new, so the last sample of the
  # run is the run's own total.
  counts="$(promql \
    "sum by (name,status) (last_over_time(locust_requests_total{run_id=\"${run_id}\"}[${METRICS_WINDOW}]))" \
    '.data.result[] | [.metric.name, .metric.status, (.value[1]|tonumber|round|tostring)] | @tsv' \
    || true)"

  if [[ -z "${counts}" ]]; then
    echo "  No series carry this run_id."
    echo "  Prometheus scrapes every 10s and cannot collect a run retroactively, so a"
    echo "  run shorter than a scrape interval, or one that started before Prometheus"
    echo "  did, leaves nothing behind. The log above is that run's only record."
    stop_port_forward
    return 0
  fi

  printf '  %-18s %-9s %s\n' "REQUEST" "STATUS" "COUNT"
  sort <<<"${counts}" | while IFS=$'\t' read -r name status count; do
    printf '  %-18s %-9s %s\n' "${name}" "${status}" "${count}"
  done

  # The same cumulative buckets, read as the quantiles over the whole run. One
  # query for the three, each tagged by label_replace, so the three cannot
  # come from three different reads.
  local buckets query=""
  buckets="sum by (le,name) (last_over_time(locust_request_duration_milliseconds_bucket{run_id=\"${run_id}\"}[${METRICS_WINDOW}]))"
  local pq
  for pq in 0.5:p50 0.95:p95 0.99:p99; do
    [[ -n "${query}" ]] && query+=" or "
    query+="label_replace(histogram_quantile(${pq%%:*}, ${buckets}), \"q\", \"${pq##*:}\", \"\", \"\")"
  done
  # Left as the string Prometheus sent rather than converted here: a quantile
  # over empty buckets comes back as NaN, which jq renders as an empty field,
  # so a number is whatever still looks like one below.
  quantiles="$(promql "${query}" \
    '.data.result[] | [.metric.name, .metric.q, .value[1]] | @tsv' \
    || true)"

  if [[ -n "${quantiles}" ]]; then
    echo
    printf '  %-18s %-6s %s\n' "REQUEST" "" "LATENCY (ms)"
    sort <<<"${quantiles}" | while IFS=$'\t' read -r name q value; do
      if [[ "${value}" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        # LC_ALL=C because printf formats a float with the locale's separator,
        # and rejects a dot-decimal input wherever that separator is a comma.
        LC_ALL=C printf '  %-18s %-6s %.1f\n' "${name}" "${q}" "${value}"
      else
        # NaN, an empty field, or +Inf: no request of this name succeeded, so
        # there is no latency distribution to take a quantile of.
        printf '  %-18s %-6s %s\n' "${name}" "${q}" "-"
      fi
    done
  fi

  echo
  echo "  Prometheus holds this until its pod restarts. Query it with:"
  echo "    kubectl port-forward -n ${NAMESPACE} svc/prometheus 9090:9090"
  echo "    locust_requests_total{run_id=\"${run_id}\"}"
  stop_port_forward
}

if [[ "${DELETE}" -eq 1 ]]; then
  echo "=== Deleting the locust Job ==="
  "${BENCHMARKING_DIR}/locust/deploy.sh" --delete-job
  exit 0
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

# A workload option without the stage that would use it is reported rather
# than ignored: it reads as having shaped the deployment the run measures,
# and a run that quietly measured the previous deployment instead answers a
# different question than the one asked.
if [[ "${DEPLOY_WORKLOADS}" -eq 0 ]]; then
  for pair in "--worker-count:${WORKER_COUNT}" \
              "--sandbox-class:${SANDBOX_CLASS}" \
              "--otlp-endpoint:${OTLP_ENDPOINT}" \
              "--actor-memory:${ACTOR_MEMORY}" \
              "--workload-wait-timeout:${WORKLOAD_WAIT_TIMEOUT}"; do
    if [[ -n "${pair#*:}" ]]; then
      echo "Error: ${pair%%:*} only applies with --deploy-workloads, which was not given." >&2
      echo "The workloads already in the cluster are what this run measures." >&2
      exit 1
    fi
  done
fi

# The setup actions, which end the invocation. A setup that went on to run
# something would measure the deployment it had just made, which is the one
# case where the run is not comparable to the ones after it: the golden
# snapshots are new, so the first run pays for warming what the rest inherit.
# Setting up and measuring are therefore two commands, and a run option given
# alongside a setup action is a mistake worth naming rather than dropping —
# nothing here would run it.
if [[ "${BUILD}" -eq 1 || "${DEPLOY_WORKLOADS}" -eq 1 || "${DEPLOY_MONITORING}" -eq 1 ]]; then
  for pair in "--duration:${DURATION}" \
              "--users:${USERS}" \
              "--run-id:${RUN_ID}" \
              "--user-class:${USER_CLASS}" \
              "--workload-name:${WORKLOAD_NAME}" \
              "--workload-template:${WORKLOAD_TEMPLATE}" \
              "--wait-timeout:${WAIT_TIMEOUT_SECS}" \
              "--pod-start-timeout:${POD_START_TIMEOUT_SECS}"; do
    if [[ -n "${pair#*:}" ]]; then
      echo "Error: ${pair%%:*} describes a run, and the setup actions only set up for" >&2
      echo "one. Run this again without it, then start the run on its own:" >&2
      echo "  $0 ${pair%%:*} ${pair#*:}" >&2
      exit 1
    fi
  done
  for flag in "--no-wait:${NO_WAIT}" "--no-metrics:${NO_METRICS}"; do
    if [[ "${flag#*:}" -eq 1 ]]; then
      echo "Error: ${flag%%:*} describes a run, and the setup actions start none." >&2
      exit 1
    fi
  done

  if [[ "${BUILD}" -eq 1 ]]; then
    echo
    echo "=== Building and pushing locust image ==="
    "${BENCHMARKING_DIR}/locust/build_and_push.sh"
  fi

  # Before the workloads, so that a single setup command leaves Prometheus
  # already watching while they come up.
  if [[ "${DEPLOY_MONITORING}" -eq 1 ]]; then
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
  fi

  if [[ "${DEPLOY_WORKLOADS}" -eq 1 ]]; then
    echo
    echo "=== Deploying benchmark workloads ==="
    # An empty value must not become an empty argument, which would overwrite
    # the default in workloads/deploy.sh rather than leave it in place.
    workload_args=(--deploy)
    [[ -n "${WORKER_COUNT}" ]] && workload_args+=(--worker-count "${WORKER_COUNT}")
    [[ -n "${SANDBOX_CLASS}" ]] && workload_args+=(--sandbox-class "${SANDBOX_CLASS}")
    [[ -n "${OTLP_ENDPOINT}" ]] && workload_args+=(--otlp-endpoint "${OTLP_ENDPOINT}")
    [[ -n "${ACTOR_MEMORY}" ]] && workload_args+=(--actor-memory "${ACTOR_MEMORY}")
    [[ -n "${WORKLOAD_WAIT_TIMEOUT}" ]] && workload_args+=(--wait-timeout "${WORKLOAD_WAIT_TIMEOUT}")
    "${BENCHMARKING_DIR}/workloads/deploy.sh" "${workload_args[@]}"
  fi

  echo
  echo "Setup done. Start a run with:"
  echo "  $0 --duration 5m --users 10"
  exit 0
fi

# Resolved here rather than at the declaration, so that the check above can
# tell a timeout the caller gave from one it did not.
WAIT_TIMEOUT_SECS="${WAIT_TIMEOUT_SECS:-${DEFAULT_WAIT_TIMEOUT_SECS}}"
POD_START_TIMEOUT_SECS="${POD_START_TIMEOUT_SECS:-${DEFAULT_POD_START_TIMEOUT_SECS}}"
for pair in "--wait-timeout:${WAIT_TIMEOUT_SECS}" \
            "--pod-start-timeout:${POD_START_TIMEOUT_SECS}"; do
  if ! [[ "${pair#*:}" =~ ^[0-9]+$ ]]; then
    echo "Error: ${pair%%:*} must be a whole number of seconds, got '${pair#*:}'" >&2
    exit 1
  fi
done

# Generated here rather than left to deploy.sh, which would default it out of
# reach: the summary at the end has to name the run it is querying for. The
# spelling matches that default, and an id the caller gave is left alone for
# deploy.sh to validate.
if [[ -z "${RUN_ID}" ]]; then
  RUN_ID="$(date -u +%Y%m%dt%H%M%Sz)"
fi

echo
echo "=== Running the benchmark Job ==="
job_args=(--deploy-job)
[[ -n "${DURATION}" ]] && job_args+=(--duration "${DURATION}")
[[ -n "${USERS}" ]] && job_args+=(--users "${USERS}")
[[ -n "${RUN_ID}" ]] && job_args+=(--run-id "${RUN_ID}")
[[ -n "${USER_CLASS}" ]] && job_args+=(--user-class "${USER_CLASS}")
[[ -n "${WORKLOAD_NAME}" ]] && job_args+=(--workload-name "${WORKLOAD_NAME}")
[[ -n "${WORKLOAD_TEMPLATE}" ]] && job_args+=(--workload-template "${WORKLOAD_TEMPLATE}")
"${BENCHMARKING_DIR}/locust/deploy.sh" "${job_args[@]}"

if [[ "${NO_WAIT}" -eq 1 ]]; then
  exit 0
fi

# The result of the run is decided by follow_run and by nothing after it, so
# the summary cannot turn a passing run into a failing one. A failed run still
# gets one: what it managed to record before it stopped is usually the point.
run_status=0
follow_run || run_status=$?

if [[ "${NO_METRICS}" -eq 0 ]]; then
  report_metrics "${RUN_ID}" || true
fi

exit "${run_status}"
