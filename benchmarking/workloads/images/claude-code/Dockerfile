FROM node:26.10.0-slim@sha256:ec7758ee051e457b468b32bde57b0879010b325bb9862718e9615225ce4aaae1

# The Debian snapshot the base image was built from (its debian.sources names
# it), so the packages below are the same on every build.
ARG DEBIAN_SNAPSHOT=20260918T000000Z
RUN sed -i \
      -e "s|^URIs: http://deb.debian.org/debian\$|URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|" \
      -e "s|^URIs: http://deb.debian.org/debian-security\$|URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|" \
      /etc/apt/sources.list.d/debian.sources \
 && apt-get -o Acquire::Check-Valid-Until=false update \
 && apt-get install -y --no-install-recommends git python3 python3-pytest \
 && rm -rf /var/lib/apt/lists/*

# Claude Code at the version package-lock.json records.
WORKDIR /opt/claude-code
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
ENV PATH=/opt/claude-code/node_modules/.bin:${PATH}

# An actor's egress is limited to the hosts the benchmark needs, so an update
# check or telemetry call would only fail. IS_SANDBOX lets root use
# --dangerously-skip-permissions. The key is mock-llm's, which takes any.
ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    IS_SANDBOX=1 \
    ANTHROPIC_API_KEY=mock-key \
    PYTHONDONTWRITEBYTECODE=1

# The repo the agent works on, its five failing tests committed and tagged
# base, at a fixed date so every build has the same commit.
COPY textkit /workspace/textkit
RUN git config --system user.name benchmark \
 && git config --system user.email benchmark@example.com \
 && cd /workspace/textkit \
 && git init -q -b main \
 && git add -A \
 && GIT_AUTHOR_DATE=2026-01-01T00:00:00Z GIT_COMMITTER_DATE=2026-01-01T00:00:00Z \
    git commit -qm "textkit with five failing tests" \
 && git tag base

COPY claude_code_actor.py /app/claude_code_actor.py

CMD ["python3", "-u", "/app/claude_code_actor.py"]
