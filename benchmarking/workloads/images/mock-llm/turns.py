"""mock-llm's second entrypoint, the recorder's, which its run.sh runs: starts
mock-llm's server, which scripts, records or replays the agent's calls, and
sends a workload's actor its turns as the benchmark's worker does, checking
each reply.

    python3 /app/turns.py WORKLOAD [script | record NAME [PROMPT | -i] | replay NAME] [--turns N]

A recording NAME is a folder in /recordings holding its cassette.jsonl.
script sends the worker's prompt. record sends PROMPT in its place if given,
saved to prompt.txt, or with -i asks for each turn's prompt once the reply to
the one before is shown, saved to turns.txt; replay sends what the recording
was made with. TURNS in the environment is --turns' default."""

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

SERVER = "http://localhost:8080"
SERVER_LOG = "/tmp/mock-llm.log"


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


# Each workload's worker prompt, how many turns it sends, its check and the
# variables recording it needs.
WORKLOADS = {
    "claude-code": ("Fix the next failing test and commit the fix.", 6, check_claude_code, ["ANTHROPIC_API_KEY"]),
    "openclaw": ("do something please", 3, check_openclaw, ["OPENAI_API_KEY", "OPENAI_UPSTREAM_MODEL"]),
}


def wait_ready(url, secs=30, server=None):
    for _ in range(secs):
        if server and server.poll() is not None:
            sys.exit("mock-llm's server exited")
        try:
            urllib.request.urlopen(f"{url}/readyz", timeout=1).close()
            return
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    sys.exit(f"{url} is not ready after {secs}s")


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
    parser.add_argument("mode", nargs="?", default="script", choices=("script", "record", "replay"))
    parser.add_argument("name", nargs="?", help="record and replay: the recording's name")
    parser.add_argument("prompt", nargs="?", help="record only: the prompt to send")
    parser.add_argument("-i", dest="interactive", action="store_true", help="record only: ask for each turn's prompt")
    parser.add_argument("--turns", type=int, default=os.environ.get("TURNS") or None, help="how many turns to send")
    parser.add_argument("--actor", default="http://actor", help="the actor's URL")
    parser.add_argument("--recordings", default="/recordings", help="the recordings' folder")
    args = parser.parse_args()

    if (args.name is None) != (args.mode == "script"):
        parser.error("record and replay take a recording's name, script none")
    if args.name in (".", "..") or "/" in (args.name or ""):
        parser.error(f"a recording's name is a folder name, got {args.name}")
    if (args.prompt is not None or args.interactive) and args.mode != "record":
        parser.error(f"a prompt is for record, not {args.mode}")
    if args.prompt is not None and args.interactive:
        parser.error("give a prompt or -i, not both")

    record_needs = WORKLOADS[args.workload][3]
    folder = f"{args.recordings}/{args.name}"
    cassette = f"{folder}/cassette.jsonl"
    if args.mode == "replay" and not os.path.exists(cassette):
        have = sorted(d for d in os.listdir(args.recordings) if os.path.exists(f"{args.recordings}/{d}/cassette.jsonl"))
        sys.exit(f"no recording {args.name} to replay; recordings: {' '.join(have) or 'none'}")
    if args.mode == "record":
        missing = [name for name in record_needs if not os.environ.get(name)]
        if missing:
            sys.exit(f"recording {args.workload} needs {', '.join(missing)}, in .env or the environment")
        if os.path.exists(cassette) and os.path.getsize(cassette):
            sys.exit(f"recording {args.name} exists; delete its folder or pick another name")
        os.makedirs(folder, exist_ok=True)

    env = {**os.environ, "MOCK_LLM_MODE": args.mode}
    if args.name:
        env["CASSETTE"] = cassette
    with open(SERVER_LOG, "w") as log:
        # In a session of its own, so ^C stops the turns, not the server.
        server = subprocess.Popen(
            [sys.executable, "-u", os.path.join(os.path.dirname(__file__), "mock_llm.py")],
            env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    passed = False
    try:
        wait_ready(SERVER, server=server)
        send_turns(args, folder)
        passed = True
        with urllib.request.urlopen(f"{SERVER}/stats") as resp:
            stats = json.load(resp)
        print(f"mock-llm answered {stats['calls']} agent calls")
        if args.mode == "record":
            print(f"recorded {stats['saved']} answers to {args.name}")
    finally:
        server.terminate()
        server.wait()
        if not passed:
            with open(SERVER_LOG) as f:
                tail = f.readlines()[-20:]
            print("--- mock-llm's log, last 20 lines", "".join(tail), sep="\n", end="", file=sys.stderr)
        if args.mode == "record":
            drop_if_empty(folder, cassette)


def send_turns(args, folder):
    worker_prompt, default_turns, check, _ = WORKLOADS[args.workload]
    prompt_file, turns_file = f"{folder}/prompt.txt", f"{folder}/turns.txt"
    turns, prompt = args.turns or default_turns, worker_prompt

    if args.mode == "record":
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
    elif args.mode == "replay" and os.path.exists(turns_file):
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
    elif args.mode == "replay" and os.path.exists(prompt_file):
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


def drop_if_empty(folder, cassette):
    """A recording that saved nothing leaves no folder behind."""
    if os.path.exists(cassette) and os.path.getsize(cassette):
        return
    for name in ("cassette.jsonl", "prompt.txt", "turns.txt"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(f"{folder}/{name}")
    with contextlib.suppress(OSError):
        os.rmdir(folder)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
