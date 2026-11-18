"""The claude-code actor: POST /invoke runs one headless Claude Code turn on
the textkit repo, its body the prompt, and continues the session of the turn
before. It answers with JSON: Claude Code's `result` and `session_id`, the
repo's `commits` since its base, and the `tasks` a session has, so the worker
can check that both the session and the repo survived the suspend between
turns.

POST /invoke?turn=K is turn K of a recorded conversation instead, whose turns
need not fix a test each: turn 1 starts over on a fresh repo and session, and
a later one continues the session."""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO = "/workspace/textkit"
# The tests the repo starts with failing, one fixed per turn; once all are,
# the next turn starts over on a fresh repo and session.
TASKS = 5
# Below the worker's deadline for the call (the case's workload-timeout), so
# a slow turn ends here and its processes are gone before the suspend.
EXEC_TIMEOUT_SECS = float(os.environ.get("CLAUDE_CODE_TIMEOUT_SECS", "100"))

session_id = None
# Set by a turn that did not commit exactly once, which leaves the session and
# the repo out of step.
dirty = False


def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True, text=True).stdout


def commits():
    return int(git("rev-list", "--count", "base..HEAD"))


def start_over():
    global session_id, dirty
    git("reset", "-q", "--hard", "base")
    git("clean", "-qfdx")
    session_id, dirty = None, False


def run_turn(prompt, turn=None):
    """Runs one turn, turn K of a conversation if given, and returns the HTTP
    status and reply."""
    global session_id, dirty
    if turn is None:
        if dirty or commits() >= TASKS:
            start_over()
    elif turn == 1:
        start_over()
    elif dirty or not session_id:
        return 409, f"turn {turn} has no session to continue; start the conversation over"
    before = commits()
    cmd = ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "json"]
    if session_id:
        cmd += ["--resume", session_id]
    proc = subprocess.Popen(
        [*cmd, prompt], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        stdout, stderr = proc.communicate(timeout=EXEC_TIMEOUT_SECS)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        dirty = True
        return 504, f"Claude Code timed out after {EXEC_TIMEOUT_SECS:g}s"
    try:
        result = json.loads(stdout)
    except ValueError:
        result = None
    if proc.returncode != 0 or not result or result.get("is_error"):
        dirty = True
        return 500, f"Claude Code exited with code {proc.returncode}.\nOutput:\n{stdout}{stderr}"
    session_id = result.get("session_id")
    after = commits()
    dirty = turn is None and after != before + 1
    reply = {"result": result.get("result", ""), "session_id": session_id, "commits": after, "tasks": TASKS}
    return 200, json.dumps(reply)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/readyz":
            self.reply(200, "OK")
        else:
            self.reply(404, "not found")

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path != "/invoke":
            self.reply(404, "not found")
            return
        prompt = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
        turn = urllib.parse.parse_qs(url.query).get("turn", [None])[0]
        if turn is not None and (not turn.isdigit() or int(turn) < 1):
            self.reply(400, f"turn {turn!r} is not a number from 1")
            return
        start = time.monotonic()
        try:
            status, body = run_turn(prompt, int(turn) if turn else None)
        except Exception as e:
            global dirty
            dirty = True
            status, body = 500, repr(e)
        self.reply(status, body, {"X-Claude-Code-Exec-Ms": str(round((time.monotonic() - start) * 1000))})

    def reply(self, status, body, headers=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json" if status == 200 and body.startswith("{") else "text/plain")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    print("Starting the Claude Code HTTP wrapper on port 80...")
    sys.exit(HTTPServer(("0.0.0.0", 80), Handler).serve_forever())
