"""mock-llm's record and replay: an agent's calls, on the Anthropic Messages
API (Claude Code), the OpenAI Chat Completions API (openclaw) or the OpenAI
Responses API, forwarded to the real API and their answers saved to a
cassette, or answered from one. Every call is recorded: the agent loop's,
those that offer no tools (a title, a summary of the conversation), token
counts, and the errors the API answered with.

A call is looked up by the parts of its conversation that stay the same from
run to run, and its answer is saved with "{{nonce}}" for the worker's nonce,
which replay fills in with the current one. The answer to a turn's last call
gets the ending the workload's worker checks for (answer_suffix)."""

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

ANTHROPIC, OPENAI, RESPONSES = "anthropic", "openai", "openai-responses"
COUNT_TOKENS = "/v1/messages/count_tokens"
# The API each recorded path talks.
RECORDED_PATHS = {
    "/v1/messages": ANTHROPIC,
    COUNT_TOKENS: ANTHROPIC,
    "/v1/chat/completions": OPENAI,
    "/v1/responses": RESPONSES,
}

ANTHROPIC_UPSTREAM = os.environ.get("ANTHROPIC_UPSTREAM") or "https://api.anthropic.com"
OPENAI_UPSTREAM = (os.environ.get("OPENAI_UPSTREAM") or "https://api.openai.com/v1").rstrip("/")
# The model OpenAI calls are recorded with, in place of the one the agent
# asks for, which is mock-llm's own (openclaw's "mock-model").
OPENAI_UPSTREAM_MODEL = os.environ.get("OPENAI_UPSTREAM_MODEL", "")
FORWARDED_ANTHROPIC_HEADERS = ("anthropic-version", "anthropic-beta", "content-type")
# The headers of an error that are saved with it, for the agent's retry.
ERROR_HEADERS = ("content-type", "retry-after", "x-should-retry")
# Errors that say the recording is set up wrong, not what the API answers an
# agent; they are passed on but not saved.
SETUP_ERRORS = (401, 403)

# The nonce the benchmark worker puts in its prompt, "(request <uuid>)", which
# the final answer repeats so the worker can tell this turn's answer from any
# other (locust/go/internal/workload).
REQUEST_NONCE = re.compile(r"\(request ([0-9a-f-]{36})\)")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# What agents add to a prompt that differs from run to run: Claude Code's
# system reminders and billing line, and openclaw's timestamp before it and
# runtime line after it, with the host's name.
VOLATILE_TEXT = [
    re.compile(r"^x-anthropic-billing-header:.*$", re.MULTILINE),
    re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL),
    re.compile(r"^\[\w{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})? [^\]]*\] ", re.MULTILINE),
    re.compile(r"^Runtime: .*$", re.MULTILINE),
]


def stable_text(text):
    for pattern in VOLATILE_TEXT:
        text = pattern.sub("", text)
    return text.strip()

# What the openclaw worker wants the turn's answer to end with, before the
# nonce (locust/go/internal/workload/openclaw.go).
OPENCLAW_DONE = "I have successfully executed the bash commands!"


# A text part's type: the Responses API has its own for each direction.
TEXT_PARTS = ("text", "input_text", "output_text")


def content_text(content):
    """The text parts of a message's content, joined."""
    if isinstance(content, str):
        return content
    return "\n".join(
        part.get("text", "") for part in content or [] if isinstance(part, dict) and part.get("type") in TEXT_PARTS
    )


def call_messages(req, api):
    """The call's messages; the Responses API's are its input items."""
    if api != RESPONSES:
        return req.get("messages", [])
    items = req.get("input") or []
    return [{"role": "user", "content": items}] if isinstance(items, str) else items


def request_nonce(messages):
    """The nonce in the last user message that has one, or None."""
    for message in reversed(messages):
        if message.get("role") == "user":
            match = REQUEST_NONCE.search(content_text(message.get("content")))
            if match:
                return match[1]
    return None


def is_prompt(message):
    """A user message carrying a turn's prompt, rather than tool results."""
    return message.get("role") == "user" and bool(REQUEST_NONCE.search(content_text(message.get("content"))))


