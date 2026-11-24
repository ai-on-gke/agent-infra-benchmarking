# Stage 1: Build kubectl-ate from substrate submodule
FROM golang:1.27.3-bookworm AS substrate-builder
RUN apt-get update && apt-get install -y --no-install-recommends patch && rm -rf /var/lib/apt/lists/*
WORKDIR /substrate
COPY --from=substrate . /substrate
COPY podidentity.patch /tmp/podidentity.patch
RUN patch -p1 < /tmp/podidentity.patch \
 && CGO_ENABLED=0 go build -o /usr/local/bin/kubectl-ate ./cmd/kubectl-ate

# Stage 2: ComputeSDK runner image
FROM node:22-bookworm

# Install kubectl, python3, and required utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    procps \
    python3 \
    python3-pip \
    tar \
    unzip \
 && curl -fsSL -o /usr/local/bin/kubectl https://dl.k8s.io/release/v1.31.0/bin/linux/amd64/kubectl \
 && chmod +x /usr/local/bin/kubectl \
 && rm -rf /var/lib/apt/lists/*

# Install python dependencies for GCS report upload
COPY requirements.in /tmp/
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.in && rm /tmp/requirements.in

# Copy kubectl-ate binary
COPY --from=substrate-builder /usr/local/bin/kubectl-ate /usr/local/bin/kubectl-ate

# Setup ComputeSDK application
WORKDIR /app
COPY --from=computesdk-benchmarks . /app/

# Enable corepack and install dependencies
RUN corepack enable && corepack prepare pnpm@10.33.0 --activate \
 && pnpm install --frozen-lockfile

# Copy runner entrypoint
COPY run_computesdk.py /app/run_computesdk.py

ENV PYTHONUNBUFFERED=1
ENV KUBECTL_ATE_PATH=/usr/local/bin/kubectl-ate

ENTRYPOINT ["python3", "/app/run_computesdk.py"]
