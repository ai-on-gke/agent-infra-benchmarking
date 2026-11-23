# mock-llm

The model the `claude-code` and `openclaw` actors call in place of a real one,
so a run measures substrate and the agent's own work rather than a model's
latency, and needs no API key. It serves the Anthropic Messages API for
claude-code and the OpenAI Chat Completions API for openclaw, on port 8080.
The scenario-runner deploys it with those workloads, on the node pool of
`infra.cluster.configuration: mock-llm`.

## Answering with a scripted turn

By default (`MOCK_LLM_MODE=script`) it scripts each workload's turn as tool
calls: for claude-code, run the textkit tests, fix the first failing one and
commit; for openclaw, write, test and fix a small app. Its last answer repeats
the prompt's nonce, so the worker counts only turns that finished.

## Replaying a recorded session

With `MOCK_LLM_MODE=replay` it answers only from a recording of an agent's
real session with a model, `recordings/<name>/cassette.jsonl`, so the agent
gets the same answers every run, instantly or at the pace they were recorded
(`REPLAY_TIMING=recorded`). It also serves the recording's prompts, from its
`turns.txt`, for the worker to send. A call the recording has no answer for
fails, and the log shows the conversation that missed.

A case replays one with `recording` in its `locust`, as `{workload}-replay` in
`scenarios.yaml` does:

```yaml
      recording:
        name: claude-code-textkit-fix-all
```

The image bakes in `recordings/`. To make or check a recording, see
[the recorder](../../recorder/README.md).
