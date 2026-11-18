# openclaw actor

The actor the `openclaw` test case drives: a small HTTP wrapper,
`openclaw_actor.py`, around a headless [openclaw](https://www.npmjs.com/package/openclaw)
agent turn. `POST /invoke` runs one turn with the request body as the prompt,
in a session of its own, through the openclaw gateway that `gateway.py`
starts and warms up at boot, so the golden snapshot holds it running.

The benchmark runs it against `mock-llm` (`../mock-llm`), which the
`openclaw` ActorTemplate selects with `OPENCLAW_USE_MOCK_LLM=true`. The mock
answers with a scripted tool-calling loop, so a run measures substrate and the
agent's own work rather than a model's latency, and it needs no Google Cloud
credentials, no service account and no key.

The scenario-runner builds and pushes the image (`../../images.json`) and
deploys the template and `mock-llm` for every `openclaw` case
(`benchmarking/README.md`); there is nothing to set up by hand.

The worker puts a nonce in each prompt, `(request <uuid>)`, which `mock-llm`
repeats at the end of its last answer, so the worker counts a turn only if its
output carries that turn's nonce.

The `openclaw-replay` case replays a recorded conversation instead
(`../../recorder`): the worker sends its prompts as `POST /invoke?turn=K`,
which `openclaw agent --local` runs in the `../textkit` repo baked into the
image, turn 1 in a new session on a fresh repo and a later turn continuing
both, and mock-llm answers from the cassette. A turn counts if it carries the
nonce and its `(turn N)` is K.
