# Workloads on a local machine

Runs a workload's actor beside [mock-llm](../images/mock-llm) in Docker,
sends it turns as the benchmark's worker does and checks each reply, to try a
workload without a cluster. It also records the agent's calls to a real model
and replays them, so the agent gets the same answers every run:
`./run.sh claude-code record`, then `./run.sh claude-code replay`.

Each workload that uses mock-llm has a folder here: `claude-code` and
`openclaw`.

## Running the check

Build the workload's actor and mock-llm, start them on a fresh state, send the
turns and check each reply:

```bash
./run.sh claude-code
```

mock-llm scripts the agent's turns. The script ends by counting the calls
mock-llm answered, and removes the containers whether the check passed or not;
on a failure it prints the end of mock-llm's log first. `TURNS` sets how many
turns to send:

```bash
TURNS=2 ./run.sh openclaw
```

## Recording a session with a real model

Put the keys in `.env`, which git ignores: `ANTHROPIC_API_KEY` for
claude-code, which talks the Anthropic API; for openclaw, which talks the
OpenAI Chat Completions API, `OPENAI_API_KEY`, the model to record with in
`OPENAI_UPSTREAM_MODEL` and, for an API other than OpenAI's,
`OPENAI_UPSTREAM`. Then run the turns with mock-llm forwarding the agent's
calls to the API and saving each answer to `<workload>/cassettes/cassette.jsonl`:

```bash
./run.sh claude-code record
```

Only mock-llm gets the keys; the actor keeps its dummy one. The script refuses
to record over an existing cassette, so delete it first to record again.

To record the answers to a prompt of your own rather than the worker's, give
it as an argument; each turn sends it, and it is saved to
`<workload>/cassettes/prompt.txt` for the replay to send:

```bash
./run.sh claude-code record "Add type hints to textkit/case.py and commit them."
```

A cassette recorded with the worker's prompt is the only one the benchmark's
worker can replay, as it always sends that prompt.

## Replaying a recording

Run the same turns, with the prompt the cassette was recorded with, and
mock-llm answering only from the cassette:

```bash
./run.sh claude-code replay
```

A call the cassette has no answer for fails the turn, and mock-llm's log shows
the conversation that missed. To replay at the pace the answers were recorded
rather than at once:

```bash
REPLAY_TIMING=recorded ./run.sh claude-code replay
```

## Adding a workload

Give it a folder with a `workload.sh`, which `run.sh` sources; see
`claude-code/workload.sh`. It sets:

- `actor_image`: the actor's folder in `../images`.
- `actor_env`: the `docker run` options that point the actor at
  `http://mock-llm:8080`.
- `worker_prompt` and `default_turns`: what the worker sends.
- `record_needs`: the variables recording needs.
- `check_reply REPLY NONCE I OWN_PROMPT`: the worker's check of turn `I`'s
  reply, failing if it does not pass; `OWN_PROMPT` is 1 for a prompt of your
  own.

mock-llm records the Anthropic Messages and OpenAI Chat Completions APIs; the
answer to a turn's last call gets the ending that workload's worker looks for
(`answer_suffix` in `../images/mock-llm/recording.py`).
