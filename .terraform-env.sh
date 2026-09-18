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
# There is nothing to configure here for that case; .env.sh stops sourcing this
# file and spells the same variables out by hand instead (see the bottom of
# env.sh.example).
#
# Requires terraform and jq, and a terraform apply that has already run. The
# lookup happens once per shell that sources this file. TF_CLUSTER_KEY, from
# the environment, picks the cluster when the deployment has more than one.

# Derived from this file's own location, so the lookup finds the deployment
# whatever directory the sourcing script runs from.
_TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/infra/terraform-gke"

# One call, reused for every lookup below: terraform output is slow enough that
# a call per variable is noticeable on every script that sources this file.
if ! _TF_OUT="$(terraform -chdir="${_TF_DIR}" output -json 2>/dev/null)" ||
  [[ -z "${_TF_OUT}" || "${_TF_OUT}" == "{}" ]]; then
  echo "Error: no Terraform outputs in ${_TF_DIR}." >&2
  echo "Run the steps in README.md (Setting up the infrastructure) first, or" >&2
  echo "fill the values in by hand as env.sh.example describes." >&2
  return 1 2>/dev/null || exit 1
fi

# Which entry of the clusters map the benchmark runs on, as the key from
# terraform.tfvars rather than the final cluster name - Terraform prefixes that
# with default_resource_name, and CLUSTER_NAME below is the prefixed one. A
# deployment with one cluster needs no choice, so only several ask for it.
if [[ -z "${TF_CLUSTER_KEY:-}" ]]; then
  TF_CLUSTER_KEY="$(jq -r '.cluster_names.value | if length == 1 then (keys | first) else empty end' <<<"${_TF_OUT}")"
fi
if [[ -z "${TF_CLUSTER_KEY}" ]]; then
  echo "Error: this deployment has more than one cluster, so TF_CLUSTER_KEY has" >&2
  echo "to name the one .env.sh points at. Known keys:" >&2
  echo "  $(jq -r '.cluster_names.value | keys | join(", ")' <<<"${_TF_OUT}")" >&2
  return 1 2>/dev/null || exit 1
fi

# Reads one value, and stops rather than exporting an empty string. An output
# added to outputs.tf after this deployment's last apply is absent until the
# next one, and a silently empty BUCKET_NAME points the benchmark at the wrong
# object store instead of failing.
_tf_value() {
  local expr="$1" label="$2" value
  value="$(jq -r --arg k "${TF_CLUSTER_KEY}" "${expr} // empty" <<<"${_TF_OUT}")"
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
# Clusters from this module are regional, so their location is the region. A
# zone here (as in env.sh.example's hand-filled block) would describe a zonal
# cluster instead.
export CLUSTER_LOCATION="${GCE_REGION}"

CLUSTER_NAME="$(_tf_value '.cluster_names.value[$k]' "a cluster named '${TF_CLUSTER_KEY}'")" || return 1 2>/dev/null || exit 1
export CLUSTER_NAME

BUCKET_NAME="$(_tf_value '.bucket_name.value' 'bucket_name')" || return 1 2>/dev/null || exit 1
export BUCKET_NAME

# The Artifact Registry repository Terraform created, as an image path prefix.
# Note this is a different repository from the gcr.io compatibility one that
# env.sh.example's hand-filled block pushes to: images built against this file
# land in the repo this deployment owns, which is the one its clusters are
# granted to pull from.
DOCKER_REPO="$(_tf_value '.artifact_registry_docker_repo.value' 'artifact_registry_docker_repo')" || return 1 2>/dev/null || exit 1
export DOCKER_REPO
export KO_DOCKER_REPO="${DOCKER_REPO}"

# The two images this repo builds, named here rather than in the scripts that
# push them so that a build and a deployment cannot disagree about where the
# image lives.
export LOCUST_IMAGE="${DOCKER_REPO}/locust-test:latest"
export OPENCLAW_IMAGE="${DOCKER_REPO}/openclaw-workload:latest"

unset -f _tf_value
unset _TF_DIR _TF_OUT