def answer_suffix(req, api, nonce):
    """What the turn's answer ends with for its worker: Claude Code's the
    turn's number in the session as well (claudecode.go), openclaw's
    OPENCLAW_DONE."""
    if api == ANTHROPIC:
        turn = sum(1 for m in req.get("messages", []) if is_prompt(m))
        return f" (turn {turn}) (request {nonce})"
    return f"\n\n{OPENCLAW_DONE} (request {nonce})"


def ends_turn(req, api, path):
    """Whether the call is one of the agent loop's, whose last answer gets
    answer_suffix. Claude Code's other calls offer no tools."""
    return path != COUNT_TOKENS and (api != ANTHROPIC or bool(req.get("tools")))


def conversation(req, api, path=None):
    """The parts of a call that are the same in every run of a recorded
    session. It leaves out metadata, tool results and VOLATILE_TEXT, which
    carry dates, hosts, paths and commit hashes, and masks UUIDs such as the
    nonce. The system prompt is left out too, except from Claude Code's calls
    that offer no tools, which it tells apart."""
    messages = []
    for message in call_messages(req, api):
        role, content, blocks = message.get("role"), message.get("content"), []
        if api == RESPONSES:
            kind = message.get("type", "message")
            if kind == "function_call":
                role = "assistant"
                blocks.append(["tool_use", message.get("call_id"), message.get("name"), message.get("arguments")])
            elif kind == "function_call_output":
                role = "tool"
                blocks.append(["tool_result", message.get("call_id")])
            elif kind != "message" or role in ("system", "developer"):
                continue
            else:
                text = stable_text(content_text(content))
                if text:
                    blocks.append(["text", text])
        elif api == OPENAI:
            if role in ("system", "developer"):
                continue
            if role == "tool":
                blocks.append(["tool_result", message.get("tool_call_id")])
            else:
                text = stable_text(content_text(content))
                if text:
                    blocks.append(["text", text])
                for call in message.get("tool_calls") or []:
                    function = call.get("function", {})
                    blocks.append(["tool_use", call.get("id"), function.get("name"), function.get("arguments")])
        else:
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for block in content or []:
                kind = block.get("type")
                if kind == "text":
                    text = stable_text(block.get("text", ""))
                    if text:
                        blocks.append(["text", text])
                elif kind == "tool_use":
                    blocks.append(["tool_use", block.get("id"), block.get("name"), block.get("input")])
                elif kind == "tool_result":
                    blocks.append(["tool_result", block.get("tool_use_id")])
        messages.append([role, blocks])
    # Anthropic's agent loop calls leave out the api, so cassettes recorded
    # before the other calls were still match.
    conv = [req.get("model"), messages] if api == ANTHROPIC else [api, req.get("model"), messages]
    if api == ANTHROPIC and not req.get("tools"):
        conv.append(["system", stable_text(content_text(req.get("system")))])
    if path == COUNT_TOKENS:
        conv.append("count_tokens")
    return json.loads(UUID.sub("<uuid>", json.dumps(conv)))


