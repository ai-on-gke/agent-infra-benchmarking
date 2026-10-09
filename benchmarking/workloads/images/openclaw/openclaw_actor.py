import sys
import os
import signal
import subprocess
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# How long one `openclaw agent exec` may run. Kept below the benchmark worker's
# deadline for the call (the case's workload-timeout), so a slow turn ends here
# with a 504 that says so, and its processes are gone before the worker
# suspends the actor.
EXEC_TIMEOUT_SECS = float(os.environ.get("OPENCLAW_TIMEOUT_SECS", "100"))

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
        if self.path.startswith("/invoke"):
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length).decode('utf-8')

            # If no input is provided, provide a fast default prompt to measure baseline latency
            if not post_data.strip() or post_data.strip() == "echo 'hello'":
                # For high-throughput load testing, we bypass the heavy Node.js boot
                # to prevent Envoy from timing out the connection (>5s).
                # This isolates Substrate's exact routing and network latency!
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Mock Substrate Benchmark Response")
                return

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
                                    "models": [{"id": "mock-model", "name": "mock-model"}]
                                }
                            }
                        }
                    }
                    with open("/tmp/mock-config.json", "w") as f:
                        json.dump(config, f)

                    env["OPENCLAW_MOCK_PROVIDER_API_KEY"] = "mock-key"
                    cmd = ["openclaw", "agent", "exec", "--config", "/tmp/mock-config.json", "--model", "mock-provider/mock-model", post_data]
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
                    cmd = ["openclaw", "agent", "exec", "--isolated", "--model", "google-vertex/gemini-2.5-flash-lite", post_data]

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
                self.reply(200, output, start)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))
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
