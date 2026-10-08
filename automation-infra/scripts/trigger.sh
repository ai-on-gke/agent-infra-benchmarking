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

# Starts one execution of a deployment's Cloud Run job with a plain HTTP
# request to the Cloud Run Admin API, as any external caller would, then
# prints the execution's log as it comes in and exits with its result. The
# caller needs run.jobs.run on the job, e.g. roles/run.invoker, and to read
# logs, e.g. roles/logging.viewer; the token is the gcloud account's.
#
# Stopping the script leaves the execution running.
#
#   trigger.sh DEPLOYMENT

set -o errexit -o nounset -o pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 DEPLOYMENT" >&2
  exit 2
fi
MAIN_TF="$(cd "$(dirname "$0")/../deployments" && pwd)/$1/main.tf"
if [[ ! -f "${MAIN_TF}" ]]; then
  echo "Error: no ${MAIN_TF}" >&2
  exit 1
fi

# The module's arguments, as main.tf sets them.
module_arg() {
  sed -n "s/^ *$1 *= *\"\(.*\)\"/\1/p" "${MAIN_TF}"
}
PROJECT_ID="$(module_arg project_id)"
REGION="$(module_arg region)"
JOB="$(module_arg default_resource_name)"
if [[ -z "${PROJECT_ID}" || -z "${REGION}" || -z "${JOB}" ]]; then
  echo "Error: ${MAIN_TF} does not set project_id, region and default_resource_name" >&2
  exit 1
fi

POLL_SECS=10

TOKEN="$(gcloud auth print-access-token)"

OPERATION="$(curl --silent --show-error --fail-with-body -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  "https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}:run")"
# The operation's metadata is the execution it started.
EXECUTION="$(jq -r '.metadata.name // empty' <<<"${OPERATION}")"
if [[ -z "${EXECUTION}" ]]; then
  echo "Error: no execution in the response:" >&2
  echo "${OPERATION}" >&2
  exit 1
fi
EXECUTION="${EXECUTION##*/}"
echo "Started ${EXECUTION}."

LOG_FILTER="resource.type=\"cloud_run_job\" AND labels.\"run.googleapis.com/execution_name\"=\"${EXECUTION}\""
LAST_TIMESTAMP="1970-01-01T00:00:00Z"

# Prints the log lines written since the last ones printed.
print_new_logs() {
  local entries timestamp text
  entries="$(gcloud logging read "${LOG_FILTER} AND timestamp>\"${LAST_TIMESTAMP}\"" \
    --project="${PROJECT_ID}" --order=asc --format='value(timestamp,textPayload)')"
  [[ -n "${entries}" ]] || return 0
  while IFS=$'\t' read -r timestamp text; do
    printf '%s\n' "${text}"
    LAST_TIMESTAMP="${timestamp}"
  done <<<"${entries}"
}

# Prints "True" once the execution succeeded and "False" once it failed or was
# cancelled; nothing while it runs.
completed() {
  gcloud run jobs executions describe "${EXECUTION}" --project="${PROJECT_ID}" --region="${REGION}" \
    --format=json | jq -r '.status.conditions[]? | select(.type == "Completed" and .status != "Unknown") | .status'
}

while true; do
  print_new_logs
  result="$(completed)"
  [[ -z "${result}" ]] || break
  sleep "${POLL_SECS}"
done
# Logging lags the execution by a few seconds.
sleep "${POLL_SECS}"
print_new_logs

if [[ "${result}" == True ]]; then
  echo "${EXECUTION} succeeded."
else
  echo "${EXECUTION} failed:" >&2
  echo "  gcloud run jobs executions describe ${EXECUTION} --project=${PROJECT_ID} --region=${REGION}" >&2
  exit 1
fi
