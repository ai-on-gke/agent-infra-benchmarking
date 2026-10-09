# The job's image: fetches the latest source of a branch, then installs and
# runs the scenario-runner in it. benchmarking/scenario_runner/requirements.txt
# is compiled for this Python.
FROM hashicorp/terraform:1.13.3@sha256:dfb1889a8ee74ada3ddacc48f89b8a0d69f3e114de3d8a2ce15f6a8d3dbfdbe2 AS terraform

# The same Go as benchmarking/locust/go/Dockerfile builds with.
FROM golang:1.27.1-bookworm@sha256:69a7b9788769bec032d238959b61854e9ae87f57be9029ec04e9885fabf99195 AS go

FROM python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e
COPY --from=terraform /bin/terraform /usr/local/bin/terraform
COPY --from=go /usr/local/go /usr/local/go
ENV PATH=/usr/local/go/bin:$PATH

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl git gnupg make zstd \
  && curl -sSfL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
    | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
  && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
    > /etc/apt/sources.list.d/google-cloud-sdk.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends google-cloud-cli google-cloud-cli-gke-gcloud-auth-plugin kubectl \
  && rm -rf /var/lib/apt/lists/*
# The extracted source keeps the uploader's owner, which git otherwise refuses
# to work in, in the repo and its submodules alike.
RUN git config --system --add safe.directory '*'

ENV PYTHONUNBUFFERED=1
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
