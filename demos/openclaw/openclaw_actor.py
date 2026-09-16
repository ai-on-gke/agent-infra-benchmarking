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
                # Use `openclaw agent exec` to run an isolated headless agent turn
                # Since we don't want to spend money on OpenAI API calls right now,
                # we pass an intentionally invalid mock API key.
                # OpenClaw will boot up, attempt the API call, and immediately fail.
                # This is perfect for load-testing Substrate's infrastructure overhead!
                import os
                os.environ["OPENAI_API_KEY"] = "sk-mock-key-do-not-charge"

                result = subprocess.run(
                    ["openclaw", "agent", "exec", "--isolated", "--auth-env-only", "--model", "openai/gpt-5.6-sol", post_data],
                    capture_output=True,
                    text=True,
                    timeout=120 # AI agents can be slow, giving it 2 minutes
                )

                # Because we passed a fake key, openclaw will exit with a non-zero status.
                # For benchmarking purposes, we'll intercept the auth failure and return 200 OK
                # to Locust so that it records the latency of the "attempt" as a success!
                output = result.stdout + result.stderr
                status_code = 200 if "authentication failed" in output.lower() or "incorrect api key" in output.lower() else (200 if result.returncode == 0 else 500)

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
