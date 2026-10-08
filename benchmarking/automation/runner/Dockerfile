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

# The Cloud Run job's container: the runner package, its infra/,
# terraform and tests.yaml. Its build context is benchmarking/automation/, for
# tests.yaml.
#
# Every base image is pinned by digest, the tag beside it saying which one it
# was taken from, so a rebuild of the same commit is the same image.

FROM hashicorp/terraform:1.13.3@sha256:dfb1889a8ee74ada3ddacc48f89b8a0d69f3e114de3d8a2ce15f6a8d3dbfdbe2 AS terraform

FROM python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e
COPY --from=terraform /bin/terraform /usr/local/bin/terraform

WORKDIR /app
COPY runner/requirements.txt runner/
RUN pip install --no-cache-dir --require-hashes -r runner/requirements.txt

COPY tests.yaml .
COPY runner/infra/ runner/infra/
# The providers the lock file pins, installed now rather than downloaded by
# every execution's terraform init.
RUN terraform -chdir=runner/infra init -backend=false -input=false
COPY runner/*.py runner/

ENV PYTHONUNBUFFERED=1
# Takes the runner's arguments.
ENTRYPOINT ["python3", "-m", "runner"]
