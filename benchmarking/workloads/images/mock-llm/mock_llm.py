import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The nonce the benchmark worker puts in its prompt, "(request <uuid>)", which
# the final answer repeats so the worker can tell this turn's answer from any
# other (locust/go/internal/runworkload/openclaw.go).
REQUEST_NONCE = re.compile(r"\(request ([0-9a-f-]{36})\)")


def request_nonce(messages):
    """The nonce in the last user message that has one, or None."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
        match = REQUEST_NONCE.search(content or "")
        if match:
            return match[1]
    return None


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
        elif self.path == "/readyz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        if self.path in ["/v1/chat/completions", "/v1/responses"]:
            try:
                req_data = json.loads(body.decode('utf-8'))
                messages = req_data.get("messages", [])
                assistant_message_count = sum(1 for m in messages if m.get("role") == "assistant")
                nonce = request_nonce(messages)
            except Exception:
                assistant_message_count = 0
                nonce = None

            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()

            if assistant_message_count == 0:
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
                answer = "I have successfully executed the bash commands!"
                if nonce:
                    answer += f" (request {nonce})"
                self._send_text_sse(answer, "turn6")

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

if __name__ == "__main__":
    print("Starting Mock LLM Server on 0.0.0.0:8080...")
    server = ThreadingHTTPServer(("0.0.0.0", 8080), MockLLMHandler)
    server.serve_forever()
