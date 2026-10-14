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

# The rootfs of the sandbox-benchmark's environments, which env runs
# unmodified with its guest mounted beside it: stock Python with what the
# tests import already installed. env gives its actors no egress policy, so
# an environment cannot reach PyPI; pip.conf has every pip, a venv's too,
# install from the wheels in /opt/wheels or fail at once instead of retrying
# an index it cannot reach. Its build context is this directory.
#
# Every base image is pinned by digest, the tag beside it saying which one it
# was taken from, so a rebuild of the same commit is the same image.

FROM python:3.12.15-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS wheels
COPY wheels.txt /tmp/
RUN pip download --no-cache-dir --only-binary=:all: --no-deps --dest=/opt/wheels -r /tmp/wheels.txt

FROM python:3.12.15-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --from=wheels /opt/wheels /opt/wheels
COPY pip.conf /etc/pip.conf
# The substrate provider's SUBSTRATE_WORKDIR default, where it writes the tests.
WORKDIR /workspace
