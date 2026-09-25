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

# Runs the benchmark as a Job, waits for it, and exits with its result. In
# order: it builds and pushes the images, resolves the case out of the image it
# just built, recreates the WorkerPool, renders manifests/job.yaml and submits
# it, follows the run, and reports what Prometheus recorded.
#
# It assumes the thing being measured is already in the cluster.
# deploy-benchmarking.sh puts it there, and owns every teardown but the Job's:
#
#   ./benchmarking/deploy-benchmarking.sh --deploy
#   ./benchmarking/run_locust_job.sh --test-case glutton-baseline
#   ./benchmarking/run_locust_job.sh --test-case glutton-scale
#
# Each run blocks until it is over and fails if it did, so a series like that
# is a script rather than three things to sit and watch.

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"
BENCHMARKING_DIR="${ROOT}/benchmarking"

if [[ -f .env.sh ]]; then
  source .env.sh
fi

# The manifest's only unconditional substitution. Unset would render an empty
# image field, which kubectl rejects without naming the cause.
if [[ -z "${LOCUST_IMAGE:-}" ]]; then
  echo "Error: LOCUST_IMAGE must be set; .terraform-env.sh exports it, and .env.sh" >&2
  echo "sources that." >&2
  exit 1
fi

JOB_MANIFEST="${BENCHMARKING_DIR}/locust/manifests/job.yaml"

# Has to match metadata.name in the manifest. Fixed rather than generated, so a
# run can be followed, waited on and deleted by name.
JOB_NAME=locust-job
NAMESPACE=benchmarking

# The WorkerPool, recreated per run so every run starts on cold workers rather
# than on whatever the last one left warm. Names have to match the manifest.
POOL_MANIFEST="${BENCHMARKING_DIR}/workloads/manifests/workloads.yaml.tmpl"
POOL_NAME=benchmark-ateom
POOL_NAMESPACE=benchmark-workloads

# ko resolves the manifest's ko:// workerImage, and both the tool wrapper and
# that import path only work inside the submodule.
SUBSTRATE_DIR="${ROOT}/dependency-repos/substrate"

# Pool shape, read off the case by jq below rather than taken as flags: the
# case is the whole run, and the pool it needs is part of that. These are the
# fallbacks for a case that states neither.
DEFAULT_WORKER_COUNT=1
DEFAULT_SANDBOX_CLASS=gvisor
WORKER_COUNT=""
SANDBOX_CLASS=""

DEFAULT_POOL_TIMEOUT_SECS=300
POOL_TIMEOUT_SECS=""

# The case a run measures when none is named, so that every run carries one:
# it is what the benchmark-test-case label records and what a series of runs is
# told apart by.
DEFAULT_TEST_CASE=glutton-baseline

# Locust user class, lowercased at the flag. Names the tests/<class>.py the
# master loads and the --user-class the boomer container is passed.
DEFAULT_USER_CLASS=runworkload

# The Job's ServiceAccount, created by infra/terraform-gke together with the
# bucket binding that names it. Has to match benchmark_runner_ksa_name there.
BENCHMARK_RUNNER_KSA="${BENCHMARK_RUNNER_KSA:-benchmark-runner}"

# How often to report that the pod has not started yet, and — without
# --follow-logs, where nothing else reports — that the run is still going.
PROGRESS_EVERY_SECS=30

# Empty means the caller did not give it, which leaves each default where it
# is defined rather than restating it here. The two timeouts are the exception,
# since nothing further down the chain holds one.
RUN_ID=""
USER_CLASS=""

# Names a case in tests.yaml, forwarded to the Job and read by runner.py
# inside the pod. It is the whole of the worker's configuration: the workload,
# its template, and every other flag the worker takes.
TEST_CASE=""
WAIT_TIMEOUT_SECS=""
POD_START_TIMEOUT_SECS=""
NO_WAIT=0
# Off by default: a run's log is kept on the finished Job for a day, so
# streaming it is for watching a run rather than for collecting it.
FOLLOW_LOGS=0
ONLY_SUMMARY=0
NO_METRICS=0
# runner.py writes every registered report when the run ends and uploads it to
# reports/<run-id>/ in the deployment's own bucket, which is where the runner's
# Workload Identity grant applies. Not optional: a run whose reports were
# skipped is one whose results have to be reconstructed from the log.
#
# An unset BUCKET_NAME leaves the reports on the pod, where the Job's TTL takes
# them with it.
BENCHMARK_REPORTS=all
BENCHMARK_REPORT_BUCKET="${BUCKET_NAME:-}"
# Has to match runner.py's --report-prefix default, which the manifest leaves.
REPORT_PREFIX=reports
# Where the uploaded reports are copied to once the run ends, one folder per
# run_id beneath it.
REPORTS_DIR="${ROOT}/reports"
NO_DOWNLOAD=0

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

