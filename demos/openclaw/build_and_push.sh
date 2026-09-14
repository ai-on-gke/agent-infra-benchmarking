#!/usr/bin/env bash
source .ate-dev-env.sh
OPENCLAW_IMAGE="us-docker.pkg.dev/${PROJECT_ID}/ate-images-dima/openclaw-actor:latest"
docker build --platform linux/amd64 -t "${OPENCLAW_IMAGE}" demos/openclaw/
docker push "${OPENCLAW_IMAGE}"
