# The claude-code workload for run.sh: Claude Code fixing textkit's failing
# tests, one a turn, in a session each turn continues; turn 6 starts over.
actor_image=claude-code
actor_env=(-e ANTHROPIC_BASE_URL=http://mock-llm:8080)
# The worker's prompt (locust/go/internal/workload/claudecode.go).
worker_prompt="Fix the next failing test and commit the fix."
default_turns=6
record_needs=(ANTHROPIC_API_KEY)

# check_reply REPLY NONCE I OWN_PROMPT: the worker's checks. A prompt of your
# own need not commit once a turn, which the turn checks rest on, so its
# replies are checked only for the nonce.
check_reply() {
  python3 - "$@" <<'PY'
import json, re, sys
reply, nonce, i, own_prompt = json.loads(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4] == "1"
result = reply["result"]
assert f"(request {nonce})" in result, f"no nonce in {result!r}"
turn = int(re.search(r"\(turn (\d+)\)", result)[1])
want = (i - 1) % 5 + 1
if not own_prompt:
    assert turn == want == reply["commits"], f"want turn {want}, got turn {turn} at commit {reply['commits']}"
print(f"turn {turn}, {reply['commits']} commits: {result}")
PY
}
