# syntax=docker/dockerfile:1.19@sha256:b6afd42430b15f2d2a4c5a02b919e98a525b785b1aaff16747d2f623364e39b6

# The job's image: the repo at one commit, built from its top level by
# .github/workflows/publish.yml, with what an execution would otherwise
# fetch or compile baked in. benchmarking/scenario_runner/requirements.txt is
# compiled for this Python.
FROM hashicorp/terraform:1.15.6@sha256:adae45661e45d3c88beef071ee1277b4621cea73517aae7f0844657c8e85f641 AS terraform

# The same Go as benchmarking/locust/go/Dockerfile builds with.
FROM golang:1.27.1-bookworm@sha256:69a7b9788769bec032d238959b61854e9ae87f57be9029ec04e9885fabf99195 AS go

FROM python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e AS base
COPY --from=terraform /bin/terraform /usr/local/bin/terraform
COPY --from=go /usr/local/go /usr/local/go
ENV PATH=/usr/local/go/bin:$PATH

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl git gnupg jq make zstd \
  && curl -sSfL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
    | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
  && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
    > /etc/apt/sources.list.d/google-cloud-sdk.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends google-cloud-cli google-cloud-cli-gke-gcloud-auth-plugin kubectl \
  && rm -rf /var/lib/apt/lists/*

# Outside the repo, so that its copy below leaves them be.
ENV PYTHONUNBUFFERED=1 \
  TF_PLUGIN_CACHE_DIR=/opt/terraform-plugin-cache \
  GOMODCACHE=/opt/go/mod \
  GOCACHE=/opt/go/cache
WORKDIR /source

# The providers of the configs the scenario-runner applies, as their lock files
# pin them.
FROM base AS providers
COPY benchmarking/scenario_runner/infra /tf/runner
COPY benchmarking/scenarios/infra/clusters /tf/clusters
RUN for dir in /tf/runner /tf/clusters/*/; do terraform -chdir="$dir" init -backend=false -input=false; done

# The microvm assets of the versions the substrate submodule's ate-setup pins,
# laid out and stamped as it assembles them, for both architectures it stages
# on GKE, so it finds them complete and downloads nothing. It checks their
# sha256s against its SandboxConfig before it uploads them.
FROM base AS microvm
COPY dependency-repos/substrate/cmd/ate-setup/internal/steps/microvm.go /tmp/microvm.go
RUN set -e; \
  kata=$(sed -n 's/^\tdefaultKataVersion *= "\(.*\)"$/\1/p' /tmp/microvm.go); \
  ch=$(sed -n 's/^\tdefaultCloudHypervisorVersion *= "\(.*\)"$/\1/p' /tmp/microvm.go); \
  test -n "$kata" && test -n "$ch"; \
  for arch in amd64 arm64; do \
    out=/microvm/$arch kroot=/tmp/kata-$arch/opt/kata; \
    mkdir -p "$out" "/tmp/kata-$arch"; \
    curl -fsSL "https://github.com/kata-containers/kata-containers/releases/download/$kata/kata-static-$kata-$arch.tar.zst" \
      | tar --zstd -x -C "/tmp/kata-$arch" --wildcards '*opt/kata/share/kata-containers/*' '*opt/kata/libexec/virtiofsd'; \
    cp -L "$kroot/share/kata-containers/vmlinux.container" "$out/vmlinux"; \
    cp -L "$kroot/share/kata-containers/kata-containers.img" "$out/rootfs.img"; \
    cp -L "$kroot/libexec/virtiofsd" "$out/virtiofsd"; \
    chname=cloud-hypervisor-static; [ "$arch" = arm64 ] && chname=cloud-hypervisor-static-aarch64; \
    curl -fsSL -o "$out/cloud-hypervisor" "https://github.com/cloud-hypervisor/cloud-hypervisor/releases/download/$ch/$chname"; \
    chmod 644 "$out/vmlinux" "$out/rootfs.img"; chmod 755 "$out/virtiofsd" "$out/cloud-hypervisor"; \
    printf 'arch=%s\nkata=%s\ncloud-hypervisor=%s\n' "$arch" "$kata" "$ch" > "$out/.asset-versions"; \
    rm -rf "/tmp/kata-$arch"; \
  done

# Go's caches for what each execution compiles: ate-setup, which the runner
# runs with go run, and env's CLI. Without VCS stamps, as
# the submodules' git dirs come with the repo's later; the packages compile the
# same.
FROM base AS gocache
COPY dependency-repos/substrate dependency-repos/substrate
COPY dependency-repos/env dependency-repos/env
RUN cd dependency-repos/substrate && go build -buildvcs=false -o /dev/null ./cmd/ate-setup \
  && cd ../env && go build -buildvcs=false -o /dev/null ./cmd/ate-env

# What changes least first, so that most commits only add the last layers.
FROM base
COPY benchmarking/scenario_runner/requirements.txt benchmarking/scenario_runner/
COPY benchmarking/tools/python benchmarking/tools/
RUN benchmarking/tools/python -c ''
COPY --from=providers /opt/terraform-plugin-cache /opt/terraform-plugin-cache
COPY --from=gocache /opt/go /opt/go
COPY dependency-repos dependency-repos
COPY --from=microvm /microvm dependency-repos/substrate/bin/microvm-assets
COPY --exclude=dependency-repos . .

# The workflow's builds fail on a tree git sees changed, as the scenario-runner
# would tag the images it builds from it dirty.
ARG REQUIRE_CLEAN=
RUN git log -1 --format='%H %s' \
  && if [ -n "$REQUIRE_CLEAN" ] && [ -n "$(git status --porcelain)" ]; then git status; exit 1; fi

ENTRYPOINT ["/source/automation-infra/image/entrypoint.sh"]
