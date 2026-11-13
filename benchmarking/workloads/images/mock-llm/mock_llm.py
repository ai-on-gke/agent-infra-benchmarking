import json
import re
import uuid
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


# The claude-code workload's repo (workloads/images/claude-code/textkit), where
# each turn fixes the first failing test: the file, and the line to replace.
TEXTKIT_FIXES = {
    "tests/test_case.py::test_camel_to_snake": (
        "textkit/case.py",
        'return re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()',
        'return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()',
    ),
    "tests/test_counts.py::test_word_counts_ignore_case": (
        "textkit/counts.py",
        "for word in text.split():",
        "for word in text.lower().split():",
    ),
    "tests/test_slug.py::test_slugify_lowercases": (
        "textkit/slug.py",
        'return "-".join(words)',
        'return "-".join(words).lower()',
    ),
    "tests/test_stats.py::test_mean": (
        "textkit/stats.py",
        "return sum(values) / (len(values) - 1)",
        "return sum(values) / len(values)",
    ),
    "tests/test_wrap.py::test_truncate_fits_width": (
        "textkit/wrap.py",
        "return text[:width] + suffix",
        "return text[: width - len(suffix)] + suffix",
    ),
}
FAILED_TEST = re.compile(r"^FAILED (\S+::\w+)", re.MULTILINE)
WORKING_DIRECTORY = re.compile(r"working directory: (\S+)", re.IGNORECASE)


def block_text(content):
    """The text of a message's content, or of a tool result's."""
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if part.get("type") == "text":
            parts.append(part.get("text", ""))
        elif part.get("type") == "tool_result":
            parts.append(block_text(part.get("content")))
    return "\n".join(parts)


def is_prompt(message):
    """A user message carrying a turn's prompt, rather than tool results."""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    text = content if isinstance(content, str) else "\n".join(
        part.get("text", "") for part in content or [] if part.get("type") == "text"
    )
    return bool(REQUEST_NONCE.search(text))


def claude_code_step(req):
    """The next content block of a Claude Code turn: run the tests, read the
    file under the first failing one, fix it, run that test, commit, then
    answer with the turn's number in the session and its nonce. The turn's
    number counts the prompts in the conversation, so it says whether the
    session survived since its first turn."""
    messages = req.get("messages", [])
    prompts = [i for i, m in enumerate(messages) if is_prompt(m)]
    if not prompts:
        return {"type": "text", "text": "No request nonce in the prompt."}
    turn, start = len(prompts), prompts[-1]
    nonce = REQUEST_NONCE.search(block_text(messages[start]["content"]))[1]
    # Claude Code merges consecutive answers into one assistant message, so
    # the step is the number of tool calls since the prompt.
    blocks = [
        block
        for m in messages[start + 1:]
        if isinstance(m.get("content"), list)
        for block in m["content"]
    ]
    calls = [block["id"] for block in blocks if block.get("type") == "tool_use"]
    step = len(calls)

    def tool(name, **tool_input):
        return {"type": "tool_use", "id": f"toolu_{nonce[:8]}_{step}", "name": name, "input": tool_input}

    def answer(text):
        return {"type": "text", "text": f"{text} (turn {turn}) (request {nonce})"}

    if step == 0:
        return tool("Bash", command="python3 -m pytest -x -q", description="Run the tests")
    first_result = next(
        (block_text(b.get("content")) for b in blocks if b.get("type") == "tool_result" and b.get("tool_use_id") == calls[0]),
        "",
    )
    failed = FAILED_TEST.search(first_result)
    if not failed or failed[1] not in TEXTKIT_FIXES:
        return answer("No failing test I know how to fix.")
    test = failed[1]
    path, old, new = TEXTKIT_FIXES[test]
    system = block_text(req.get("system"))
    cwd = WORKING_DIRECTORY.search(system)
    file_path = f"{cwd[1] if cwd else '/workspace/textkit'}/{path}"
    if step == 1:
        return tool("Read", file_path=file_path)
    if step == 2:
        return tool("Edit", file_path=file_path, old_string=old, new_string=new)
    if step == 3:
        return tool("Bash", command=f"python3 -m pytest -q {test}", description="Run the fixed test")
    if step == 4:
        return tool("Bash", command=f'git commit -qam "Fix {test}"', description="Commit the fix")
    return answer(f"Fixed {test} and committed it.")


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

        path = self.path.split("?", 1)[0]
        if path == "/v1/messages/count_tokens":
            self._send_json({"input_tokens": 100})
            return
        if path == "/v1/messages":
            self._answer_anthropic(body)
            return

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

    def _send_json(self, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _answer_anthropic(self, body):
        req = json.loads(body.decode("utf-8"))
        # Claude Code's side requests, such as a title for the session, offer
        # no tools; only the agent loop's do.
        block = claude_code_step(req) if req.get("tools") else {"type": "text", "text": "OK"}
        stop_reason = "tool_use" if block["type"] == "tool_use" else "end_turn"
        message = {
            # Claude Code merges the assistant messages that share an id.
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": req.get("model", "mock-model"),
            "content": [block],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        if not req.get("stream"):
            self._send_json(message)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if block["type"] == "tool_use":
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        else:
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        events = [
            ("message_start", {"message": {**message, "content": [], "stop_reason": None}}),
            ("content_block_start", {"index": 0, "content_block": start}),
            ("content_block_delta", {"index": 0, "delta": delta}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": stop_reason, "stop_sequence": None},
                               "usage": {"output_tokens": 10}}),
            ("message_stop", {}),
        ]
        for name, data in events:
            self.wfile.write(f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n".encode("utf-8"))

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
