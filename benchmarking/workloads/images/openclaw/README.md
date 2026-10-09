# openclaw actor

The actor the `openclaw` test case drives: a small HTTP wrapper,
`openclaw_actor.py`, around a headless [openclaw](https://www.npmjs.com/package/openclaw)
agent turn. `POST /invoke` runs one turn with the request body as the prompt.

The benchmark runs it against `mock-llm` (`../mock-llm`), which the
`openclaw` ActorTemplate selects with `OPENCLAW_USE_MOCK_LLM=true`. The mock
answers with a scripted tool-calling loop, so a run measures substrate and the
agent's own work rather than a model's latency, and it needs no Google Cloud
credentials, no service account and no key.

`deploy-benchmarking.sh --deploy` builds and pushes the image and deploys the
template and `mock-llm`; there is nothing to set up by hand.
