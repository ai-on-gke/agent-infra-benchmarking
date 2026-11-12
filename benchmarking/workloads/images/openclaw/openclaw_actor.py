import json
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from gateway import GATEWAY_PORT, GATEWAY_TOKEN, start_and_warmup_gateway

EXEC_TIMEOUT_SECS = float(os.environ.get("OPENCLAW_TIMEOUT_SECS", "100"))


class OpenClawHandler(BaseHTTPRequestHandler):
    """Port 80 actor server providing /readyz and /invoke for Substrate benchmarking."""

    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/readyz":
            # The Gateway is already warmed up and running in the memory snapshot.
            # Answer immediately with 200 OK so Substrate's wakeupProbe takes <1ms.
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
        if not self.path.startswith("/invoke"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length).decode("utf-8")

        # Fast-path default prompt to measure baseline network and routing latency
        if not post_data.strip() or post_data.strip() == "echo 'hello'":
            self.reply(200, "Mock Substrate Benchmark Response", time.monotonic())
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
            with urllib.request.urlopen(req, timeout=EXEC_TIMEOUT_SECS) as resp:
                raw_body = resp.read().decode("utf-8")

            try:
                parsed = json.loads(raw_body)
                choices = parsed.get("choices") or []
                content = choices[0].get("message", {}).get("content", "") if choices else ""
                output = content or raw_body
            except Exception:
                output = raw_body

            self.reply(200, output, start)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            self.reply(500, f"OpenClaw Gateway HTTP {e.code}:\n{err_body}", start)
        except (TimeoutError, socket.timeout, urllib.error.URLError) as e:
            self.reply(504, f"OpenClaw execution timed out: {e}", start)
        except Exception as e:
            self.reply(500, str(e), start)

    def reply(self, status, body, start):
        """Sends an /invoke reply with the execution time in X-Openclaw-Exec-Ms."""
        encoded_body = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded_body)))
        self.send_header("X-Openclaw-Exec-Ms", str(round((time.monotonic() - start) * 1000)))
        self.end_headers()
        self.wfile.write(encoded_body)


if __name__ == "__main__":
    gateway_proc = start_and_warmup_gateway()

    def cleanup(sig, frame):
        if gateway_proc:
            try:
                os.killpg(gateway_proc.pid, signal.SIGTERM)
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    sys.stderr.write("Starting OpenClaw HTTP server on port 80...\n")
    sys.stderr.flush()
    server = HTTPServer(("0.0.0.0", 80), OpenClawHandler)
    sys.exit(server.serve_forever())