def conversation_key(conv):
    return hashlib.sha256(json.dumps(conv, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def replace_in(value, old, new):
    return json.loads(json.dumps(value).replace(old, new))


def output_texts(output):
    """The text parts of a Responses API answer's output items."""
    return [
        part
        for item in output or []
        if item.get("type") == "message"
        for part in item.get("content") or []
        if part.get("type") == "output_text"
    ]


def text_runs(entry, api):
    """The answer's texts, each as the (dict, key) pairs that hold its pieces
    in the order they are sent. A Responses API stream sends a text again in
    full once it is done; each of those is a text of its own here."""
    if "message" in entry:
        message = entry["message"]
        if api == ANTHROPIC:
            return [[(block, "text")] for block in message.get("content", []) if block.get("type") == "text"]
        if api == RESPONSES:
            return [[(part, "text")] for part in output_texts(message.get("output"))]
        return [
            [(choice["message"], "content")]
            for choice in message.get("choices", [])
            if isinstance(choice.get("message", {}).get("content"), str)
        ]
    runs, whole = {}, []
    for _, name, data in entry.get("events", []):
        if not isinstance(data, dict):
            continue
        if api == ANTHROPIC:
            if name == "content_block_delta" and data["delta"].get("type") == "text_delta":
                runs.setdefault(data["index"], []).append((data["delta"], "text"))
        elif api == RESPONSES:
            if name == "response.output_text.delta":
                runs.setdefault((data.get("output_index"), data.get("content_index")), []).append((data, "delta"))
            elif name == "response.output_text.done":
                whole.append([(data, "text")])
            elif name == "response.content_part.done":
                whole += [[(part, "text")] for part in output_texts([{"type": "message", "content": [data["part"]]}])]
            elif name == "response.output_item.done":
                whole += [[(part, "text")] for part in output_texts([data["item"]])]
            elif isinstance(data.get("response"), dict):
                whole += [[(part, "text")] for part in output_texts(data["response"].get("output"))]
        else:
            for choice in data.get("choices", []):
                if isinstance(choice.get("delta", {}).get("content"), str):
                    runs.setdefault(choice.get("index", 0), []).append((choice["delta"], "content"))
    return list(runs.values()) + whole


def mask_nonces(entry, nonce, api):
    """The entry with "{{nonce}}" for the nonces in its answer's text: the
    turn's own, and any "(request <uuid>)" the model copied from an earlier
    answer, also where the stream splits one over several pieces."""
    entry = json.loads(json.dumps(entry))
    for parts in text_runs(entry, api):
        text = "".join(holder[key] for holder, key in parts)
        spans = [m.span(1) for m in REQUEST_NONCE.finditer(text)]
        if nonce:
            spans += [m.span() for m in re.finditer(re.escape(nonce), text)]
        chars = list(text)
        for start, end in spans:
            chars[start:end] = ["{{nonce}}"] + [""] * (end - start - 1)
        pos = 0
        for holder, key in parts:
            holder[key], pos = "".join(chars[pos:pos + len(holder[key])]), pos + len(holder[key])
    return entry


def with_suffix(entry, suffix, api):
    """The entry with suffix added to its answer's final text, unless the
    answer calls tools, so the turn goes on."""
    entry = json.loads(json.dumps(entry))
    if "message" in entry:
        message = entry["message"]
        if api == RESPONSES:
            texts = output_texts(message.get("output"))
            if texts and not calls_functions(message.get("output")):
                texts[-1]["text"] += suffix
        elif api == ANTHROPIC:
            if message.get("stop_reason") == "tool_use":
                return entry
            texts = [block for block in message["content"] if block.get("type") == "text"]
            if texts:
                texts[-1]["text"] += suffix
            else:
                message["content"].append({"type": "text", "text": suffix.lstrip()})
        else:
            choice = (message.get("choices") or [{}])[0]
            if choice.get("finish_reason") == "tool_calls" or "message" not in choice:
                return entry
            choice["message"]["content"] = (choice["message"].get("content") or "") + suffix
        return entry
    events = entry["events"]
    if api == ANTHROPIC:
        entry["events"] = anthropic_events_with_suffix(events, suffix)
        return entry
    if api == RESPONSES:
        entry["events"] = responses_events_with_suffix(events, suffix)
        return entry
    finish = next(
        (
            (i, choice["finish_reason"])
            for i, (_, _, data) in enumerate(events)
            if isinstance(data, dict)
            for choice in data.get("choices", [])
            if choice.get("finish_reason")
        ),
        None,
    )
    if finish and finish[1] == "tool_calls":
        return entry
    at = finish[0] if finish else next((i for i, (_, _, data) in enumerate(events) if data == "[DONE]"), len(events))
    base = events[at][2] if at < len(events) and isinstance(events[at][2], dict) else {}
    chunk = {key: base[key] for key in ("id", "object", "created", "model") if key in base}
    chunk["choices"] = [{"index": 0, "delta": {"content": suffix}, "finish_reason": None}]
    ms = events[at][0] if at < len(events) else (events[-1][0] if events else 0)
    entry["events"] = events[:at] + [[ms, None, chunk]] + events[at:]
    return entry


def anthropic_events_with_suffix(events, suffix):
    stop = next((d["delta"].get("stop_reason") for _, name, d in events if name == "message_delta"), None)
    if stop == "tool_use":
        return events
    starts = {d["index"]: d["content_block"]["type"] for _, name, d in events if name == "content_block_start"}
    texts = [index for index, kind in starts.items() if kind == "text"]
    if texts:
        index = max(texts)
        at = next(i for i, (_, name, d) in enumerate(events) if name == "content_block_stop" and d["index"] == index)
        added = [("content_block_delta", {"index": index, "delta": {"type": "text_delta", "text": suffix}})]
    else:
        index = max(starts, default=-1) + 1
        at = next(i for i, (_, name, _) in enumerate(events) if name == "message_delta")
        added = [
            ("content_block_start", {"index": index, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": index, "delta": {"type": "text_delta", "text": suffix.lstrip()}}),
            ("content_block_stop", {"index": index}),
        ]
    ms = events[at][0]
    return events[:at] + [[ms, name, {"type": name, **data}] for name, data in added] + events[at:]


def calls_functions(output):
    return any(item.get("type") == "function_call" for item in output or [])


def responses_events_with_suffix(events, suffix):
    """The Responses API stream with suffix added to its last text: as one
    more delta, and in each event that sends the text again in full."""
    if calls_functions([d["item"] for _, name, d in events if name == "response.output_item.done"]):
        return events
    deltas = [i for i, (_, name, _) in enumerate(events) if name == "response.output_text.delta"]
    if not deltas:
        return events
    at = deltas[-1] + 1
    ms, name, last = events[at - 1]
    output_index, content_index = last.get("output_index"), last.get("content_index")
    for _, name, data in events[at:]:
        if not isinstance(data, dict):
            continue
        if "sequence_number" in data:
            data["sequence_number"] += 1
        if name in ("response.output_text.done", "response.content_part.done"):
            if (data.get("output_index"), data.get("content_index")) != (output_index, content_index):
                continue
            texts = [data] if name == "response.output_text.done" else [data["part"]]
        elif name == "response.output_item.done" and data.get("output_index") == output_index:
            texts = data["item"].get("content", [])[content_index:content_index + 1]
        elif isinstance(data.get("response"), dict):
            item = (data["response"].get("output") or [])[output_index:output_index + 1]
            texts = item[0].get("content", [])[content_index:content_index + 1] if item else []
        else:
            continue
        for part in texts:
            if isinstance(part.get("text"), str):
                part["text"] += suffix
    added = {**last, "delta": suffix}
    if "sequence_number" in added:
        added["sequence_number"] += 1
    return events[:at] + [[ms, "response.output_text.delta", added]] + events[at:]


def failed(entry):
    """Whether a streamed answer ended in an error."""
    return any(
        name in ("error", "response.failed") or (isinstance(data, dict) and "error" in data)
        for _, name, data in entry.get("events", [])
    )


def is_error(entry):
    """Whether the answer is an error, which the agent's next try of the call
    is recorded after."""
    return "error" in entry or failed(entry)


class UpstreamError(Exception):
    def __init__(self, status, content_type, body):
        super().__init__(f"upstream answered {status}")
        self.status, self.content_type, self.body = status, content_type, body


def error_body(api, kind, text):
    if api == ANTHROPIC:
        return json.dumps({"type": "error", "error": {"type": kind, "message": text}}).encode("utf-8")
    return json.dumps({"error": {"type": kind, "message": text}}).encode("utf-8")


def forward(api, path, headers, req, body):
    """The real API's answer to the call: the JSON message, the SSE events of
    a streamed one with their offset in ms, or the error it answered with.
    Raises UpstreamError with what to pass on to the agent when there is none
    to save."""
    if api == ANTHROPIC:
        key_name, url = "ANTHROPIC_API_KEY", ANTHROPIC_UPSTREAM + path
    else:
        key_name, url = "OPENAI_API_KEY", OPENAI_UPSTREAM + path.removeprefix("/v1")
    key = os.environ.get(key_name)
    if not key or (api == OPENAI and not OPENAI_UPSTREAM_MODEL):
        missing = key_name if not key else "OPENAI_UPSTREAM_MODEL"
        raise UpstreamError(500, "application/json", error_body(api, "api_error", f"mock-llm records with {missing}, which is not set"))
    request = urllib.request.Request(url, method="POST")
    if api == ANTHROPIC:
        request.data = body
        for name in FORWARDED_ANTHROPIC_HEADERS:
            if headers.get(name):
                request.add_header(name, headers[name])
        request.add_header("x-api-key", key)
    else:
        request.data = json.dumps({**req, "model": OPENAI_UPSTREAM_MODEL}).encode("utf-8")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {key}")
    start = time.monotonic()
    try:
        resp = urllib.request.urlopen(request, timeout=600)
    except urllib.error.HTTPError as e:
        if e.code in SETUP_ERRORS:
            raise UpstreamError(e.code, e.headers.get("Content-Type", "application/json"), e.read())
        return {
            "error": {
                "status": e.code,
                "headers": {name: e.headers[name] for name in ERROR_HEADERS if e.headers.get(name)},
                "body": e.read().decode("utf-8", "replace"),
            },
            "ms": round((time.monotonic() - start) * 1000),
        }
    except urllib.error.URLError as e:
        raise UpstreamError(502, "application/json", error_body(api, "api_error", f"mock-llm could not reach {url}: {e.reason}"))
    with resp:
        if not req.get("stream"):
            return {"message": json.load(resp), "ms": round((time.monotonic() - start) * 1000)}
        events, name = [], None
        for line in resp:
            line = line.decode("utf-8").rstrip("\r\n")
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
                events.append([round((time.monotonic() - start) * 1000), name, data if data == "[DONE]" else json.loads(data)])
            elif not line:
                name = None
    return {"events": events}


def sse(name, data):
    """One SSE event as sent: OpenAI's have no event name."""
    head = f"event: {name}\n" if name else ""
    return f"{head}data: {data if isinstance(data, str) else json.dumps(data)}\n\n".encode("utf-8")


class Cassette:
    """Recorded answers, one JSON line each, keyed by conversation_key. A call
    has several when the API answered it with errors first: they are in the
    order the agent got them."""

    def __init__(self, path, must_exist):
        self.path, self.lock, self.entries, self.asked = path, threading.Lock(), {}, {}
        if must_exist and not os.path.exists(path):
            raise SystemExit(f"No cassette at {path} to replay")
        if not must_exist:
            # Fails here rather than when the first answer is recorded.
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                open(path, "a").close()
            except OSError as e:
                raise SystemExit(f"Cannot write the cassette {path}: {e.strerror}")
        with open(path) as f:
            for line in f:
                if line.strip():
                    entry = json.loads(line)
                    # For cassettes recorded before mask_nonces, or the api.
                    entry = mask_nonces(entry, None, entry.get("api", ANTHROPIC))
                    self.entries.setdefault(entry["key"], []).append(entry)

    def __len__(self):
        return sum(len(answers) for answers in self.entries.values())

    def get(self, key):
        """The call's answers, in the order they were recorded."""
        return self.entries.get(key, [])

    def next(self, key, client):
        """The call's answer for the client's next try of it: each client gets
        the answers in order, and after the last the first again, as a new
        session of its would."""
        answers = self.entries[key]
        if len(answers) == 1:
            return answers[0]
        with self.lock:
            n = self.asked.get((client, key), 0)
            self.asked[client, key] = n + 1
        return answers[n % len(answers)]

    def add(self, entry):
        with self.lock:
            self.entries.setdefault(entry["key"], []).append(entry)
            with open(self.path, "a") as f:
                f.write(json.dumps(entry) + "\n")