DELETE=0

usage() {
  echo "Usage: $0 [options]"
  echo ""
  echo "Builds and pushes the images, recreates the WorkerPool, runs the benchmark as a"
  echo "Job, waits for it, and exits with its result. The ActorTemplates it measures are"
  echo "assumed to be deployed already."
  echo ""
  echo "The run:"
  echo "  --run-id NAME           Names this run in Prometheus, as the run_id label on"
  echo "                          every series it produces (default: a UTC timestamp)"
  echo "  --user-class NAME       Locust user class, lowercase; runs tests/NAME.py"
  echo "                          (default: runworkload)"
  echo "  --test-case NAME        The run, named as a case in benchmarking/tests.yaml"
  echo "                          (default: ${DEFAULT_TEST_CASE}). A case is the whole of"
  echo "                          it — the duration and user count as much as the"
  echo "                          workload — so another load level is another case."
  echo ""
  echo "The WorkerPool is deleted and recreated before every run, so that no run"
  echo "inherits the workers the last one warmed. Its shape comes from the case —"
  echo "\`worker-count\` (default ${DEFAULT_WORKER_COUNT}) and \`sandbox-class\` (default"
  echo "${DEFAULT_SANDBOX_CLASS}) — not from a flag, because a pool that differs is a"
  echo "different run and so is a different case."
  echo "  --pool-timeout SECONDS  Ceiling on the wait for the new pool's workers to be"
  echo "                          ready (default: ${DEFAULT_POOL_TIMEOUT_SECS})"
  echo ""
  echo "Waiting:"
  echo "  --no-wait               Submit the Job and return, instead of following it."
  echo "                          The run continues in the cluster either way."
  echo "  --follow-logs           Stream the run's logs while it runs. Without it the run"
  echo "                          is still waited on, and its log read afterwards from the"
  echo "                          finished Job: kubectl logs job/${JOB_NAME} -n ${NAMESPACE}"
  echo "  --only-summary          Pass locust --only-summary, dropping the stats table it"
  echo "                          reprints while the run goes. The aggregate it prints when"
  echo "                          the run ends is kept, so the result is unaffected."
  echo "  --wait-timeout SECONDS  Ceiling on the wait for the run (default: 3600). Reaching"
  echo "                          it stops the waiting, not the Job."
  echo "  --pod-start-timeout SECONDS"
  echo "                          Ceiling on the wait for the pod to start, which covers the"
  echo "                          image pull and the scheduling (default: 300)"
  echo "  --no-metrics            Skip the Prometheus summary printed after the run. The"
  echo "                          summary is skipped anyway where Prometheus is not"
  echo "                          deployed, or where jq or curl is missing."
  echo "  --reports-dir DIR       Where the run's reports are downloaded from the bucket,"
  echo "                          into DIR/<run-id>/ (default: ${ROOT}/reports)"
  echo "  --no-download           Leave the reports in the bucket."
  echo ""
  echo "Other:"
  echo "  --delete                Delete the Job and exit. The workloads are left alone;"
  echo "                          deploy-benchmarking.sh --delete removes those."
  echo "  -h|--help               Show this help message"
  echo ""
  echo "Deploying what a run measures is deploy-benchmarking.sh, which runs nothing:"
  echo "  ./benchmarking/deploy-benchmarking.sh --deploy"
  echo ""
  echo "Environment:"
  echo "  PROJECT_ID, BUCKET_NAME, etc. are read from .env.sh by the"
  echo "  scripts this wrapper invokes."
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --run-id) shift; RUN_ID="$1" ;;
    --run-id=*) RUN_ID="${1#*=}" ;;
    --user-class) shift; USER_CLASS="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" ;;
    --user-class=*) USER_CLASS="$(printf '%s' "${1#*=}" | tr '[:upper:]' '[:lower:]')" ;;
    --pool-timeout) shift; POOL_TIMEOUT_SECS="$1" ;;
    --pool-timeout=*) POOL_TIMEOUT_SECS="${1#*=}" ;;
    --test-case) shift; TEST_CASE="$1" ;;
    --test-case=*) TEST_CASE="${1#*=}" ;;
    --no-wait) NO_WAIT=1 ;;
    --follow-logs) FOLLOW_LOGS=1 ;;
    --only-summary) ONLY_SUMMARY=1 ;;
    --no-metrics) NO_METRICS=1 ;;
    --reports-dir) shift; REPORTS_DIR="$1" ;;
    --reports-dir=*) REPORTS_DIR="${1#*=}" ;;
    --no-download) NO_DOWNLOAD=1 ;;
    --wait-timeout) shift; WAIT_TIMEOUT_SECS="$1" ;;
    --wait-timeout=*) WAIT_TIMEOUT_SECS="${1#*=}" ;;
    --pod-start-timeout) shift; POD_START_TIMEOUT_SECS="$1" ;;
    --pod-start-timeout=*) POD_START_TIMEOUT_SECS="${1#*=}" ;;
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

