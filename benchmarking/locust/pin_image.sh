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

# Prints IMAGE pinned to the digest its registry serves for its tag right now,
# as repo@sha256:..., so what reads the image and what runs it are the same
# bits even if the tag moves in between. An image already given by digest is
# printed unchanged.

set -o errexit -o nounset -o pipefail

if [[ "$#" -ne 1 ]]; then
  echo "Usage: $0 IMAGE" >&2
  exit 1
fi
image="$1"

if [[ "${image}" == *@sha256:* ]]; then
  echo "${image}"
  exit 0
fi
if ! command -v jq &>/dev/null; then
  echo "Error: jq is required to read the digest of ${image}." >&2
  exit 1
fi

# The tag is after the last colon unless that colon is a registry port.
repo="${image}"
if [[ "${image##*:}" != */* ]]; then
  repo="${image%:*}"
fi

if ! digest="$(docker buildx imagetools inspect "${image}" --format '{{json .Manifest}}' | jq -r '.digest')" ||
  [[ "${digest}" != sha256:* ]]; then
  echo "Error: could not read the digest of ${image} from its registry; is it pushed?" >&2
  exit 1
fi
echo "${repo}@${digest}"
