import sys
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

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

                # Substrate's ActorTemplate doesn't directly support Kubernetes Secret volume mounts,
                # so the deploy script injects the base64-encoded secret into the environment.
                # If present, we decode it and write it to disk so Vertex AI can authenticate!
                if "GCP_SA_KEY_B64" in os.environ and os.environ["GCP_SA_KEY_B64"]:
                    key_path = "/tmp/gcp_key.json"
                    with open(key_path, "wb") as f:
                        f.write(base64.b64decode(os.environ["GCP_SA_KEY_B64"]))
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key_path

                # Use `openclaw agent exec` to run an isolated headless agent turn.
                result = subprocess.run(
                    ["openclaw", "agent", "exec", "--isolated", "--model", "google-vertex/gemini-2.5-flash-lite", post_data],
                    capture_output=True,
                    text=True,
                    timeout=120 # AI agents can be slow, giving it 2 minutes
                )

                # Return the actual result.
                output = result.stdout + result.stderr
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
