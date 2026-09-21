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
USER_CLASS=""
WORKLOAD_NAME=""
WORKLOAD_TEMPLATE=""
WAIT_TIMEOUT_SECS=""
POD_START_TIMEOUT_SECS=""
NO_WAIT=0

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
DELETE=0

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
  echo ""
  echo "Setup, which runs nothing. Either one ends the command when its work is done,"
  echo "so a run is started separately afterwards:"
  echo "  --build                 Build and push the locust image, then exit"
  echo "  --deploy-workloads      Deploy the benchmark workloads, then exit. Redeploying"
  echo "                          retakes the golden snapshots, so runs that straddle it"
  echo "                          are not comparable; do it once, before a series."
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
    --user-class) shift; USER_CLASS="$1" ;;
    --user-class=*) USER_CLASS="${1#*=}" ;;
    --workload-name) shift; WORKLOAD_NAME="$1" ;;
    --workload-name=*) WORKLOAD_NAME="${1#*=}" ;;
    --workload-template) shift; WORKLOAD_TEMPLATE="$1" ;;
    --workload-template=*) WORKLOAD_TEMPLATE="${1#*=}" ;;
    --no-wait) NO_WAIT=1 ;;
    --wait-timeout) shift; WAIT_TIMEOUT_SECS="$1" ;;
    --wait-timeout=*) WAIT_TIMEOUT_SECS="${1#*=}" ;;
    --pod-start-timeout) shift; POD_START_TIMEOUT_SECS="$1" ;;
    --pod-start-timeout=*) POD_START_TIMEOUT_SECS="${1#*=}" ;;
    --build) BUILD=1 ;;
    --deploy-workloads) DEPLOY_WORKLOADS=1 ;;
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
if [[ "${BUILD}" -eq 1 || "${DEPLOY_WORKLOADS}" -eq 1 ]]; then
  for pair in "--duration:${DURATION}" \
              "--users:${USERS}" \
              "--user-class:${USER_CLASS}" \
              "--workload-name:${WORKLOAD_NAME}" \
              "--workload-template:${WORKLOAD_TEMPLATE}" \
              "--wait-timeout:${WAIT_TIMEOUT_SECS}" \
              "--pod-start-timeout:${POD_START_TIMEOUT_SECS}"; do
    if [[ -n "${pair#*:}" ]]; then
      echo "Error: ${pair%%:*} describes a run, and --build/--deploy-workloads only set up" >&2
      echo "for one. Run this again without it, then start the run on its own:" >&2
      echo "  $0 ${pair%%:*} ${pair#*:}" >&2
      exit 1
    fi
  done
  if [[ "${NO_WAIT}" -eq 1 ]]; then
    echo "Error: --no-wait describes a run, and --build/--deploy-workloads start none." >&2
    exit 1
  fi

  if [[ "${BUILD}" -eq 1 ]]; then
    echo
    echo "=== Building and pushing locust image ==="
    "${BENCHMARKING_DIR}/locust/build_and_push.sh"
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

echo
echo "=== Running the benchmark Job ==="
job_args=(--deploy-job)
[[ -n "${DURATION}" ]] && job_args+=(--duration "${DURATION}")
[[ -n "${USERS}" ]] && job_args+=(--users "${USERS}")
[[ -n "${USER_CLASS}" ]] && job_args+=(--user-class "${USER_CLASS}")
[[ -n "${WORKLOAD_NAME}" ]] && job_args+=(--workload-name "${WORKLOAD_NAME}")
[[ -n "${WORKLOAD_TEMPLATE}" ]] && job_args+=(--workload-template "${WORKLOAD_TEMPLATE}")
"${BENCHMARKING_DIR}/locust/deploy.sh" "${job_args[@]}"

if [[ "${NO_WAIT}" -eq 1 ]]; then
  exit 0
fi

follow_run
