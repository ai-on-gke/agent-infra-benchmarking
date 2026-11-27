FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/*

WORKDIR /testbed
RUN git config --system user.name "sweperf" && \
    git config --system user.email "sweperf@example.com" && \
    git config --system init.defaultBranch main && \
    git init && \
    echo "# SWE-perf Workspace" > README.md && \
    git add README.md && \
    git commit -m "Initial commit"

COPY replay.py /replay.py
COPY traces/ /traces/
COPY traces/astropy-7336.json /trace.json

EXPOSE 80

CMD ["python3", "/replay.py", "/trace.json"]
