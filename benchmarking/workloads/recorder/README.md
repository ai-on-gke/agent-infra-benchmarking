# Recording workloads locally

Records an agent's calls to a real model as a named recording in
[mock-llm's recordings](../images/mock-llm/recordings), which the benchmark
replays, and replays it locally, so the agent gets the same answers every run:
`./run.sh claude-code record claude-code-textkit-fix-all -i`, then
`./run.sh claude-code replay claude-code-textkit-fix-all`. It runs a workload's actor
beside [mock-llm](../images/mock-llm) in Docker and, from mock-llm's
container, sends it turns as the benchmark's worker does and checks each
reply.

It knows the workloads that use mock-llm: `claude-code` and `openclaw`.

## Recording a session with a real model

Put the keys in `.env`, which git ignores: `ANTHROPIC_API_KEY` for
claude-code, which talks the Anthropic API, and `ANTHROPIC_UPSTREAM` for an
API other than Anthropic's; for openclaw, which talks the OpenAI Chat
Completions API, `OPENAI_API_KEY`, the model to record with in
`OPENAI_UPSTREAM_MODEL` and, for an API other than OpenAI's,
`OPENAI_UPSTREAM`. Then run the turns with mock-llm forwarding the agent's
calls to the API and saving each answer to
`../images/mock-llm/recordings/<name>/cassette.jsonl`:

```bash
./run.sh claude-code record my-recording
```

Only mock-llm gets the keys; the actor keeps its dummy one. The script refuses
to record over an existing cassette, so pick another name or delete it first.

To type each turn's prompt once the reply to the one before is shown, give
`-i`; an empty line ends the recording, and the prompts are saved to the
recording's `turns.txt` for the replay to send:

```bash
./run.sh claude-code record my-recording -i
```

To send a prompt of your own every turn rather than the worker's, give it as
an argument; it is saved to the recording's `prompt.txt`:

```bash
./run.sh claude-code record my-recording "Add type hints to textkit/case.py and commit them."
```

## Replaying a recording

Run the same turns, with the prompts the recording was made with, and mock-llm
answering only from its cassette:

```bash
./run.sh claude-code replay my-recording
```

A call the cassette has no answer for fails the turn, and mock-llm's log shows
the conversation that missed. To replay at the pace the answers were recorded
rather than at once:

```bash
REPLAY_TIMING=recorded ./run.sh claude-code replay my-recording
```

## Replaying a recording in a benchmark case

The mock-llm image bakes in `../images/mock-llm/recordings`, so commit the
recording's folder. A case replays it with `recording` in its `locust`, as
`{workload}-replay` in `scenarios.yaml` does:

```yaml
      recording:
        name: claude-code-textkit-fix-all
        # Optional; instant by default.
        timing: recorded
```

Only a recording made with `-i` can be: the worker sends the prompts of its
`turns.txt`, which it gets from mock-llm, one a turn and from the first again
after the last.

## Running the scripted check

With no mode, mock-llm scripts the agent's turns, so a workload can be tried
without a key or a recording:

```bash
./run.sh claude-code
```

A run that passes ends by counting the calls mock-llm answered, and one that
fails by printing the end of mock-llm's log; either removes the containers.
`TURNS` sets how many turns to send:

```bash
TURNS=2 ./run.sh openclaw
```

## Adding a workload

Add a branch for it to the `case $workload` in `run.sh`, which sets
`actor_env`: the `docker run` options that point the actor, built from its
folder in `../images`, at `http://mock-llm:8080`.

The rest is mock-llm's [`turns.py`](../images/mock-llm/turns.py), which
`run.sh` runs in mock-llm's container: it starts mock-llm's server and calls
the actor at `http://actor`. Add the workload to its `WORKLOADS`: the worker's
prompt, how many turns it sends, its check of each reply and the variables
recording needs. A variable other than the ones `run.sh` passes on also needs
adding to its list.

mock-llm records the Anthropic Messages, OpenAI Chat Completions and OpenAI
Responses APIs; the answer to a turn's last call gets the ending that
workload's worker looks for (`answer_suffix` in
`../images/mock-llm/recording.py`).
