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

# The locust master: locust, the locustfile and run_master.py, which uploads
# locust's CSVs once it exits. Its build context is this directory.
#
# Every base image is pinned by digest, the tag beside it saying which one it
# was taken from, so a rebuild of the same commit is the same image.

FROM python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e AS pydeps
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --target=/app/deps -r requirements.txt

FROM gcr.io/distroless/python3-debian12:latest@sha256:2fdb05402a2cf21cf78fdb3ba4c5db167241e9e498140f5bf689d7efb773731f
WORKDIR /app
COPY --from=pydeps /app/deps /app/deps
COPY tests/ /app/tests/
COPY run_master.py upload.py custom_history.py cluster_vitals.py readme.py /app/

ENV PYTHONPATH=/app/deps
ENV PYTHONUNBUFFERED=1
# locust's own -f, read from the environment so the arguments are only the run's.
ENV LOCUST_LOCUSTFILE=/app/tests/runworkload.py
# locust's own --csv and --csv-full-history. run_master.py uploads this folder.
ENV LOCUST_CSV=/app/reports/locust
ENV LOCUST_CSV_FULL_HISTORY=true
# Takes locust's arguments.
ENTRYPOINT ["python3", "/app/run_master.py"]
