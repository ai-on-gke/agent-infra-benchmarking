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

# The boomer worker. Its build context is this directory, and go.mod's replace
# needs the substrate submodule beside it as a second, named one:
#
#   docker build --build-context substrate=dependency-repos/substrate \
#     -f benchmarking/locust/go/Dockerfile benchmarking/locust/go
#
# Every base image is pinned by digest, the tag beside it saying which one it
# was taken from, so a rebuild of the same commit is the same image.

# CGO_ENABLED=0 keeps us on boomer's pure-Go ZMQ (gomq), no libzmq needed.
FROM golang:1.27.1-bookworm@sha256:69a7b9788769bec032d238959b61854e9ae87f57be9029ec04e9885fabf99195 AS build
# The image's own Go, which has to satisfy go.mod's go line. auto would
# download a newer toolchain mid-build whenever go.mod asked for one.
ENV GOTOOLCHAIN=local
# Where go.mod's ../../../dependency-repos/substrate finds it.
COPY --from=substrate . /src/dependency-repos/substrate
WORKDIR /src/benchmarking/locust/go
COPY go.mod go.sum ./
RUN go mod download
COPY . ./
RUN CGO_ENABLED=0 go build -o /out/boomer-worker ./cmd/boomer-worker

FROM gcr.io/distroless/static-debian12:latest@sha256:d75cdd72874d4790092fcb1b058493ecf6bb5bf2b2b897045b00ff01d91843f2
COPY --from=build /out/boomer-worker /app/boomer-worker
ENTRYPOINT ["/app/boomer-worker"]
