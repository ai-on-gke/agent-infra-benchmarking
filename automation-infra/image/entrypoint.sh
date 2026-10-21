#!/usr/bin/env bash

# Runs the scenario-runner in the repo the image holds, with its arguments; or,
# when they start with destroy-leftovers, that tool with the rest.

set -o errexit -o nounset -o pipefail

cd /source
git log -1

# The cleanup job's: destroy what killed executions left, rather than run.
if [[ "${1:-}" == destroy-leftovers ]]; then
  shift
  exec automation-infra/tools/destroy-leftovers "$@"
fi
exec benchmarking/tools/run "$@"
