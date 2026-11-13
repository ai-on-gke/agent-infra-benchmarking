# claude-code actor

The actor the `claude-code` workload drives: a small HTTP wrapper,
`claude_code_actor.py`, around headless [Claude Code](https://www.npmjs.com/package/@anthropic-ai/claude-code)
(`claude -p --dangerously-skip-permissions`), working on `textkit/`, a small
Python repo baked into the image with five failing tests. `POST /invoke` runs
one turn with the request body as the prompt.

Each turn continues the session of the turn before (`--resume`), so the
session file and the repo's commits have to survive the suspend between
turns. The benchmark runs it against `mock-llm` (`../mock-llm`), which scripts
the turn with Claude Code's own tools: run the tests, read the file under the
first failing one, edit it, run that test, commit. After five turns every test
passes, and the next turn starts over on a fresh repo and session; so does the
turn after a failed one.

The reply is JSON: Claude Code's `result`, which ends with mock-llm's
`(turn N) (request <nonce>)`, the `session_id`, the repo's `commits` since its
base and the session's `tasks`. The worker counts a turn only if it carries
the turn's nonce, its turn and commits agree, and both are one more than the
actor's last turn.

To change Claude Code's version, change it in `package.json` and regenerate
`package-lock.json` with `npm install --package-lock-only` in the base image.
