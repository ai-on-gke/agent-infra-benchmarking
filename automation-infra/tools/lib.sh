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

# Sourced by the tools, not run.

# Takes the required --deployment=NAME, or --deployment NAME, from anywhere in
# the arguments, setting DEPLOYMENT, DEPLOYMENT_DIR and ARGS, the arguments
# left. Without it, calls the sourcing script's usage.
parse_deployment() {
  DEPLOYMENT=""
  ARGS=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --deployment=*) DEPLOYMENT="${1#*=}" ;;
      --deployment)
        [[ $# -ge 2 ]] || usage
        DEPLOYMENT="$2"
        shift
        ;;
      *) ARGS+=("$1") ;;
    esac
    shift
  done
  [[ -n "${DEPLOYMENT}" ]] || usage

  DEPLOYMENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../deployments" && pwd)/${DEPLOYMENT}"
  if [[ ! -f "${DEPLOYMENT_DIR}/main.tf" ]]; then
    echo "Error: no ${DEPLOYMENT_DIR}/main.tf" >&2
    exit 1
  fi
}
