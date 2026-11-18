"""The openclaw actor: POST /invoke runs one headless openclaw agent turn, its
body the prompt, each in a session of its own.

POST /invoke?turn=K is turn K of a recorded conversation instead, in the
textkit repo: turn 1 starts a new session on a fresh repo, and a later one
continues both."""

import sys
import os
import signal
import subprocess
import json
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

# How long one `openclaw agent exec` may run. Kept below the benchmark worker's
# deadline for the call (the case's workload-timeout), so a slow turn ends here
# with a 504 that says so, and its processes are gone before the worker
# suspends the actor.
EXEC_TIMEOUT_SECS = float(os.environ.get("OPENCLAW_TIMEOUT_SECS", "100"))
REPO = "/workspace/textkit"

# The conversation's session; None until its turn 1, and after a turn that
# failed, which may have left it half done.
session_id = None

# MAIN ACTOR SERVER (Runs on port 80)
# ==========================================
class OpenClawHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/readyz":
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global session_id
        url = urllib.parse.urlsplit(self.path)
        if url.path.startswith("/invoke"):
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length).decode('utf-8')
            turn = urllib.parse.parse_qs(url.query).get("turn", [None])[0]
            if turn is not None and (not turn.isdigit() or int(turn) < 1):
                self.reply(400, f"turn {turn!r} is not a number from 1", time.monotonic())
                return
            turn = int(turn) if turn else None
            if turn is not None and turn > 1 and not session_id:
                self.reply(409, f"turn {turn} has no session to continue; start the conversation over", time.monotonic())
                return

            # If no input is provided, provide a fast default prompt to measure baseline latency
            if turn is None and (not post_data.strip() or post_data.strip() == "echo 'hello'"):
                # For high-throughput load testing, we bypass the heavy Node.js boot
                # to prevent Envoy from timing out the connection (>5s).
                # This isolates Substrate's exact routing and network latency!
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Mock Substrate Benchmark Response")
                return

            ok = False
            try:
                import base64

                env = os.environ.copy()
                use_mock = env.get("OPENCLAW_USE_MOCK_LLM", "").lower() in ["true", "1", "yes"]

                if use_mock:
                    base_url = env.get("MOCK_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
                    config = {
                        "models": {
                            "providers": {
                                "mock-provider": {
                                    "api": "openai-completions",
                                    "baseUrl": base_url,
                                    # openclaw reads no env var for a custom provider's key,
                                    # so without this every turn fails with missing-provider-auth.
                                    "apiKey": "mock-key",
                                    "models": [{"id": "mock-model", "name": "mock-model"}]
                                }
                            }
                        }
                    }
                    if turn is not None:
                        # skipBootstrap keeps openclaw's own files out of the repo.
                        config["agents"] = {"defaults": {"workspace": REPO, "skipBootstrap": True}}
                    with open("/tmp/mock-config.json", "w") as f:
                        json.dump(config, f)

                    model = "mock-provider/mock-model"
                    # `openclaw agent` takes its config from the environment.
                    env["OPENCLAW_CONFIG_PATH"] = "/tmp/mock-config.json"
                    cmd = ["openclaw", "agent", "exec", "--config", "/tmp/mock-config.json", "--model", model, post_data]
                else:
                    # Unused by the benchmark, whose template sets OPENCLAW_USE_MOCK_LLM: nothing
                    # in this repo sets GCP_SA_KEY_B64. An ActorTemplate cannot mount a
                    # Kubernetes Secret, so a key would have to arrive base64-encoded in the env.
                    if "GCP_SA_KEY_B64" in env and env["GCP_SA_KEY_B64"]:
                        key_path = "/tmp/gcp_key.json"
                        with open(key_path, "wb") as f:
                            f.write(base64.b64decode(env["GCP_SA_KEY_B64"]))
                        env["GOOGLE_APPLICATION_CREDENTIALS"] = key_path

                    # Use `openclaw agent exec` to run an isolated headless agent turn.
                    model = "google-vertex/gemini-2.5-flash-lite"
                    cmd = ["openclaw", "agent", "exec", "--isolated", "--model", model, post_data]

                # `agent exec` starts a new session every time; `agent --local`
                # runs the same embedded agent in the session it names.
                if turn is not None:
                    if turn == 1:
                        session_id = str(uuid.uuid4())
                        for args in (["reset", "-q", "--hard", "base"], ["clean", "-qfdx"]):
                            subprocess.run(["git", *args], cwd=REPO, check=True)
                    cmd = ["openclaw", "agent", "--local", "--session-id", session_id, "--model", model,
                           "--message", post_data]

                # Its own session, so a timeout can kill openclaw together with
                # every process it started.
                start = time.monotonic()
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    start_new_session=True,
                )
                try:
                    stdout, stderr = proc.communicate(timeout=EXEC_TIMEOUT_SECS)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()
                    self.reply(504, f"OpenClaw execution timed out after {EXEC_TIMEOUT_SECS:g}s", start)
                    return

                output = stdout + stderr
                if proc.returncode != 0:
                    self.reply(500, f"OpenClaw exited with code {proc.returncode} after "
                               f"{time.monotonic() - start:.1f}s.\nOutput:\n{output}", start)
                    return
                ok = True
                self.reply(200, output, start)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))
            finally:
                if turn is not None and not ok:
                    session_id = None
        else:
            self.send_response(404)
            self.end_headers()

    def reply(self, status, body, start):
        """Answers an /invoke, with how long openclaw ran in X-Openclaw-Exec-Ms."""
        self.send_response(status)
        self.send_header("Content-type", "text/plain")
        self.send_header("X-Openclaw-Exec-Ms", str(round((time.monotonic() - start) * 1000)))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


if __name__ == "__main__":
    print("Starting OpenClaw HTTP wrapper on port 80...")
    server = HTTPServer(("0.0.0.0", 80), OpenClawHandler)
    sys.exit(server.serve_forever())
