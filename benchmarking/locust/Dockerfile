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

# Build context must be the monorepo root so the Go builder can pull in
# benchmarking/locust/go/ and the substrate submodule. build_and_push.sh sets this up.
#
# Every base image is pinned by digest, the tag beside it saying which one it
# was taken from, so a rebuild of the same commit is the same image.

# Stage 1: Python dependency builder
FROM python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e AS pydeps
WORKDIR /app
COPY benchmarking/locust/requirements.txt .
RUN pip install --no-cache-dir --target=/app/deps -r requirements.txt

# Stage 2: Go build of the boomer-worker worker. Lives in the same image so
# runner.py can spawn it as a subprocess during headless runs of glutton.
# CGO_ENABLED=0 keeps us on boomer's pure-Go ZMQ (gomq), no libzmq needed.
FROM golang:1.27.1-bookworm@sha256:69a7b9788769bec032d238959b61854e9ae87f57be9029ec04e9885fabf99195 AS goboomer
# The image's own Go, which has to satisfy go.mod's go line. auto would
# download a newer toolchain mid-build whenever go.mod asked for one.
ENV GOTOOLCHAIN=local
COPY dependency-repos/substrate /src/dependency-repos/substrate
# go.mod's replace reaches the submodule as ../../../dependency-repos/substrate.
WORKDIR /src/benchmarking/locust/go
COPY benchmarking/locust/go/go.mod benchmarking/locust/go/go.sum ./
RUN go mod download
COPY benchmarking/locust/go ./
# Read from outside the image too: run_locust_job.sh resolves a case by running
# this binary, so the host needs no YAML parser of its own.
RUN CGO_ENABLED=0 go build -o /out/testcase-helper ./cmd/testcase-helper
RUN CGO_ENABLED=0 go build -o /out/boomer-worker ./cmd/boomer-worker

# Stage 3: Distroless runner. Carries the locust install AND the boomer
# binary; the manifest decides which to launch via the `args` field.
FROM gcr.io/distroless/python3-debian12:latest@sha256:2fdb05402a2cf21cf78fdb3ba4c5db167241e9e498140f5bf689d7efb773731f
WORKDIR /app
COPY --from=pydeps /app/deps /app/deps
COPY --from=goboomer /out/testcase-helper /app/testcase-helper
COPY --from=goboomer /out/boomer-worker /app/boomer-worker
COPY benchmarking/locust/tests/ /app/tests/
COPY benchmarking/locust/reports/ /app/reports/
COPY benchmarking/locust/runner.py /app/runner.py
# The cases --test-case selects from.
COPY benchmarking/tests.yaml /app/tests.yaml

ENV PYTHONPATH=/app:/app/deps
ENV PYTHONUNBUFFERED=1
