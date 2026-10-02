FROM node:26.10.0-slim@sha256:ec7758ee051e457b468b32bde57b0879010b325bb9862718e9615225ce4aaae1

# The Debian snapshot the base image was built from (its debian.sources names
# it), so the packages below are the same on every build.
ARG DEBIAN_SNAPSHOT=20260918T000000Z
RUN sed -i \
      -e "s|^URIs: http://deb.debian.org/debian\$|URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|" \
      -e "s|^URIs: http://deb.debian.org/debian-security\$|URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|" \
      /etc/apt/sources.list.d/debian.sources \
 && apt-get -o Acquire::Check-Valid-Until=false update \
 && apt-get install -y curl python3 \
 && rm -rf /var/lib/apt/lists/*

# openclaw and its whole dependency tree, at the versions package-lock.json
# records.
WORKDIR /opt/openclaw
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
ENV PATH=/opt/openclaw/node_modules/.bin:${PATH}

# An actor's egress is limited to the hosts the benchmark needs, so a version
# check or telemetry call would only fail.
ENV OPENCLAW_NO_AUTO_UPDATE=1 \
    OPENCLAW_TELEMETRY=0 \
    DO_NOT_TRACK=1

COPY openclaw_actor.py /app/openclaw_actor.py

CMD ["python3", "-u", "/app/openclaw_actor.py"]
