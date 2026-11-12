import base64
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

EXEC_TIMEOUT_SECS = float(os.environ.get("OPENCLAW_TIMEOUT_SECS", "100"))
GATEWAY_PORT = int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18789"))
WARMUP_PORT = 18080
GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "benchmark-token")

BASE_DIR = "/tmp/openclaw_home"
CONFIG_PATH = os.path.join(BASE_DIR, "openclaw.json")

gateway_proc = None


class WarmupHandler(BaseHTTPRequestHandler):
    """Temporary loopback SSE responder used strictly during container boot to warm
    up the OpenClaw Gateway before port 80 is opened for the golden snapshot."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            req_data = json.loads(body.decode("utf-8"))
            messages = req_data.get("messages", [])
            assistant_count = sum(1 for m in messages if m.get("role") == "assistant")
        except Exception:
            assistant_count = 0

        if assistant_count == 0:
            c1 = {
                "id": "w1",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "warmup-model",
                "choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_w",
                            "type": "function",
                            "function": {"name": "exec", "arguments": json.dumps({"command": "true", "title": "w"})},
                        }],
                    },
                    "finish_reason": None,
                }],
            }
            c2 = {"id": "w1", "object": "chat.completion.chunk", "created": 1, "model": "warmup-model", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
        else:
            c1 = {"id": "w2", "object": "chat.completion.chunk", "created": 1, "model": "warmup-model", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]}
            c2 = {"id": "w2", "object": "chat.completion.chunk", "created": 1, "model": "warmup-model", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}

        sse = f"data: {json.dumps(c1)}\n\ndata: {json.dumps(c2)}\n\ndata: [DONE]\n\n".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(sse)))
        self.end_headers()
        self.wfile.write(sse)


def setup_openclaw_config():
    """Configures the Gateway in /tmp before launch."""
    for sub in ["workspace", "agents/main/agent", "agents/main/sessions", "state"]:
        os.makedirs(os.path.join(BASE_DIR, sub), exist_ok=True)

    env = os.environ.copy()
    use_mock = env.get("OPENCLAW_USE_MOCK_LLM", "").lower() in ["true", "1", "yes"]

    warmup_provider = {
        "api": "openai-completions",
        "baseUrl": f"http://127.0.0.1:{WARMUP_PORT}/v1",
        "apiKey": "mock-key",
        "models": [{"id": "warmup-model", "name": "warmup-model"}],
    }

    config = {
        "gateway": {
            "mode": "local",
            "auth": {
                "mode": "token",
                "token": GATEWAY_TOKEN,
            },
            "controlUi": {
                "enabled": False,
                "dangerouslyDisableDeviceAuth": True,
            },
            "http": {
                "endpoints": {
                    "chatCompletions": {
                        "enabled": True,
                    }
                }
            },
        },
        "plugins": {
            "enabled": False,
        },
        "meta": {
            "migrations": {
                "modelPolicyAllowlist": True,
                "utilityModelSeparation": True,
            },
            "lastTouchedVersion": "2026.10.5",
        },
    }

    if use_mock:
        base_url = env.get("MOCK_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
        model_name = "mock-provider/mock-model"
        config["models"] = {
            "mode": "replace",
            "providers": {
                "mock-provider": {
                    "api": "openai-completions",
                    "baseUrl": base_url,
                    "apiKey": "mock-key",
                    "models": [{"id": "mock-model", "name": "mock-model"}],
                },
                "warmup-provider": warmup_provider,
            },
        }
    else:
        model_name = "google-vertex/gemini-2.5-flash-lite"
        if "GCP_SA_KEY_B64" in env and env["GCP_SA_KEY_B64"]:
            key_path = "/tmp/gcp_key.json"
            with open(key_path, "wb") as f:
                f.write(base64.b64decode(env["GCP_SA_KEY_B64"]))
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key_path
        config["models"] = {
            "providers": {
                "warmup-provider": warmup_provider,
            },
        }

    config["agents"] = {
        "entries": {
            "main": {
                "name": "main",
                "workspace": os.path.join(BASE_DIR, "workspace"),
                "agentDir": os.path.join(BASE_DIR, "agents", "main", "agent"),
                "identity": {"name": "main"},
                "model": model_name,
            }
        },
        "defaults": {
            "workspace": os.path.join(BASE_DIR, "workspace"),
            "model": {"primary": model_name},
        },
    }

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def start_and_warmup_gateway():
    """Starts the Gateway and executes one warmup turn before port 80 is opened."""
    global gateway_proc

    # 1. Start temporary loopback warmup server on 18080
    warmup_server = HTTPServer(("127.0.0.1", WARMUP_PORT), WarmupHandler)
    threading.Thread(target=warmup_server.serve_forever, daemon=True).start()

    # 2. Start OpenClaw Gateway as background daemon
    sys.stderr.write(f"Starting OpenClaw Gateway on 127.0.0.1:{GATEWAY_PORT}...\n")
    sys.stderr.flush()

    cmd = [
        "openclaw",
        "gateway",
        "run",
        "--bind",
        "loopback",
        "--port",
        str(GATEWAY_PORT),
        "--allow-unconfigured",
    ]
    env = os.environ.copy()
    env["HOME"] = BASE_DIR
    env["OPENCLAW_CONFIG_PATH"] = CONFIG_PATH
    env["OPENCLAW_STATE_DIR"] = os.path.join(BASE_DIR, "state")

    gateway_proc = subprocess.Popen(
        cmd,
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=env,
        start_new_session=True,
    )

    # 3. Wait for Gateway to report ready on /readyz
    t0 = time.monotonic()
    ready_url = f"http://127.0.0.1:{GATEWAY_PORT}/readyz"
    while True:
        if gateway_proc.poll() is not None:
            raise RuntimeError(f"OpenClaw Gateway exited early with code {gateway_proc.returncode}")
        try:
            with urllib.request.urlopen(ready_url, timeout=1.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("ready") is True:
                        break
        except Exception:
            pass
        time.sleep(0.2)

    sys.stderr.write(f"OpenClaw Gateway listening after {time.monotonic() - t0:.1f}s, warming up...\n")
    sys.stderr.flush()

    # 4. Run local warmup turn so the JIT code, HTTP endpoint, and SQLite databases are initialized
    chat_url = f"http://127.0.0.1:{GATEWAY_PORT}/v1/chat/completions"
    payload = json.dumps({
        "model": "openclaw",
        "messages": [{"role": "user", "content": "warmup"}],
    }).encode("utf-8")
    req = urllib.request.Request(
        chat_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GATEWAY_TOKEN}",
            "x-openclaw-model": "warmup-provider/warmup-model",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60.0) as resp:
        resp.read()

    # Clean up warmup server
    warmup_server.shutdown()

    # Clean up BOOTSTRAP.md if created so it doesn't leak into benchmark turns
    bootstrap_md = os.path.join(BASE_DIR, "workspace", "BOOTSTRAP.md")
    if os.path.exists(bootstrap_md):
        try:
            os.remove(bootstrap_md)
        except OSError:
            pass

    sys.stderr.write(f"OpenClaw Gateway fully warmed after {time.monotonic() - t0:.1f}s!\n")
    sys.stderr.flush()


# MAIN ACTOR SERVER (Runs on port 80)
# ==========================================
class OpenClawHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

    def log_message(self, *args):
        # Do not spam container stdout on every probe/request
        pass

    def do_GET(self):
        if self.path == "/readyz":
            # Gateway is guaranteed warm and running in the golden snapshot.
            # Answer instantly with 200 OK so Substrate's wakeupProbe takes <1ms.
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        if self.path.startswith("/invoke"):
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length).decode("utf-8")

            # Fast-path default prompt to measure baseline network/routing latency
            if not post_data.strip() or post_data.strip() == "echo 'hello'":
                body = b"Mock Substrate Benchmark Response"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            start = time.monotonic()
            try:
                payload = json.dumps({
                    "model": "openclaw",
                    "user": uuid.uuid4().hex,
                    "messages": [{"role": "user", "content": post_data}],
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{GATEWAY_PORT}/v1/chat/completions",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {GATEWAY_TOKEN}",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=EXEC_TIMEOUT_SECS) as resp:
                        raw_body = resp.read().decode("utf-8")
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode("utf-8", errors="replace")
                    self.reply(
                        500,
                        f"OpenClaw Gateway HTTP {e.code} after {time.monotonic() - start:.1f}s:\n{err_body}",
                        start,
                    )
                    return
                except (TimeoutError, socket.timeout) as e:
                    self.reply(504, f"OpenClaw execution timed out after {EXEC_TIMEOUT_SECS:g}s: {e}", start)
                    return
                except urllib.error.URLError as e:
                    if isinstance(e.reason, (TimeoutError, socket.timeout)):
                        self.reply(504, f"OpenClaw execution timed out after {EXEC_TIMEOUT_SECS:g}s", start)
                        return
                    raise

                try:
                    parsed = json.loads(raw_body)
                    choices = parsed.get("choices") or []
                    content = choices[0].get("message", {}).get("content", "") if choices else ""
                    output = content or raw_body
                except Exception:
                    output = raw_body

                self.reply(200, output, start)
            except Exception as e:
                self.reply(500, str(e), start)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def reply(self, status, body, start):
        """Sends the /invoke response with duration header."""
        encoded_body = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded_body)))
        self.send_header("X-Openclaw-Exec-Ms", str(round((time.monotonic() - start) * 1000)))
        self.end_headers()
        self.wfile.write(encoded_body)


def cleanup(sig, frame):
    global gateway_proc
    if gateway_proc:
        try:
            os.killpg(gateway_proc.pid, signal.SIGTERM)
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    setup_openclaw_config()
    start_and_warmup_gateway()

    sys.stderr.write("Starting OpenClaw HTTP server on port 80...\n")
    sys.stderr.flush()

    server = HTTPServer(("0.0.0.0", 80), OpenClawHandler)
    sys.exit(server.serve_forever())
