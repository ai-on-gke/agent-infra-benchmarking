# The openclaw workload for run.sh: one openclaw agent turn per prompt, on
# mock-llm's OpenAI Chat Completions API.
actor_image=openclaw
actor_env=(-e OPENCLAW_USE_MOCK_LLM=true -e MOCK_LLM_BASE_URL=http://mock-llm:8080/v1)
# The worker's prompt (locust/go/internal/workload/openclaw.go).
worker_prompt="do something please"
default_turns=3
record_needs=(OPENAI_API_KEY OPENAI_UPSTREAM_MODEL)

# check_reply REPLY NONCE I OWN_PROMPT: the worker's check, which mock-llm's
# answer meets for any prompt.
check_reply() {
  python3 - "$@" <<'PY'
import sys
reply, nonce, i = sys.argv[1], sys.argv[2], sys.argv[3]
want = f"I have successfully executed the bash commands! (request {nonce})"
assert want in reply, f"no {want!r} in {reply[-2000:]!r}"
answer = next(line for line in reply.splitlines() if nonce in line)
print(f"turn {i}: {answer.strip()[-300:]}")
PY
}
