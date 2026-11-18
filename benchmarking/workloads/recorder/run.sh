#!/usr/bin/env bash
# Runs a workload's actor beside mock-llm in Docker, and mock-llm's turns.py
# in mock-llm's container, which sends the actor turns as the benchmark's
# worker does and checks each reply. The mode is mock-llm's: script (default),
# record (the agent's calls go to the real API, with the keys in .env) or
# replay, both of a recording NAME in ../images/mock-llm/recordings/. record
# takes a prompt to send instead of the worker's, or -i to ask for each turn's
# prompt once the reply to the one before is shown; replay sends what the
# recording was made with. TURNS sets how many turns to send.
set -euo pipefail
cd "$(dirname "$0")"

usage="usage: $0 WORKLOAD [script | record NAME [PROMPT | -i] | replay NAME]"
workload=${1:-}
mode=${2:-script}
recording=${3:-}
# Each workload's actor: its folder in ../images, the docker run options that
# point it at mock-llm, and the variables recording needs. Its prompt, turns
# and checks are mock-llm's turns.py's.
case $workload in
  claude-code)
    actor_image=claude-code
    actor_env=(-e ANTHROPIC_BASE_URL=http://mock-llm:8080)
    record_needs=(ANTHROPIC_API_KEY)
    ;;
  openclaw)
    actor_image=openclaw
    actor_env=(-e OPENCLAW_USE_MOCK_LLM=true -e MOCK_LLM_BASE_URL=http://mock-llm:8080/v1)
    record_needs=(OPENAI_API_KEY OPENAI_UPSTREAM_MODEL)
    ;;
  *)
    echo "$usage" >&2
    echo "workloads: claude-code openclaw" >&2
    exit 2
    ;;
esac
case $mode in
  script) [ $# -le 2 ] || { echo "$usage" >&2; exit 2; } ;;
  replay) [ $# -eq 3 ] || { echo "$usage" >&2; exit 2; } ;;
  record) [ $# -ge 3 ] && [ $# -le 4 ] || { echo "$usage" >&2; exit 2; } ;;
  *) echo "$usage" >&2; exit 2 ;;
esac
case $recording in
  . | .. | */*) echo "a recording's name is a folder name, got $recording" >&2; exit 2 ;;
esac

recordings=../images/mock-llm/recordings
cassettes=$recordings/$recording
cassette=$cassettes/cassette.jsonl
mock_llm_env=(-e MOCK_LLM_MODE="$mode")
mock_llm_opts=()
turns_args=("$workload" ${4+"$4"} ${TURNS:+--turns "$TURNS"})
exec_opts=()
if [ "$mode" = record ]; then
  if [ "${4:-}" = -i ]; then
    exec_opts=(-it)
  fi
  if [ -f .env ]; then
    set -a; source .env; set +a
  fi
  for name in "${record_needs[@]}"; do
    if [ -z "${!name:-}" ]; then
      echo "recording $workload needs $name, in .env or the environment" >&2; exit 1
    fi
  done
  if [ -e "$cassette" ]; then
    echo "$cassette exists; delete it or pick another name" >&2; exit 1
  fi
  for name in "${record_needs[@]}" ANTHROPIC_UPSTREAM OPENAI_UPSTREAM; do
    if [ -n "${!name:-}" ]; then
      # Passed by name, so a key stays out of docker's command line.
      export "${name?}"
      mock_llm_env+=(-e "$name")
    fi
  done
fi
if [ "$mode" = replay ]; then
  if [ ! -e "$cassette" ]; then
    have=$(cd "$recordings" && ls -d */ 2>/dev/null | xargs -rn1 basename | tr '\n' ' ')
    echo "no $cassette to replay; recordings: ${have:-none}" >&2; exit 1
  fi
  if [ -n "${REPLAY_TIMING:-}" ]; then
    mock_llm_env+=(-e REPLAY_TIMING)
  fi
fi
if [ "$mode" != script ]; then
  # Created here, not by Docker, which would make it root's.
  mkdir -p "$cassettes"
  mock_llm_opts=(-v "$PWD/$cassettes:/cassettes")
fi

name=recorder-$workload-$$
mock_llm=$name-mock-llm
actor=$name-actor

finish() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "--- mock-llm's log, last 20 lines" >&2
    docker logs --tail 20 "$mock_llm" >&2 2>&1 || true
  fi
  docker rm -f "$mock_llm" "$actor" >/dev/null 2>&1 || true
  docker network rm "$name" >/dev/null 2>&1 || true
  # A recording that saved nothing leaves no folder behind.
  if [ "$mode" = record ]; then
    rmdir "$cassettes" 2>/dev/null || true
  fi
  exit "$status"
}
trap finish EXIT

docker build -q -t recorder-mock-llm ../images/mock-llm >/dev/null
docker build -q -t "recorder-$actor_image" "../images/$actor_image" >/dev/null
docker network create "$name" >/dev/null
docker run -d --name "$mock_llm" --network "$name" --network-alias mock-llm \
  --user "$(id -u):$(id -g)" "${mock_llm_opts[@]}" "${mock_llm_env[@]}" \
  recorder-mock-llm >/dev/null
docker run -d --name "$actor" --network "$name" --network-alias actor "${actor_env[@]}" \
  "recorder-$actor_image" >/dev/null

docker exec "${exec_opts[@]}" "$mock_llm" python3 -u /app/turns.py "${turns_args[@]}"

log=$(docker logs "$mock_llm" 2>&1)
echo "mock-llm answered $(grep -cE 'POST /v1/(messages|chat/completions)' <<<"$log") agent calls"
if [ "$mode" = record ]; then
  echo "recorded $(grep -c '^record: saved' <<<"$log") answers to $cassette"
fi
