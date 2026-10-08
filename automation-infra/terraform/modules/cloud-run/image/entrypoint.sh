#!/usr/bin/env bash

# Extracts the tarball of SOURCE_BRANCH's latest commit, which
# .github/workflows/upload-source.yml uploads to SOURCE_BUCKET, and cds into
# it. Ignores its arguments.

set -o errexit -o nounset -o pipefail

: "${SOURCE_BUCKET:?}" "${SOURCE_BRANCH:?}"
SOURCE_URL="https://storage.googleapis.com/${SOURCE_BUCKET}/source/${SOURCE_BRANCH}"

# ACCESS_TOKEN stands in for the job's account when run outside Cloud Run.
if [ -z "${ACCESS_TOKEN:-}" ]; then
  ACCESS_TOKEN="$(curl -sSf -H 'Metadata-Flavor: Google' \
    http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token |
    sed -n 's/.*"access_token" *: *"\([^"]*\)".*/\1/p')"
fi

fetch() {
  curl -sSfL -H "Authorization: Bearer ${ACCESS_TOKEN}" "$1"
}

COMMIT="$(fetch "${SOURCE_URL}/latest.txt")"
echo "Fetching ${SOURCE_BRANCH} at ${COMMIT}."

mkdir -p /source
fetch "${SOURCE_URL}/${COMMIT}.tar.gz" | tar -xzf - -C /source
cd /source

# Nothing to run yet.
echo "Source of ${COMMIT} is in $(pwd)."
