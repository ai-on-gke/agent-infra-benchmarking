#!/usr/bin/env bash
# Runs a workload's actor beside mock-llm in Docker, sends it turns as the
# benchmark's worker does and checks each reply with the workload's own check
# (<workload>/workload.sh). The mode is mock-llm's: script (default), record
# (the agent's calls go to the real API, with the keys in .env, and are saved
# to <workload>/cassettes/) or replay. record takes a prompt to send instead
# of the worker's, or -i to ask for each turn's prompt once the reply to the
# one before is shown; replay sends what the cassette was recorded with.
# TURNS sets how many turns to send.
set -euo pipefail
cd "$(dirname "$0")"

usage="usage: $0 WORKLOAD [script | record [PROMPT | -i] | replay]"
workload=${1:-}
mode=${2:-script}
if [ -z "$workload" ] || [ ! -f "$workload/workload.sh" ]; then
  echo "$usage" >&2
  echo "workloads: $(ls -d */ | xargs -n1 basename | tr '\n' ' ')" >&2
  exit 2
fi
case $mode in
  script | replay) [ $# -le 2 ] || { echo "$usage" >&2; exit 2; } ;;
  record) [ $# -le 3 ] || { echo "$usage" >&2; exit 2; } ;;
  *) echo "$usage" >&2; exit 2 ;;
esac

# Sets actor_image, actor_env, worker_prompt, default_turns, record_needs and
# check_reply.
source "$workload/workload.sh"

prompt=$worker_prompt
turns=${TURNS:-$default_turns}
cassettes=$workload/cassettes
cassette=$cassettes/cassette.jsonl
prompt_file=$cassettes/prompt.txt
# A conversation's prompts, one a line, in place of prompt_file.
turns_file=$cassettes/turns.txt
# record when the turns' prompts are asked for, replay when they are turns_file's.
conversation=
mock_llm_env=(-e MOCK_LLM_MODE="$mode")
if [ "$mode" = record ]; then
  prompt=${3:-$worker_prompt}
  if [ "$prompt" = -i ]; then
    conversation=record
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
    echo "$cassette exists; delete it to record a new one" >&2; exit 1
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
    echo "no $cassette to replay; record one with: $0 $workload record" >&2; exit 1
  fi
  if [ -e "$turns_file" ]; then
    conversation=replay
    mapfile -t turn_prompts <"$turns_file"
    turns=${TURNS:-${#turn_prompts[@]}}
    if [ "$turns" -gt "${#turn_prompts[@]}" ]; then
      echo "$turns_file has only ${#turn_prompts[@]} turns" >&2; exit 1
    fi
  elif [ -e "$prompt_file" ]; then
    prompt=$(cat "$prompt_file")
  fi
  if [ -n "${REPLAY_TIMING:-}" ]; then
    mock_llm_env+=(-e REPLAY_TIMING)
  fi
fi
# Created here, not by Docker, which would make it root's.
mkdir -p "$cassettes"
if [ "$mode" = record ]; then
  rm -f "$prompt_file" "$turns_file"
  if [ -z "$conversation" ]; then
    printf '%s\n' "$prompt" >"$prompt_file"
  fi
fi

name=local-test-$workload-$$
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
  exit "$status"
}
trap finish EXIT

docker build -q -t local-test-mock-llm ../images/mock-llm >/dev/null
docker build -q -t "local-test-$actor_image" "../images/$actor_image" >/dev/null
docker network create "$name" >/dev/null
docker run -d --name "$mock_llm" --network "$name" --network-alias mock-llm \
  --user "$(id -u):$(id -g)" -v "$PWD/$cassettes:/cassettes" "${mock_llm_env[@]}" \
  local-test-mock-llm >/dev/null
docker run -d --name "$actor" --network "$name" -p 127.0.0.1::80 "${actor_env[@]}" \
  "local-test-$actor_image" >/dev/null

url=http://$(docker port "$actor" 80/tcp | head -1)
for _ in $(seq 30); do
  curl -sf "$url/readyz" >/dev/null && break
  sleep 1
done
if [ "$(docker inspect -f '{{.State.Running}}' "$mock_llm")" != true ]; then
  echo "mock-llm did not start" >&2; exit 1
fi

# send_turn I PROMPT: sends the turn and checks its reply. A conversation's
# says which turn it is, which the actor starts over on at 1.
send_turn() {
  local nonce reply query=
  [ -n "$conversation" ] && query="?turn=$1"
  nonce=$(cat /proc/sys/kernel/random/uuid)
  if ! reply=$(printf '%s (request %s)' "$2" "$nonce" |
    curl -s --fail-with-body -X POST -H 'Content-Type: text/plain' --data-binary @- "$url/invoke$query"); then
    echo "turn $1 failed: ${reply:0:2000}" >&2; exit 1
  fi
  check_reply "$reply" "$nonce" "$1" "$own_prompt"
}

if [ "$conversation" = record ]; then
  own_prompt=1
  echo "Type each turn's prompt; an empty line ends the recording."
  i=0
  while read -r -e -p "turn $((i + 1))> " prompt && [ -n "$prompt" ]; do
    i=$((i + 1))
    send_turn "$i" "$prompt"
    # Saved once answered, so the file has only the turns the cassette does.
    printf '%s\n' "$prompt" >>"$turns_file"
  done
elif [ "$conversation" = replay ]; then
  own_prompt=1
  for i in $(seq "$turns"); do
    echo "turn $i> ${turn_prompts[i - 1]}"
    send_turn "$i" "${turn_prompts[i - 1]}"
  done
else
  echo "prompt: $prompt"
  [ "$prompt" = "$worker_prompt" ] && own_prompt=0 || own_prompt=1
  for i in $(seq "$turns"); do
    send_turn "$i" "$prompt"
  done
fi

log=$(docker logs "$mock_llm" 2>&1)
echo "mock-llm answered $(grep -cE 'POST /v1/(messages|chat/completions)' <<<"$log") agent calls"
if [ "$mode" = record ]; then
  echo "recorded $(grep -c '^record: saved' <<<"$log") answers to $cassette"
fi
