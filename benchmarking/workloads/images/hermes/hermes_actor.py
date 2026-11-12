"""Serves a resident Hermes Agent gateway on port 80.

Starts `hermes gateway run` with its OpenAI-compatible API server on loopback,
and forwards to it: GET /readyz to its /health, and POST /invoke, whose body is
the prompt, to /v1/chat/completions, answering with the reply's text.
"""

import json
import os
import secrets
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERMES_HOME = os.environ.get("HERMES_HOME", "/opt/data")
API_URL = "http://127.0.0.1:8642"
# Generated here, as the API server refuses to start without one.
API_KEY = secrets.token_hex(24)
# Below the worker's deadline for a call (the case's workload-timeout), so a
# slow turn ends here with a 504 that says so.
TURN_TIMEOUT_SECS = float(os.environ.get("HERMES_TIMEOUT_SECS", "100"))

CONFIG = {
    "model": {
        "provider": "custom",
        "model": "mock-model",
        "base_url": os.environ.get("MOCK_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
        "api_key": "mock-key",
        # Read from the endpoint otherwise, which mock-llm does not serve.
        "context_length": 128000,
    },
    # Only the terminal, which mock-llm's scripted loop calls, without the
    # approval prompts an API client could not answer.
    "platform_toolsets": {"api_server": ["terminal"]},
    "approvals": {"mode": "off"},
    # Each would be another call to mock-llm, outside the measured loop.
    "auxiliary": {"title_generation": {"enabled": False}},
    # An actor's egress is limited to the hosts the benchmark needs.
    "plugins": {"auto_update_check_hours": 0},
}


def start_gateway() -> subprocess.Popen:
    with open(os.path.join(HERMES_HOME, "config.yaml"), "w") as f:
        json.dump(CONFIG, f, indent=2)  # YAML is a superset of JSON.
    env = dict(
        os.environ,
        API_SERVER_ENABLED="true",
        API_SERVER_HOST="127.0.0.1",
        API_SERVER_PORT="8642",
        API_SERVER_KEY=API_KEY,
    )
    # The image's entrypoint bootstraps HERMES_HOME and drops to its hermes
    # user, which the gateway insists on.
    return subprocess.Popen(["/opt/hermes/docker/entrypoint-dispatch.sh", "gateway", "run"], env=env)


def exit_with(gateway: subprocess.Popen) -> None:
    """Ends the actor when the gateway does, so it is not left answering 503."""
    code = gateway.wait()
    print(f"hermes gateway exited with {code}", file=sys.stderr)
    os._exit(1)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/readyz":
            self.reply(404, "not found")
            return
        try:
            with urllib.request.urlopen(f"{API_URL}/health", timeout=2) as resp:
                self.reply(resp.status, resp.read().decode())
        except (urllib.error.URLError, OSError) as e:
            self.reply(503, f"hermes not ready: {e}")

    def do_POST(self):
        if self.path != "/invoke":
            self.reply(404, "not found")
            return
        prompt = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        request = urllib.request.Request(
            f"{API_URL}/v1/chat/completions",
            data=json.dumps({"model": "hermes-agent", "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TURN_TIMEOUT_SECS) as resp:
                completion = json.load(resp)
        except urllib.error.HTTPError as e:
            self.reply(e.code, f"hermes answered {e.code}: {e.read().decode(errors='replace')}")
            return
        except TimeoutError:
            self.reply(504, f"hermes turn timed out after {TURN_TIMEOUT_SECS:g}s")
            return
        except (urllib.error.URLError, OSError) as e:
            # Before the gateway listens; the worker retries a 503.
            self.reply(503, f"hermes unreachable: {e}")
            return
        self.reply(200, completion["choices"][0]["message"]["content"] or "")

    def reply(self, status: int, body: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())


if __name__ == "__main__":
    gateway = start_gateway()
    threading.Thread(target=exit_with, args=(gateway,), daemon=True).start()
    print("hermes actor listening on port 80")
    ThreadingHTTPServer(("0.0.0.0", 80), Handler).serve_forever()
