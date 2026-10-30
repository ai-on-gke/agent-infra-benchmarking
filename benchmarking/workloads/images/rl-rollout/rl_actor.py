"""Substrate Actor Server for Reinforcement Learning Rollout Execution.

This server runs inside an Agent Substrate Actor (isolated via gVisor or microVM)
and provides execution sandbox capabilities (code execution, tool execution,
and multi-turn environment state) for an external RL training pipeline.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

try:
    from .train_rl import ActorRLTrainer
except ImportError:
    from train_rl import ActorRLTrainer

DEFAULT_PORT = int(os.environ.get("PORT", "80"))
EXEC_TIMEOUT_SECS = float(os.environ.get("RL_EXEC_TIMEOUT_SECS", "30"))
REQUEST_NONCE_RE = re.compile(r"\(request ([0-9a-f-]{36})\)")


class RLStatefulSandbox:
    """In-memory state and tool execution environment inside the actor."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> dict[str, Any]:
        self.variables: dict[str, Any] = {}
        self.step_count: int = 0
        self.history: list[dict[str, Any]] = []
        return {"status": "reset", "step": 0}

    def execute_python(self, code: str, timeout: float = EXEC_TIMEOUT_SECS) -> dict[str, Any]:
        """Executes python code safely in an isolated subprocess."""
        start = time.monotonic()
        try:
            proc = subprocess.run(
                ["python3", "-c", code],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            elapsed_ms = (time.monotonic() - start) * 1000.0
            self.step_count += 1
            result = {
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
                "exit_code": proc.returncode,
                "elapsed_ms": round(elapsed_ms, 2),
                "success": proc.returncode == 0,
            }
            self.history.append({"tool": "python", "input": code, "result": result})
            return result
        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": f"Execution timed out after {timeout}s",
                "exit_code": 124,
                "elapsed_ms": round((time.monotonic() - start) * 1000.0, 2),
                "success": False,
            }
        except Exception as exc:
            return {
                "stdout": "",
                "stderr": str(exc),
                "exit_code": 1,
                "elapsed_ms": round((time.monotonic() - start) * 1000.0, 2),
                "success": False,
            }

    def execute_bash(self, cmd: str, timeout: float = EXEC_TIMEOUT_SECS) -> dict[str, Any]:
        """Executes a bash / shell command in an isolated subprocess."""
        start = time.monotonic()
        try:
            proc = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            elapsed_ms = (time.monotonic() - start) * 1000.0
            self.step_count += 1
            result = {
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
                "exit_code": proc.returncode,
                "elapsed_ms": round(elapsed_ms, 2),
                "success": proc.returncode == 0,
            }
            self.history.append({"tool": "bash", "input": cmd, "result": result})
            return result
        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": f"Execution timed out after {timeout}s",
                "exit_code": 124,
                "elapsed_ms": round((time.monotonic() - start) * 1000.0, 2),
                "success": False,
            }

    def search_mock(self, query: str) -> dict[str, Any]:
        """Deterministic search tool mock returning indexed knowledge chunks."""
        start = time.monotonic()
        self.step_count += 1
        query_lower = query.lower()
        database = {
            "fibonacci": "The Fibonacci sequence F(n) is defined by F(0)=0, F(1)=1, and F(n)=F(n-1)+F(n-2).",
            "prime": "A prime number is a natural number greater than 1 with no positive divisors other than 1 and itself.",
            "gravity": "Standard acceleration due to Earth's gravity is 9.80665 m/s^2.",
            "substrate": "Agent Substrate is a high-density Kubernetes runtime for autonomous agent sandboxes.",
            "kimi": "Kimi-Researcher uses end-to-end RL and REINFORCE with outcome rewards for search and reasoning.",
        }
        matches = [val for key, val in database.items() if key in query_lower]
        if not matches:
            # Fallback keyword match
            matches = [val for key, val in database.items() if any(word in key for word in query_lower.split())]

        elapsed_ms = (time.monotonic() - start) * 1000.0
        output_text = " ".join(matches) if matches else "No relevant records found."
        result = {
            "stdout": output_text,
            "stderr": "",
            "exit_code": 0,
            "elapsed_ms": round(elapsed_ms, 2),
            "success": True,
        }
        self.history.append({"tool": "search", "input": query, "result": result})
        return result

    def execute_step(self, tool: str, action: str) -> dict[str, Any]:
        """Unified step execution method for tool actions."""
        start = time.monotonic()
        if tool == "python":
            res = self.execute_python(action)
        elif tool in ("bash", "sh"):
            res = self.execute_bash(action)
        elif tool == "search":
            res = self.search_mock(action)
        else:
            res = {"error": f"Unknown tool: {tool}", "success": False, "exit_code": 1}
        res["rtt_ms"] = (time.monotonic() - start) * 1000.0
        return res


