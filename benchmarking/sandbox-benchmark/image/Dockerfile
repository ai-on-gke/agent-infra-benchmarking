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

# The comparator: ai-sandbox-benchmark's comparator.py, its providers and
# tests, the ate-env client its substrate provider drives, and
# run_comparator.py, which runs the tests and uploads what they recorded. Its
# build context is this directory, with the ai-sandbox-benchmark submodule and
# env's Python client as the build contexts of those names.
#
# Every base image is pinned by digest, the tag beside it saying which one it
# was taken from, so a rebuild of the same commit is the same image.

FROM python:3.12.15-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
WORKDIR /app
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt
# Its dependencies are in requirements.txt.
COPY --from=ate-env-client . /tmp/ate-env-client
RUN pip install --no-cache-dir --no-deps /tmp/ate-env-client && rm -rf /tmp/ate-env-client /tmp/requirements.txt

# Named one by one, so a .env left in the submodule stays out of the image.
COPY --from=ai-sandbox-benchmark comparator.py metrics.py config.yml /app/
COPY --from=ai-sandbox-benchmark providers/ /app/providers/
COPY --from=ai-sandbox-benchmark tests/ /app/tests/
COPY run_comparator.py /app/

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/run_comparator.py"]
