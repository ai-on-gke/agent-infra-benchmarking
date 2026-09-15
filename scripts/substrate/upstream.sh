#!/usr/bin/env bash

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

# Runs an executable that belongs to the substrate submodule with that
# submodule as the working directory. Upstream's scripts resolve their own
# paths against the repo root they live in (go.mod, hack/, manifests/,
# .ate-dev-env.sh), so invoking them from this repo's root silently picks up
# the wrong tree — or nothing at all. This wrapper fixes the cwd so callers
# here can reuse upstream tooling verbatim instead of forking it.

set -o errexit -o nounset -o pipefail

# Derived from this file's location rather than `git rev-parse --show-toplevel`,
# which returns the submodule's own root when the caller is already inside it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SUBSTRATE_DIR="${ROOT}/dependency-repos/substrate"

usage() {
  echo "Usage: $0 <executable> [args...]"
  echo ""
  echo "Runs <executable> from the substrate submodule with"
  echo "dependency-repos/substrate as the working directory. All remaining"
  echo "arguments are passed through unchanged."
  echo ""
  echo "<executable> is resolved, in order, as:"
  echo "  1. an absolute path"
  echo "  2. a path relative to the substrate root (the usual form)"
  echo "  3. a path relative to the current directory"
  echo "It must resolve to a file inside the submodule."
  echo ""
  echo "Examples:"
  echo "  $0 hack/install-ate.sh --help"
  echo "  $0 hack/run-tool.sh ko version"
}

if [[ "$#" -lt 1 ]]; then
  usage >&2
  exit 1
fi

case "$1" in
  -h|--help)
    usage
    exit 0
    ;;
esac

if [[ ! -f "${SUBSTRATE_DIR}/go.mod" ]]; then
  echo "Error: substrate submodule not initialized at ${SUBSTRATE_DIR}." >&2
  echo "Run: git submodule update --init dependency-repos/substrate" >&2
  exit 1
fi

TARGET_ARG="$1"
shift

# Resolved to an absolute path before the cd, because a relative path means
# something different on the other side of it.
TARGET=""
if [[ "${TARGET_ARG}" == /* ]]; then
  TARGET="${TARGET_ARG}"
elif [[ -f "${SUBSTRATE_DIR}/${TARGET_ARG}" ]]; then
  TARGET="${SUBSTRATE_DIR}/${TARGET_ARG}"
elif [[ -f "${TARGET_ARG}" ]]; then
  TARGET="$(cd "$(dirname "${TARGET_ARG}")" && pwd)/$(basename "${TARGET_ARG}")"
else
  echo "Error: no such file: ${TARGET_ARG}" >&2
  echo "Looked in ${SUBSTRATE_DIR} and in $(pwd)." >&2
  exit 1
fi

if [[ ! -f "${TARGET}" ]]; then
  echo "Error: no such file: ${TARGET}" >&2
  exit 1
fi

# A target outside the submodule would run with a working directory it does not
# expect, which is the one failure this wrapper exists to prevent. Catch it here
# rather than let the callee fail somewhere further in.
TARGET_REAL="$(realpath "${TARGET}")"
SUBSTRATE_REAL="$(realpath "${SUBSTRATE_DIR}")"
if [[ "${TARGET_REAL}" != "${SUBSTRATE_REAL}/"* ]]; then
  echo "Error: ${TARGET_ARG} resolves to ${TARGET_REAL}, which is outside" >&2
  echo "${SUBSTRATE_REAL}. This wrapper only runs upstream executables." >&2
  exit 1
fi

if [[ ! -x "${TARGET_REAL}" ]]; then
  echo "Error: ${TARGET_ARG} is not executable." >&2
  exit 1
fi

cd "${SUBSTRATE_DIR}"

# Only announce the run when a human is watching: callers that capture the
# target's output (hack/run-tool.sh --print-bin-path, ko resolving manifests to
# stdout) merge stderr often enough that unconditional chatter would corrupt it.
if [[ -t 2 ]]; then
  echo "Running ${TARGET_REAL#"${SUBSTRATE_REAL}/"} in ${SUBSTRATE_DIR}" >&2
fi

# Run as a child process rather than replacing this shell, and hand back what
# it exited with. The `|| status=$?` is what keeps errexit from aborting here
# on a non-zero exit, which would lose the distinction between the target's
# status and this wrapper's own failures.
status=0
"${TARGET_REAL}" "$@" || status=$?
exit "${status}"