SANDBOX = RLStatefulSandbox()
TRAINER = ActorRLTrainer(seed=42, lr=0.03)


class RLHandler(BaseHTTPRequestHandler):
    """HTTP handler implementing readiness probes, RL step execution, and Locust /invoke."""

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str, extra_headers: dict[str, str] | None = None) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/readyz", "/healthz"):
            self._send_text(200, "OK")
        elif self.path == "/history":
            self._send_json(200, {"history": SANDBOX.history, "step_count": SANDBOX.step_count})
        elif self.path == "/training_status":
            self._send_json(200, {
                "iteration": TRAINER.iteration,
                "history": TRAINER.history,
            })
        else:
            self._send_text(404, "Not Found")

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length).decode("utf-8")

        if self.path == "/reset":
            res = SANDBOX.reset()
            self._send_json(200, res)
            return

        if self.path == "/step" or self.path == "/execute":
            try:
                payload = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON body"})
                return

            tool = payload.get("tool", "python")
            cmd = payload.get("action") or payload.get("command") or payload.get("code") or ""
            timeout = float(payload.get("timeout", EXEC_TIMEOUT_SECS))

            if tool == "python":
                res = SANDBOX.execute_python(cmd, timeout=timeout)
            elif tool in ("bash", "sh"):
                res = SANDBOX.execute_bash(cmd, timeout=timeout)
            elif tool == "search":
                res = SANDBOX.search_mock(cmd)
            else:
                res = {"error": f"Unknown tool: {tool}", "success": False, "exit_code": 1}

            self._send_json(200, res)
            return

        if self.path.startswith("/invoke"):
            # Driven by Locust or benchmark harness: executes real on-policy RL training iteration
            start = time.monotonic()
            match = REQUEST_NONCE_RE.search(raw_body)
            nonce = match.group(1) if match else "test-nonce"

            # Execute real on-policy RL training iteration and update neural weights
            record = TRAINER.train_step(SANDBOX, batch_size=2)
            elapsed_ms = round((time.monotonic() - start) * 1000.0)

            reply = (
                f"RL training step {record['iteration']} completed: "
                f"loss={record['loss']:.4f}, reward={record['mean_reward']:.3f}, accuracy={record['accuracy']}% "
                f"(request {nonce})"
            )
            headers = {
                "X-RL-Step": str(record["iteration"]),
                "X-RL-Loss": str(record["loss"]),
                "X-RL-Reward": str(record["mean_reward"]),
                "X-RL-Accuracy": str(record["accuracy"]),
                "X-RL-Exec-Ms": str(elapsed_ms),
            }
            self._send_text(200, reply, headers)
            return

        self._send_text(404, "Not Found")


def run_server(port: int = DEFAULT_PORT) -> None:
    server_address = ("0.0.0.0", port)
    httpd = ThreadingHTTPServer(server_address, RLHandler)
    print(f"RL Rollout Actor Server listening on port {port}...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run_server()
