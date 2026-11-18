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

# The loader of a swebench case: sandboxenv-rl's swebench-loader, and
# run_loader.py, which runs it and uploads what it wrote. Its build context is
# this directory, and the sandboxenv-rl submodule is a second, named one:
#
#   docker build --build-context sandboxenv-rl=dependency-repos/sandboxenv-rl \
#     -f benchmarking/swebench/image/Dockerfile benchmarking/swebench/image
#
# Every base image is pinned by digest, the tag beside it saying which one it
# was taken from, so a rebuild of the same commit is the same image.

FROM golang:1.27.1-bookworm@sha256:69a7b9788769bec032d238959b61854e9ae87f57be9029ec04e9885fabf99195 AS build
# The image's own Go, which has to satisfy go.mod's go line. auto would
# download a newer toolchain mid-build whenever go.mod asked for one.
ENV GOTOOLCHAIN=local
WORKDIR /src
COPY --from=sandboxenv-rl go.mod go.sum ./
RUN go mod download
COPY --from=sandboxenv-rl . ./
# -buildvcs=false: the submodule's .git is a file naming a directory that is
# not in the build context, so Go could not read the version control state.
RUN CGO_ENABLED=0 go build -trimpath -buildvcs=false -o /out/swebench-loader ./cmd/benchmarking/swebench-loader

FROM python:3.12.15-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
COPY --from=build /out/swebench-loader /usr/local/bin/swebench-loader
# Only the standard library: it reads the access token from the metadata
# server and uploads with the Cloud Storage JSON API.
COPY run_loader.py /app/run_loader.py
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/run_loader.py"]
