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

# The task image of a swebench case whose dataset is `images`: nothing but a
# Debian userland with a shell and coreutils, which swebench-loader's built-in
# workloads (workload-file-read-one runs `true` and reads /etc/os-release) need
# and the environments' guest is mounted into. It exists so that the images
# the case runs are in the repository of the run, pulled from the same place
# as the others, and not a public image another registry may rate-limit.
#
# The base image is pinned by digest, the tag beside it saying which one it
# was taken from, so a rebuild of the same commit is the same image.

FROM python:3.12.15-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
LABEL org.opencontainers.image.description="swebench case smoke rootfs"
