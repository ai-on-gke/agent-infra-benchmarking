import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

GATEWAY_PORT = int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18789"))
WARMUP_PORT = 18080
GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "benchmark-token")

BASE_DIR = "/tmp/openclaw_home"
CONFIG_PATH = os.path.join(BASE_DIR, "openclaw.json")


class _LoopbackWarmupHandler(BaseHTTPRequestHandler):
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


def _setup_openclaw_config():
    """Generates the openclaw.json configuration in /tmp before launching the daemon."""
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


def start_and_warmup_gateway() -> subprocess.Popen:
    """Launches OpenClaw Gateway as a background daemon and warms it up against a
    temporary loopback provider so V8 JIT code, HTTP endpoint, and SQLite databases
    are already initialized when the golden snapshot is taken.

    Returns the running Gateway subprocess.Popen instance.
    """
    _setup_openclaw_config()

    # 1. Start temporary loopback warmup server on port 18080
    warmup_server = HTTPServer(("127.0.0.1", WARMUP_PORT), _LoopbackWarmupHandler)
    threading.Thread(target=warmup_server.serve_forever, daemon=True).start()

    # 2. Launch OpenClaw Gateway daemon
    sys.stderr.write(f"Starting OpenClaw Gateway daemon on 127.0.0.1:{GATEWAY_PORT}...\n")
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

    # 3. Wait for Gateway /readyz endpoint to report ready
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

    # 4. Trigger one warmup turn through the Gateway
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

    # Clean up temporary warmup responder
    warmup_server.shutdown()

    # Remove workspace BOOTSTRAP.md if generated during first initialization
    bootstrap_md = os.path.join(BASE_DIR, "workspace", "BOOTSTRAP.md")
    if os.path.exists(bootstrap_md):
        try:
            os.remove(bootstrap_md)
        except OSError:
            pass

    sys.stderr.write(f"OpenClaw Gateway fully warmed after {time.monotonic() - t0:.1f}s!\n")
    sys.stderr.flush()

    return gateway_proc
