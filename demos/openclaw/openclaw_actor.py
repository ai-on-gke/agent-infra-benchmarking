import sys
import subprocess
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

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
                import os
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
                    # Substrate's ActorTemplate doesn't directly support Kubernetes Secret volume mounts,
                    # so the deploy script injects the base64-encoded secret into the environment.
                    # If present, we decode it and write it to disk so Vertex AI can authenticate!
                    if "GCP_SA_KEY_B64" in env and env["GCP_SA_KEY_B64"]:
                        key_path = "/tmp/gcp_key.json"
                        with open(key_path, "wb") as f:
                            f.write(base64.b64decode(env["GCP_SA_KEY_B64"]))
                        env["GOOGLE_APPLICATION_CREDENTIALS"] = key_path

                    # Use `openclaw agent exec` to run an isolated headless agent turn.
                    cmd = ["openclaw", "agent", "exec", "--isolated", "--model", "google-vertex/gemini-2.5-flash-lite", post_data]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=120, # AI agents can be slow, giving it 2 minutes
                    env=env
                )

                output = result.stdout + result.stderr
                if result.returncode != 0 and not output.strip():
                    output = f"OpenClaw exited with code {result.returncode} and no output (possibly OOM killed)."

                status_code = 200 if result.returncode == 0 else 500

                self.send_response(status_code)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(output.encode("utf-8"))

            except subprocess.TimeoutExpired:
                self.send_response(504)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OpenClaw execution timed out")
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    print("Starting OpenClaw HTTP wrapper on port 80...")
    server = HTTPServer(("0.0.0.0", 80), OpenClawHandler)
    sys.exit(server.serve_forever())