# The manifest targets the benchmarking namespace, which Prometheus is also in.
# Ensured here so a run works in a cluster where monitoring was never deployed.
ensure_namespace() {
  kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
}

run_ko() {
  (cd "${SUBSTRATE_DIR}" && hack/run-tool.sh ko "$@")
}

# resolve_test_case prints the named case as JSON, by running the
# testcase-helper binary that ships in the locust image beside the tests.yaml it
# reads. Going through the image is what keeps a YAML parser off the host, which
# has kubectl and docker and not necessarily PyYAML or yq.
#
# It also moves the "no such case" failure to before the pool is rebuilt and
# the Job submitted, where it used to surface in the pod once both had happened.
resolve_test_case() {
  if ! command -v docker &>/dev/null; then
    echo "Error: docker is required to resolve --test-case, which runs the" >&2
    echo "testcase-helper binary out of ${LOCUST_IMAGE}." >&2
    return 1
  fi
  # --entrypoint because the distroless python3 base image launches python.
  # --platform suppresses the Docker warning about host/image architecture mismatch
  # when resolving the case locally on an ARM Mac.
  docker run --rm --platform "${LOCUST_IMAGE_PLATFORM:-linux/amd64}" --entrypoint /app/testcase-helper "${LOCUST_IMAGE}" --case "$1"
}

# recreate_worker_pool gives the run its own pool: the old one goes, a new one
# is applied, and the wait is for its workers to be ready.
#
# It deletes the WorkerPool by name rather than through the manifest, which also
# carries the namespace — tearing that down would take the atespace's pods and
# anything else in it along with the pool.
#
# The ActorTemplates and their golden snapshots are untouched by this. They are
# not in this manifest and their snapshots live in the bucket, so a run gets
# cold workers without paying to re-warm what it restores from.
recreate_worker_pool() {
  echo "Deleting any previous ${POOL_NAME} WorkerPool..."
  kubectl delete workerpool "${POOL_NAME}" --namespace="${POOL_NAMESPACE}" \
    --ignore-not-found --wait=true
  # The Deployment goes by owner reference, which is collected asynchronously
  # after the pool object is gone. Waiting for it keeps the apply below from
  # racing a Deployment of the same name that is still terminating.
  kubectl wait --for=delete "deployment/${POOL_NAME}" \
    --namespace="${POOL_NAMESPACE}" --timeout="${POOL_TIMEOUT_SECS}s" 2>/dev/null || true

  echo "Deploying ${POOL_NAME} WorkerPool (replicas=${WORKER_COUNT}, sandbox_class=${SANDBOX_CLASS})..."
  sed -e "s|\${WORKER_COUNT}|${WORKER_COUNT}|g" \
      -e "s|\${SANDBOX_CLASS}|${SANDBOX_CLASS}|g" \
      "${POOL_MANIFEST}" | run_ko apply -f -

  kubectl wait --for=create "deployment/${POOL_NAME}" \
    --namespace="${POOL_NAMESPACE}" --timeout="${POOL_TIMEOUT_SECS}s"
  kubectl rollout status "deployment/${POOL_NAME}" \
    --namespace="${POOL_NAMESPACE}" --timeout="${POOL_TIMEOUT_SECS}s"
}

# submit_job renders the manifest and applies it, then returns; it does not
# wait. What to do with a run in flight is decided below, not here.
submit_job() {
  ensure_namespace
  # A Job's pod template is immutable, so a second run cannot be an apply over
  # the first. Deleting also keeps the fixed name unambiguous: the logs and the
  # status under it are the run just submitted. --wait=true so the old pods are
  # gone before the new Job creates its own.
  echo "Deleting any previous ${JOB_NAME}..."
  kubectl delete job "${JOB_NAME}" --namespace="${NAMESPACE}" \
    --ignore-not-found --wait=true
  echo "Running Locust benchmark as a Job:"
  echo "  image:     ${LOCUST_IMAGE}"
  echo "  class:     ${BENCHMARK_USER_CLASS}"
  echo "  case:      ${BENCHMARK_TEST_CASE}"
  echo "  run_id:    ${BENCHMARK_RUN_ID}"
  envsubst < "${JOB_MANIFEST}" | kubectl apply -f -
}

