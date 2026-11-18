#!/usr/bin/env bash
# Runs a workload's actor beside mock-llm in Docker, mock-llm's container
# running its turns.py with this script's arguments: turns.py serves the
# agent's calls and sends the actor its turns. The keys come from .env or the
# environment.
#
#   ./run.sh WORKLOAD [script | record NAME [PROMPT | -i] | replay NAME]
set -euo pipefail
cd "$(dirname "$0")"

workload=${1:-}
# How each workload's actor, its folder in ../images, finds mock-llm.
case $workload in
  claude-code) actor_env=(-e ANTHROPIC_BASE_URL=http://mock-llm:8080) ;;
  openclaw) actor_env=(-e OPENCLAW_USE_MOCK_LLM=true -e MOCK_LLM_BASE_URL=http://mock-llm:8080/v1) ;;
  *) echo "usage: $0 WORKLOAD ...; workloads: claude-code openclaw" >&2; exit 2 ;;
esac

if [ -f .env ]; then
  set -a; source .env; set +a
fi
# Passed by name, so a key stays out of docker's command line; unset ones
# stay unset.
mock_llm_env=()
for name in ANTHROPIC_API_KEY ANTHROPIC_UPSTREAM OPENAI_API_KEY OPENAI_UPSTREAM OPENAI_UPSTREAM_MODEL \
  REPLAY_TIMING TURNS; do
  mock_llm_env+=(-e "$name")
done
tty=(-i)
if [ -t 0 ]; then
  tty=(-it)
fi

name=recorder-$workload-$$
trap 'docker rm -f "$name-mock-llm" "$name-actor" >/dev/null 2>&1; docker network rm "$name" >/dev/null 2>&1 || true' EXIT

docker build -q -t recorder-mock-llm ../images/mock-llm >/dev/null
docker build -q -t "recorder-$workload" --build-context textkit=../images/textkit "../images/$workload" >/dev/null
docker network create "$name" >/dev/null
docker run -d --name "$name-actor" --network "$name" --network-alias actor "${actor_env[@]}" \
  "recorder-$workload" >/dev/null
# As you, so the recordings it writes are yours.
docker run --rm "${tty[@]}" --name "$name-mock-llm" --network "$name" --network-alias mock-llm \
  --user "$(id -u):$(id -g)" -v "$PWD/../images/mock-llm/recordings:/recordings" "${mock_llm_env[@]}" \
  recorder-mock-llm python3 -u /app/turns.py "$@"
