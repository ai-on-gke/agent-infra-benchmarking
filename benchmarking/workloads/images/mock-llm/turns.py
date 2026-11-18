"""mock-llm's second entrypoint, for the recorder's run.sh: sends a workload's
actor its turns as the benchmark's worker does and checks each reply, while
this container's server scripts, records or replays the agent's calls, as
MOCK_LLM_MODE says.

    python3 /app/turns.py WORKLOAD [PROMPT | -i] [--turns N]

script sends the worker's prompt. record sends PROMPT in its place if given,
saved to prompt.txt, or with -i asks for each turn's prompt once the reply to
the one before is shown, saved to turns.txt; replay sends what the cassette
was recorded with."""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid


def check_claude_code(reply, nonce, i, own_prompt):
    """The worker's checks (locust/go/internal/workload/claudecode.go). A
    prompt of your own need not commit once a turn, which the turn checks rest
    on, so its replies are checked only for the nonce."""
    reply = json.loads(reply)
    result = reply["result"]
    assert f"(request {nonce})" in result, f"no nonce in {result!r}"
    turn = int(re.search(r"\(turn (\d+)\)", result)[1])
    want = (i - 1) % 5 + 1
    if not own_prompt:
        assert turn == want == reply["commits"], f"want turn {want}, got turn {turn} at commit {reply['commits']}"
    return f"turn {turn}, {reply['commits']} commits: {result}"


def check_openclaw(reply, nonce, i, own_prompt):
    """The worker's check (locust/go/internal/workload/openclaw.go), which
    mock-llm's answer meets for any prompt."""
    want = f"I have successfully executed the bash commands! (request {nonce})"
    assert want in reply, f"no {want!r} in {reply[-2000:]!r}"
    answer = next(line for line in reply.splitlines() if nonce in line)
    return f"turn {i}: {answer.strip()[-300:]}"


# Each workload's worker prompt, how many turns it sends and its check.
WORKLOADS = {
    "claude-code": ("Fix the next failing test and commit the fix.", 6, check_claude_code),
    "openclaw": ("do something please", 3, check_openclaw),
}


def wait_ready(actor, secs=30):
    for _ in range(secs):
        try:
            urllib.request.urlopen(f"{actor}/readyz", timeout=1).close()
            return
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    sys.exit(f"{actor} is not ready after {secs}s")


class Turns:
    def __init__(self, actor, check, own_prompt, conversation):
        self.actor, self.check, self.own_prompt, self.conversation = actor, check, own_prompt, conversation

    def send(self, i, prompt):
        """Sends turn i and checks its reply. A conversation's says which turn
        it is, which the actor starts over on at 1."""
        nonce = str(uuid.uuid4())
        url = f"{self.actor}/invoke" + (f"?turn={i}" if self.conversation else "")
        request = urllib.request.Request(
            url, data=f"{prompt} (request {nonce})".encode("utf-8"), headers={"Content-Type": "text/plain"}
        )
        try:
            with urllib.request.urlopen(request) as resp:
                reply = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            sys.exit(f"turn {i} failed: {e.code} {e.read().decode('utf-8', 'replace')[:2000]}")
        try:
            print(self.check(reply, nonce, i, self.own_prompt), flush=True)
        except (AssertionError, KeyError, TypeError, ValueError, StopIteration) as e:
            sys.exit(f"turn {i}'s reply fails the check: {e!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("workload", choices=WORKLOADS)
    parser.add_argument("prompt", nargs="?", help="record only: the prompt to send")
    parser.add_argument("-i", dest="interactive", action="store_true", help="record only: ask for each turn's prompt")
    parser.add_argument("--turns", type=int, help="how many turns to send")
    parser.add_argument("--actor", default="http://actor", help="the actor's URL")
    parser.add_argument("--dir", default="/cassettes", help="the cassette's folder")
    args = parser.parse_args()

    worker_prompt, default_turns, check = WORKLOADS[args.workload]
    mode = os.environ.get("MOCK_LLM_MODE", "script")
    if (args.prompt is not None or args.interactive) and mode != "record":
        parser.error(f"a prompt is for record, not {mode}")
    prompt_file, turns_file = f"{args.dir}/prompt.txt", f"{args.dir}/turns.txt"
    turns, prompt = args.turns or default_turns, worker_prompt

    if mode == "record":
        for path in (prompt_file, turns_file):
            if os.path.exists(path):
                os.remove(path)
        if args.interactive:
            wait_ready(args.actor)
            record_conversation(Turns(args.actor, check, True, True), turns_file)
            return
        prompt = args.prompt or worker_prompt
        with open(prompt_file, "w") as f:
            f.write(prompt + "\n")
    elif mode == "replay" and os.path.exists(turns_file):
        with open(turns_file) as f:
            prompts = f.read().splitlines()
        turns = args.turns or len(prompts)
        if turns > len(prompts):
            sys.exit(f"{turns_file} has only {len(prompts)} turns")
        wait_ready(args.actor)
        sender = Turns(args.actor, check, True, True)
        for i, prompt in enumerate(prompts[:turns], 1):
            print(f"turn {i}> {prompt}", flush=True)
            sender.send(i, prompt)
        return
    elif mode == "replay" and os.path.exists(prompt_file):
        with open(prompt_file) as f:
            prompt = f.read().rstrip("\n")

    print(f"prompt: {prompt}", flush=True)
    wait_ready(args.actor)
    sender = Turns(args.actor, check, prompt != worker_prompt, False)
    for i in range(1, turns + 1):
        sender.send(i, prompt)


def record_conversation(sender, turns_file):
    import readline  # noqa: F401, for input()'s line editing

    print("Type each turn's prompt; an empty line ends the recording.", flush=True)
    i = 0
    while True:
        try:
            prompt = input(f"turn {i + 1}> ").strip()
        except EOFError:
            print()
            break
        if not prompt:
            break
        i += 1
        sender.send(i, prompt)
        # Saved once answered, so the file has only the turns the cassette does.
        with open(turns_file, "a") as f:
            f.write(prompt + "\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