delete_job() {
  envsubst < "${JOB_MANIFEST}" | kubectl delete --ignore-not-found -f -
}

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
  local phase job_uid

  echo "Waiting for the pod to start..."
  while ((SECONDS < deadline)); do
    if [[ -z "${job_uid:-}" ]]; then
      job_uid="$(kubectl get job "${JOB_NAME}" --namespace="${NAMESPACE}" -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
    fi
    if [[ -n "${job_uid:-}" ]]; then
      # controller-uid isolates the pod to this specific Job run, ignoring
      # terminating pods from the previous run.
      phase="$(kubectl get pods --namespace="${NAMESPACE}" -l "controller-uid=${job_uid}" \
        -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true)"
      case "${phase}" in
        Running|Succeeded|Failed) return 0 ;;
      esac
    fi
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

# follow_run waits for the run to end and returns what the Job did.
#
# With --follow-logs the stream is the wait: kubectl logs -f returns when the
# container exits, so the output arrives as it is produced. Without it the
# status poll below is the wait on its own. Either way the result is read from
# the Job rather than inferred from the wait ending, which says that the run is
# over and not how it went — and a stream that broke early says nothing at all.
follow_run() {
  local deadline=$((SECONDS + WAIT_TIMEOUT_SECS))
  local started=${SECONDS}
  local next_report=$((SECONDS + PROGRESS_EVERY_SECS))
  local succeeded failed

  wait_for_pod || return 1

  if [[ "${FOLLOW_LOGS}" -eq 1 ]]; then
    echo
    echo "=== Run log ==="
    # Not fatal on its own: a dropped stream is a lost log, not a failed run,
    # and the status below is what decides.
    kubectl logs -f "job/${JOB_NAME}" --namespace="${NAMESPACE}" || true
    echo "=== End of run log ==="
    echo
  else
    echo "Waiting for the run to finish (--follow-logs streams it as it goes)..."
  fi

  while ((SECONDS < deadline)); do
    IFS='|' read -r succeeded failed < <(job_status)
    if [[ "${succeeded:-0}" -ge 1 ]]; then
      echo "${JOB_NAME} completed."
      if [[ "${FOLLOW_LOGS}" -eq 0 ]]; then
        echo "  kubectl logs job/${JOB_NAME} -n ${NAMESPACE}"
      fi
      return 0
    fi
    if [[ "${failed:-0}" -ge 1 ]]; then
      if [[ "${FOLLOW_LOGS}" -eq 1 ]]; then
        echo "Error: ${JOB_NAME} failed. The log above is the run." >&2
      else
        echo "Error: ${JOB_NAME} failed. Its log is the run:" >&2
        echo "  kubectl logs job/${JOB_NAME} -n ${NAMESPACE}" >&2
      fi
      return 1
    fi
    # Nothing else reports without the stream, so a long run would otherwise
    # be silent from here to its deadline.
    if [[ "${FOLLOW_LOGS}" -eq 0 ]] && ((SECONDS >= next_report)); then
      echo "  $((SECONDS - started))s/${WAIT_TIMEOUT_SECS}s — still running"
      next_report=$((SECONDS + PROGRESS_EVERY_SECS))
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
    echo "  kubectl apply -f benchmarking/monitoring/prometheus.yaml"
    echo "  adds it for the runs after it, without retaking the golden snapshots that"
    echo "  deploy-benchmarking.sh --deploy would."
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

  # The scale gauges, read as the high-water mark rather than as the last
  # sample. The counters above are cumulative, so their final value is the
  # run's total; a gauge ends the run at zero, because teardown suspends and
  # deletes every actor before the worker exits. The peak is what a run says
  # about scale, and the average beside it tells a brief spike from a level
  # the run held — over the whole window, so the ramp up is in it.
  #
  # Collapsed with max() instead of broken out per workload: a Job bakes one
  # workload into its pod, so there is only one series, and the scrape renames
  # the gauge's own `workload` label to `exported_workload` in any case — the
  # pod carries a target label of that name, and the target wins.
  local scale scale_query="" spec agg
  for spec in active:boomer_active_actors suspended:boomer_suspended_actors; do
    for agg in peak:max_over_time avg:avg_over_time; do
      [[ -n "${scale_query}" ]] && scale_query+=" or "
      scale_query+="label_replace(label_replace("
      scale_query+="max(${agg##*:}(${spec##*:}{run_id=\"${run_id}\"}[${METRICS_WINDOW}]))"
      scale_query+=", \"gauge\", \"${spec%%:*}\", \"\", \"\")"
      scale_query+=", \"stat\", \"${agg%%:*}\", \"\", \"\")"
    done
  done
  scale="$(promql "${scale_query}" \
    '.data.result[] | [.metric.gauge, .metric.stat, .value[1]] | @tsv' \
    || true)"

  if [[ -n "${scale}" ]]; then
    echo
    printf '  %-18s %-6s %s\n' "SCALE" "" "ACTORS"
    sort <<<"${scale}" | while IFS=$'\t' read -r gauge stat value; do
      if [[ "${value}" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        LC_ALL=C printf '  %-18s %-6s %.1f\n' "${gauge}" "${stat}" "${value}"
      else
        printf '  %-18s %-6s %s\n' "${gauge}" "${stat}" "-"
      fi
    done
  else
    echo
    echo "  No scale gauges for this run. They are registered by the runworkload"
    echo "  user class alone, and by a worker image built since they were added,"
    echo "  so either explains a run whose requests above came through intact."
  fi

  echo
  echo "  Prometheus holds this until its pod restarts. Query it with:"
  echo "    kubectl port-forward -n ${NAMESPACE} svc/prometheus 9090:9090"
  echo "    locust_requests_total{run_id=\"${run_id}\"}"
  stop_port_forward
}

# download_reports copies what runner.py uploaded for one run into
# REPORTS_DIR/<run-id>/. Best-effort like the metrics summary: the reports stay
# in the bucket either way, so a failed copy is only a command to rerun.
download_reports() {
  local run_id="$1"
  local src="gs://${BENCHMARK_REPORT_BUCKET}/${REPORT_PREFIX}/${run_id}"

  if [[ -z "${BENCHMARK_REPORT_BUCKET}" ]]; then
    echo "Skipping the report download: BUCKET_NAME is unset, so nothing was uploaded."
    return 0
  fi
  if ! command -v gcloud &>/dev/null; then
    echo "Skipping the report download: gcloud is not installed."
    echo "  The reports are in ${src}/"
    return 0
  fi

  echo
  echo "=== Downloading reports ==="
  mkdir -p "${REPORTS_DIR}"
  if gcloud storage cp --recursive "${src}" "${REPORTS_DIR}/"; then
    echo "  ${REPORTS_DIR}/${run_id}/"
    echo "  cd ${REPORTS_DIR}/${run_id} && python3 -m benchmark_charts ."
  else
    echo "  Could not download ${src}/; to retry:" >&2
    echo "    gcloud storage cp --recursive ${src} ${REPORTS_DIR}/" >&2
  fi
}

BENCHMARK_USER_CLASS="${USER_CLASS:-${DEFAULT_USER_CLASS}}"
if [[ ! -f "${BENCHMARKING_DIR}/locust/tests/${BENCHMARK_USER_CLASS}.py" ]]; then
  echo "Error: no locust/tests/${BENCHMARK_USER_CLASS}.py; --user-class must name a test file" >&2
  exit 1
fi

# Exported for envsubst, which reads the environment rather than arguments.
export LOCUST_IMAGE BENCHMARK_USER_CLASS BENCHMARK_RUNNER_KSA
export BENCHMARK_REPORTS BENCHMARK_REPORT_BUCKET

if [[ "${DELETE}" -eq 1 ]]; then
  # The manifest renders with whatever is set, because delete matches on the
  # name and kind alone; the run-shaped values below are not needed for it.
  export BENCHMARK_RUN_ID="" BENCHMARK_TEST_CASE="" BENCHMARK_ONLY_SUMMARY=false
  echo "=== Deleting the locust Job ==="
  delete_job
  exit 0
fi

WAIT_TIMEOUT_SECS="${WAIT_TIMEOUT_SECS:-${DEFAULT_WAIT_TIMEOUT_SECS}}"
POD_START_TIMEOUT_SECS="${POD_START_TIMEOUT_SECS:-${DEFAULT_POD_START_TIMEOUT_SECS}}"
POOL_TIMEOUT_SECS="${POOL_TIMEOUT_SECS:-${DEFAULT_POOL_TIMEOUT_SECS}}"
for pair in "--wait-timeout:${WAIT_TIMEOUT_SECS}" \
            "--pod-start-timeout:${POD_START_TIMEOUT_SECS}" \
            "--pool-timeout:${POOL_TIMEOUT_SECS}"; do
  if ! [[ "${pair#*:}" =~ ^[0-9]+$ ]]; then
    echo "Error: ${pair%%:*} must be a whole number, got '${pair#*:}'" >&2
    exit 1
  fi
done

# Required rather than best-effort, unlike the metrics summary further down:
# the pool's shape is read off the case with it, so a run cannot start without.
if ! command -v jq &>/dev/null; then
  echo "Error: jq is required to read the pool's shape out of the test case." >&2
  exit 1
fi

if [[ ! -f "${SUBSTRATE_DIR}/go.mod" ]]; then
  echo "Error: substrate submodule not initialized at ${SUBSTRATE_DIR}; the" >&2
  echo "WorkerPool manifest's ko:// workerImage cannot be resolved without it." >&2
  echo "Run: git submodule update --init dependency-repos/substrate" >&2
  exit 1
fi
if [[ ! -f "${POOL_MANIFEST}" ]]; then
  echo "Error: ${POOL_MANIFEST} not found." >&2
  exit 1
fi

# A timestamp rather than a random string, so the ids of a series of runs sort
# into the order the runs happened in. Seconds are enough: the Job it names can
# only exist one at a time.
if [[ -z "${RUN_ID}" ]]; then
  RUN_ID="$(date -u +%Y%m%dt%H%M%Sz)"
fi
# Checked here rather than left to the API server, which would reject the whole
# manifest for it and say so in terms of label syntax rather than of the flag
# that produced it.
if ! [[ "${RUN_ID}" =~ ^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$ ]]; then
  echo "Error: --run-id must be 1-63 alphanumerics, dashes, underscores or dots," >&2
  echo "starting and ending with an alphanumeric. Got '${RUN_ID}'" >&2
  exit 1
fi

export BENCHMARK_RUN_ID="${RUN_ID}"
export BENCHMARK_TEST_CASE="${TEST_CASE:-${DEFAULT_TEST_CASE}}"
# locust rejects an empty --only-summary rather than reading it as false, so
# this is never blank.
if [[ "${ONLY_SUMMARY}" -eq 1 ]]; then
  export BENCHMARK_ONLY_SUMMARY=true
else
  export BENCHMARK_ONLY_SUMMARY=false
fi

# Ahead of the resolve below, not merely ahead of the pool: the case is read
# out of the locust image, so building first is what makes a run measure the
# tests.yaml in the working tree rather than the one the last build baked in.
echo
echo "=== Building and pushing images ==="
"${BENCHMARKING_DIR}/locust/build_and_push.sh"

echo
echo "=== Resolving test case ==="
TEST_CASE_JSON="$(resolve_test_case "${BENCHMARK_TEST_CASE}")"
echo "  ${BENCHMARK_TEST_CASE}: ${TEST_CASE_JSON}"

# The pool keys, defaulted here rather than in the file so a case states only
# what it means to change. runner.py drops the same two before the rest of the
# case becomes worker flags.
WORKER_COUNT="$(jq -r --argjson d "${DEFAULT_WORKER_COUNT}" '."worker-count" // $d' <<<"${TEST_CASE_JSON}")"
SANDBOX_CLASS="$(jq -r --arg d "${DEFAULT_SANDBOX_CLASS}" '."sandbox-class" // $d' <<<"${TEST_CASE_JSON}")"

if ! [[ "${WORKER_COUNT}" =~ ^[0-9]+$ ]]; then
  echo "Error: case ${BENCHMARK_TEST_CASE}: \`worker-count\` must be a whole number, got '${WORKER_COUNT}'" >&2
  exit 1
fi
case "${SANDBOX_CLASS}" in
  gvisor|microvm) ;;
  *)
    echo "Error: case ${BENCHMARK_TEST_CASE}: \`sandbox-class\` must be gvisor or microvm, got '${SANDBOX_CLASS}'" >&2
    exit 1
    ;;
esac

echo
echo "=== Recreating the WorkerPool ==="
recreate_worker_pool

echo
echo "=== Running the benchmark Job ==="
submit_job

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

if [[ "${NO_DOWNLOAD}" -eq 0 ]]; then
  download_reports "${RUN_ID}" || true
fi

exit "${run_status}"
