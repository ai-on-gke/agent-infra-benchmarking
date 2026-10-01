# shellcheck shell=bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# The settings that describe the deployment itself - the project, the region,
# the cluster, the bucket and the image repository - read off the Terraform
# state in infra/terraform-gke rather than typed in. Sourced by .env.sh.
#
# Nothing here is per-developer and nothing here is edited: every value comes
# from `terraform output`, so a deployment changes this file's answers without
# changing the file. The settings that do get edited live in .env.sh, copied
# from env.sh.example.
#
# A cluster that did not come from infra/terraform-gke has no outputs to read.
# For that case, replace the line in .env.sh that sources this file with exports
# of the variables below.
#
# Requires terraform and jq, and a terraform apply that has already run. The
# lookup happens once per shell that sources this file.

# Derived from this file's own location, so the lookup finds the deployment
# whatever directory the sourcing script runs from. See env.sh.example for why
# this isn't just BASH_SOURCE[0]: zsh's 1-indexed arrays leave it empty there.
if [ -n "${ZSH_VERSION:-}" ]; then
  _TF_SELF="$(eval 'print -r -- "${(%):-%x}"')"
else
  _TF_SELF="${BASH_SOURCE[0]}"
fi
_TF_DIR="$(cd "$(dirname "${_TF_SELF}")" && pwd)/infra/terraform-gke"
unset _TF_SELF

# One call, reused for every lookup below: terraform output is slow enough that
# a call per variable is noticeable on every script that sources this file.
# State kept in GCS needs application-default credentials and a terraform init
# against the backend to read. Terraform's own error says which is missing,
# where "no outputs" would not.
_TF_ERR="$(mktemp)"
if ! _TF_OUT="$(terraform -chdir="${_TF_DIR}" output -json -no-color 2>"${_TF_ERR}")"; then
  echo "Error: terraform output failed in ${_TF_DIR}:" >&2
  cat "${_TF_ERR}" >&2
  rm -f "${_TF_ERR}"
  return 1 2>/dev/null || exit 1
fi
rm -f "${_TF_ERR}"
if [[ -z "${_TF_OUT}" || "${_TF_OUT}" == "{}" ]]; then
  echo "Error: no Terraform outputs in ${_TF_DIR}." >&2
  echo "Run the steps in README.md (Setting up the infrastructure) first. For a" >&2
  echo "cluster Terraform did not create, export the variables this file sets in" >&2
  echo ".env.sh instead of sourcing it." >&2
  return 1 2>/dev/null || exit 1
fi

# Reads one value, and stops rather than exporting an empty string. An output
# added to outputs.tf after this deployment's last apply is absent until the
# next one, and a silently empty BUCKET_NAME points the benchmark at the wrong
# object store instead of failing.
_tf_value() {
  local expr="$1" label="$2" value
  value="$(jq -r "${expr} // empty" <<<"${_TF_OUT}")"
  if [[ -z "${value}" ]]; then
    echo "Error: Terraform published no ${label}." >&2
    echo "Outputs added since the last apply appear only after the next one:" >&2
    echo "  terraform -chdir=infra/terraform-gke apply" >&2
    return 1
  fi
  printf '%s\n' "${value}"
}

PROJECT_ID="$(_tf_value '.project_id.value' 'project_id')" || return 1 2>/dev/null || exit 1
export PROJECT_ID

# Not a Terraform output, but a lookup on the project one: the value follows
# PROJECT_ID, so it is resolved next to it rather than left for every script
# that needs it. A failed lookup stops here, for the same reason an empty
# output does.
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")" ||
  return 1 2>/dev/null || exit 1
export PROJECT_NUMBER

GCE_REGION="$(_tf_value '.region.value' 'region')" || return 1 2>/dev/null || exit 1
export GCE_REGION
# The cluster from this module is regional, so its location is the region. A
# zone here would describe a zonal cluster instead.
export CLUSTER_LOCATION="${GCE_REGION}"

CLUSTER_NAME="$(_tf_value '.cluster_name.value' 'cluster_name')" || return 1 2>/dev/null || exit 1
export CLUSTER_NAME

BUCKET_NAME="$(_tf_value '.bucket_name.value' 'bucket_name')" || return 1 2>/dev/null || exit 1
export BUCKET_NAME

# The Artifact Registry repository Terraform created, as an image path prefix.
# Images built against this file land in the repository this deployment owns,
# which is the one its cluster is granted to pull from.
DOCKER_REPO="$(_tf_value '.artifact_registry_docker_repo.value' 'artifact_registry_docker_repo')" || return 1 2>/dev/null || exit 1
export DOCKER_REPO
export KO_DOCKER_REPO="${DOCKER_REPO}"

# The images this repo builds, named here rather than in the scripts that push
# them so that a build and a deployment cannot disagree about where the image
# lives.
export LOCUST_IMAGE="${DOCKER_REPO}/locust-test:latest"
export OPENCLAW_IMAGE="${DOCKER_REPO}/openclaw-workload:latest"
export MOCK_LLM_IMAGE="${DOCKER_REPO}/mock-llm:latest"

unset -f _tf_value
unset _TF_DIR _TF_OUT
