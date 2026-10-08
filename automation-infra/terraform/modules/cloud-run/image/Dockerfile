# The job's image: fetches the latest source of a branch, then installs and
# runs the orchestrator in it. The same Python and terraform as
# benchmarking/scenario_runner/Dockerfile, whose requirements.txt is
# compiled for that Python.
FROM hashicorp/terraform:1.13.3@sha256:dfb1889a8ee74ada3ddacc48f89b8a0d69f3e114de3d8a2ce15f6a8d3dbfdbe2 AS terraform

FROM python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e
COPY --from=terraform /bin/terraform /usr/local/bin/terraform

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl git \
  && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
