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

# Starts one execution of this deployment's Cloud Run job with a plain HTTP
# request to the Cloud Run Admin API, as any external caller would, and prints
# the operation it returns. The caller needs run.jobs.run on the job, e.g.
# roles/run.invoker; the token is the gcloud account's.

set -o errexit -o nounset -o pipefail

# Have to match main.tf.
PROJECT_ID=akvelon-gke-aieco
REGION=us-central1
JOB=agent-substrate-bench-nightly

TOKEN="$(gcloud auth print-access-token)"

curl --silent --show-error --fail-with-body -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  "https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}:run"
echo
