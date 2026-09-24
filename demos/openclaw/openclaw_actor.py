import sys
import subprocess
import threading
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

# ==========================================
# 1. MOCK LLM SERVER (Runs on port 8080)
# ==========================================
class MockLLMHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/tags":
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            models_response = {
                "models": [
                    {
                        "name": "mock-model:latest",
                        "model": "mock-model:latest"
                    }
                ]
            }
            self.wfile.write(json.dumps(models_response).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        try:
            req_data = json.loads(body.decode('utf-8'))
            print("--- INCOMING REQUEST ---", flush=True)
            print(json.dumps(req_data, indent=2), flush=True)
            print("------------------------", flush=True)
        except Exception as e:
            print(f"Failed to parse body: {e}", flush=True)

        if self.path in ["/v1/chat/completions", "/v1/responses"]:
            # Parse the incoming request body to determine the conversation state
            try:
                req_data = json.loads(body.decode('utf-8'))
                messages = req_data.get("messages", [])

                # Count how many times the assistant has previously responded in THIS specific agent run.
                # OpenClaw sends the full conversation history.
                assistant_message_count = sum(1 for m in messages if m.get("role") == "assistant")
            except Exception:
                assistant_message_count = 0

            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()

            if assistant_message_count == 0:
                # Turn 1: Create buggy app
                cmd = 'cat << \'EOF\' > /tmp/app.py\ndef slugify(text):\n    return text.replace(" ", "-")\nEOF'
                tool_call = {
                    "id": "call_create_app",
                    "type": "function",
                    "function": {
                        "name": "exec",
                        "arguments": json.dumps({"command": cmd, "title": "Create target application file"})
                    }
                }
                self._send_tool_call_sse(tool_call, "turn1")
            elif assistant_message_count == 1:
                # Turn 2: Create failing test
                cmd = 'cat << \'EOF\' > /tmp/test_app.py\nfrom app import slugify\n\ndef test_slugify():\n    assert slugify("hello world") == "hello-world"\n    assert slugify("HELLO World") == "hello-world", "Failed: Should be lowercase"\n    print("Tests passed!")\n\nif __name__ == "__main__":\n    test_slugify()\nEOF'
                tool_call = {
                    "id": "call_create_test",
                    "type": "function",
                    "function": {
                        "name": "exec",
                        "arguments": json.dumps({"command": cmd, "title": "Write reproduction test"})
                    }
                }
                self._send_tool_call_sse(tool_call, "turn2")
            elif assistant_message_count == 2:
                # Turn 3: Run the failing test
                tool_call = {
                    "id": "call_run_test_fail",
                    "type": "function",
                    "function": {
                        "name": "exec",
                        "arguments": json.dumps({"command": "python3 /tmp/test_app.py", "title": "Run reproduction script"})
                    }
                }
                self._send_tool_call_sse(tool_call, "turn3")
            elif assistant_message_count == 3:
                # Turn 4: Patch the buggy app using sed
                tool_call = {
                    "id": "call_patch_app",
                    "type": "function",
                    "function": {
                        "name": "exec",
                        "arguments": json.dumps({"command": "sed -i 's/text.replace/text.lower().replace/g' /tmp/app.py", "title": "Patch app.py to fix casing"})
                    }
                }
                self._send_tool_call_sse(tool_call, "turn4")
            elif assistant_message_count == 4:
                # Turn 5: Run the test again (now it passes)
                tool_call = {
                    "id": "call_run_test_pass",
                    "type": "function",
                    "function": {
                        "name": "exec",
                        "arguments": json.dumps({"command": "python3 /tmp/test_app.py", "title": "Verify fix with reproduction script"})
                    }
                }
                self._send_tool_call_sse(tool_call, "turn5")
            else:
                # Final turn: text response
                self._send_text_sse("I have successfully executed the bash commands!", "turn6")

            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_response(404)
            self.end_headers()

    def _send_tool_call_sse(self, tool_call, turn_id):
        event = {
            "id": f"mock-{turn_id}",
            "object": "chat.completion.chunk",
            "created": 123456789,
            "model": "mock-model",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": tool_call["id"],
                        "type": "function",
                        "function": tool_call["function"]
                    }]
                },
                "finish_reason": None
            }]
        }
        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode('utf-8'))

        event_done = {
            "id": f"mock-{turn_id}",
            "object": "chat.completion.chunk",
            "created": 123456789,
            "model": "mock-model",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "tool_calls"
            }]
        }
        self.wfile.write(f"data: {json.dumps(event_done)}\n\n".encode('utf-8'))

    def _send_text_sse(self, text, turn_id):
        event = {
            "id": f"mock-{turn_id}",
            "object": "chat.completion.chunk",
            "created": 123456789,
            "model": "mock-model",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": text
                },
                "finish_reason": None
            }]
        }
        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode('utf-8'))

        event_done = {
            "id": f"mock-{turn_id}",
            "object": "chat.completion.chunk",
            "created": 123456789,
            "model": "mock-model",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop"
            }]
        }
        self.wfile.write(f"data: {json.dumps(event_done)}\n\n".encode('utf-8'))

def run_mock_llm():
    print("Starting internal Mock LLM Server on 127.0.0.1:8080...")
    server = HTTPServer(("127.0.0.1", 8080), MockLLMHandler)
    server.serve_forever()

# ==========================================
# 2. MAIN ACTOR SERVER (Runs on port 80)
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
                    config = {
                        "models": {
                            "providers": {
                                "mock-provider": {
                                    "api": "openai-completions",
                                    "baseUrl": "http://127.0.0.1:8080/v1",
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
    use_mock = os.environ.get("OPENCLAW_USE_MOCK_LLM", "").lower() in ["true", "1", "yes"]
    if use_mock:
        mock_thread = threading.Thread(target=run_mock_llm, daemon=True)
        mock_thread.start()

    print("Starting OpenClaw HTTP wrapper on port 80...")
    server = HTTPServer(("0.0.0.0", 80), OpenClawHandler)
    sys.exit(server.serve_forever())
