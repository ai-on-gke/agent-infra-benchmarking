#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SWE-perf Asynchronous Execution Engine & HTTP Server.

Exposes an asynchronous REST API for executing chunks of pre-recorded agent trajectories:
- POST /execute: Dispatches a bounded chunk of trace commands in a background daemon thread. Returns 202 Accepted.
- GET /status?job_id=<id>: Queries in-memory execution state ("RUNNING", "COMPLETED", "FAILED").
"""

import http.server
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional, Tuple


class ExecutionState:
  """Thread-safe in-memory state tracking for asynchronous execution jobs."""

  def __init__(self) -> None:
    self.lock = threading.Lock()
    self.jobs: Dict[str, Dict[str, Any]] = {}
    self.active_job_id: Optional[str] = None
    self.last_completed_step: int = 0
    self.total_steps: int = 0
    self.trace_commands: List[Any] = []

  def load_trace(self, trace_path: str) -> None:
    """Loads trajectory commands from a JSON trace file into memory."""
    if os.path.exists(trace_path):
      try:
        with open(trace_path, "r") as f:
          self.trace_commands = json.load(f)
          self.total_steps = len(self.trace_commands)
        print(f"Loaded {self.total_steps} commands from {trace_path}")
      except Exception as e:
        print(f"Error loading trace payload from {trace_path}: {e}")
        self.trace_commands = []
        self.total_steps = 0
    else:
      print(f"Warning: Trace file not found at {trace_path}")
      self.trace_commands = []
      self.total_steps = 0

  def validate_and_start_job(
      self, start_step: Any, end_step: Any
  ) -> Tuple[int, Dict[str, Any]]:
    """Validates step bounds and registers an active asynchronous job.

    Returns:
        A tuple of (http_status_code, response_payload_dict).
    """
    with self.lock:
      if not self.trace_commands:
        self.load_trace(get_trace_path())

      if self.active_job_id is not None:
        return 409, {
            "error": "Conflict: An execution chunk is already running on this container.",
            "active_job_id": self.active_job_id,
        }

      if not isinstance(start_step, int) or isinstance(start_step, bool) or not isinstance(end_step, int) or isinstance(end_step, bool):
        return 400, {"error": "start_step and end_step must both be integers."}

      if start_step < 1:
        return 400, {"error": "start_step must be an integer >= 1."}

      if end_step < start_step:
        return 400, {"error": f"end_step ({end_step}) cannot be less than start_step ({start_step})."}

      if self.total_steps > 0 and end_step > self.total_steps:
        return 400, {
            "error": f"end_step ({end_step}) exceeds total available trace steps ({self.total_steps})."
        }

      if start_step == 1:
        pass
      elif self.last_completed_step > 0 and start_step == self.last_completed_step + 1:
        pass
      elif self.last_completed_step == 0:
        return 400, {
            "error": f"Initial execution chunk must begin at start_step 1, got {start_step}."
        }
      else:
        return 400, {
            "error": (
                f"Invalid start_step {start_step}. Expected {self.last_completed_step + 1} "
                f"(previous_end_step + 1) or 1 to reset."
            )
        }

      job_id = str(uuid.uuid4())
      self.active_job_id = job_id
      self.jobs[job_id] = {
          "job_id": job_id,
          "status": "RUNNING",
          "start_step": start_step,
          "end_step": end_step,
          "created_at": time.time(),
          "exit_code": None,
      }

      return 202, {"job_id": job_id, "status": "ACCEPTED"}

  def finish_job(
      self,
      job_id: str,
      status: str,
      completed_step: int,
      exit_code: int,
      execution_duration_ms: Optional[float] = None,
  ) -> None:
    """Updates job completion status and releases the concurrency lock."""
    with self.lock:
      if job_id in self.jobs:
        self.jobs[job_id]["status"] = status
        self.jobs[job_id]["exit_code"] = exit_code
        self.jobs[job_id]["completed_step"] = completed_step
        self.jobs[job_id]["finished_at"] = time.time()
        if execution_duration_ms is not None:
          self.jobs[job_id]["execution_duration_ms"] = execution_duration_ms
      if self.active_job_id == job_id:
        self.active_job_id = None
      self.last_completed_step = completed_step

  def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves the current execution status of a given job ID."""
    with self.lock:
      if job_id not in self.jobs:
        return None
      job_data = {
          "job_id": job_id,
          "status": self.jobs[job_id]["status"],
      }
      if "execution_duration_ms" in self.jobs[job_id]:
        job_data["execution_duration_ms"] = self.jobs[job_id]["execution_duration_ms"]
      return job_data


GLOBAL_STATE = ExecutionState()


def get_trace_path() -> str:
  """Resolves the active trace file path from CLI argument or environment variables."""
  if len(sys.argv) > 1 and sys.argv[1].endswith(".json"):
    return sys.argv[1]
  return os.environ.get("TRACE_PATH", os.environ.get("AGENT_TRACE_PATH", "/trace.json"))


def execute_chunk_worker(job_id: str, start_step: int, end_step: int) -> None:
  """Background daemon worker executing a bounded subset of trace commands."""
  trace_path = get_trace_path()
  if not GLOBAL_STATE.trace_commands or len(GLOBAL_STATE.trace_commands) < end_step:
    GLOBAL_STATE.load_trace(trace_path)

  commands = GLOBAL_STATE.trace_commands
  chunk = commands[start_step - 1 : end_step]
  disable_sleep = os.environ.get("SWEPERF_DISABLE_INTERNAL_SLEEP", "0") in ("1", "true")

  completed_step = start_step - 1
  final_exit_code = 0
  unhandled_exception = False
  total_execution_duration_ms = 0.0

  print(f"[Job {job_id}] Starting execution for steps {start_step}..{end_step} ({len(chunk)} commands)")
  sys.stdout.flush()

  env = os.environ.copy()
  env["PAGER"] = "cat"
  env["GIT_PAGER"] = "cat"

  for i, cmd_item in enumerate(chunk):
    current_step = start_step + i

    if isinstance(cmd_item, dict):
      command = cmd_item.get("command", "")
      sleep_time = cmd_item.get("sleep", None)
    else:
      command = cmd_item
      sleep_time = None

    if not command.strip():
      completed_step = current_step
      continue

    print(f"\033[92m[Step {current_step}]$ {command}\033[0m")
    sys.stdout.flush()

    wrapped_cmd = (
        "if [ -f /home/swe-bench/miniconda3/etc/profile.d/conda.sh ]; then "
        "source /home/swe-bench/miniconda3/etc/profile.d/conda.sh && conda activate testbed; "
        "elif [ -f /opt/miniconda3/bin/activate ]; then "
        "source /opt/miniconda3/bin/activate testbed; fi && "
        f"{command}"
    )

    try:
      cwd_dir = "/testbed" if os.path.exists("/testbed") else os.getcwd()
      cmd_t0 = time.perf_counter()
      result = subprocess.run(
          wrapped_cmd,
          shell=True,
          capture_output=True,
          text=True,
          cwd=cwd_dir,
          executable="/bin/bash",
          env=env,
      )
      cmd_duration_ms = (time.perf_counter() - cmd_t0) * 1000.0
      total_execution_duration_ms += cmd_duration_ms

      if result.stdout:
        sys.stdout.write(result.stdout)
      if result.stderr:
        sys.stderr.write(result.stderr)
      sys.stdout.flush()
      sys.stderr.flush()

      if result.returncode != 0:
        final_exit_code = result.returncode

      completed_step = current_step

    except Exception as e:
      print(f"FATAL EXCEPTION executing step {current_step}: {e}")
      sys.stdout.flush()
      final_exit_code = -1
      unhandled_exception = True
      break

    if not disable_sleep:
      if sleep_time is None:
        mu = float(os.environ.get("LLM_LATENCY_MU", "2.0414"))
        sigma = float(os.environ.get("LLM_LATENCY_SIGMA", "0.8674"))
        min_latency = float(os.environ.get("LLM_LATENCY_MIN", "0.5"))
        sleep_time = max(min_latency, random.lognormvariate(mu, sigma))
      if sleep_time > 0:
        time.sleep(sleep_time)

  final_status = "FAILED" if unhandled_exception else "COMPLETED"
  GLOBAL_STATE.finish_job(
      job_id,
      final_status,
      completed_step,
      final_exit_code,
      round(total_execution_duration_ms, 2),
  )
  print(
      f"[Job {job_id}] Finished with status: {final_status} (Last Completed"
      f" Step: {completed_step}, Exit Code: {final_exit_code}, Execution"
      f" Duration: {round(total_execution_duration_ms, 2)} ms)"
  )
  sys.stdout.flush()


class WorkloadServerHandler(http.server.BaseHTTPRequestHandler):
  """HTTP Request Handler implementing SWE-perf Asynchronous Execution API."""

  def log_message(self, format, *args):
    pass

  def do_GET(self) -> None:
    parsed_url = urllib.parse.urlparse(self.path)
    path = parsed_url.path

    if path in ("/status", "/health"):
      query_params = urllib.parse.parse_qs(parsed_url.query)

      if "job_id" in query_params:
        job_id = query_params["job_id"][0]
        status_data = GLOBAL_STATE.get_job_status(job_id)
        if status_data is None:
          self.send_response(404)
          self.send_header("Content-Type", "application/json")
          self.end_headers()
          self.wfile.write(json.dumps({"error": f"Job ID {job_id} not found."}).encode("utf-8"))
          return

        response_bytes = json.dumps(status_data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)
        return

      liveness_payload = {
          "status": "UP",
          "total_steps": GLOBAL_STATE.total_steps,
          "last_completed_step": GLOBAL_STATE.last_completed_step,
      }
      response_bytes = json.dumps(liveness_payload).encode("utf-8")
      self.send_response(200)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(response_bytes)))
      self.end_headers()
      self.wfile.write(response_bytes)
      return

    self.send_response(404)
    self.end_headers()

  def do_POST(self) -> None:
    parsed_url = urllib.parse.urlparse(self.path)
    if parsed_url.path not in ("/execute", "/"):
      self.send_response(404)
      self.end_headers()
      return

    content_length = int(self.headers.get("Content-Length", 0))
    post_data = self.rfile.read(content_length)

    try:
      payload = json.loads(post_data.decode("utf-8"))
      if not isinstance(payload, dict):
        raise ValueError("Payload must be a JSON object.")
    except Exception as e:
      self.send_response(400)
      self.send_header("Content-Type", "application/json")
      self.end_headers()
      self.wfile.write(json.dumps({"error": f"Invalid JSON payload: {e}"}).encode("utf-8"))
      return

    start_step = payload.get("start_step")
    end_step = payload.get("end_step")

    if start_step is None or end_step is None:
      self.send_response(400)
      self.send_header("Content-Type", "application/json")
      self.end_headers()
      self.wfile.write(
          json.dumps({"error": "Both 'start_step' and 'end_step' are required."}).encode("utf-8")
      )
      return

    status_code, response_body = GLOBAL_STATE.validate_and_start_job(start_step, end_step)

    if status_code == 202:
      t = threading.Thread(
          target=execute_chunk_worker,
          args=(response_body["job_id"], start_step, end_step),
          daemon=True,
      )
      t.start()

    response_bytes = json.dumps(response_body).encode("utf-8")
    self.send_response(status_code)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(response_bytes)))
    self.end_headers()
    self.wfile.write(response_bytes)


def main(
    server_class=http.server.HTTPServer,
    handler_class=WorkloadServerHandler,
    port: int = 80,
) -> None:
  trace_path = get_trace_path()
  GLOBAL_STATE.load_trace(trace_path)

  server_port = int(os.environ.get("PORT", port))
  server_address = ("0.0.0.0", server_port)
  httpd = server_class(server_address, handler_class)
  print(f"Starting SWE-perf Asynchronous Execution Server on port {server_port} (Total Steps: {GLOBAL_STATE.total_steps})...")
  sys.stdout.flush()
  httpd.serve_forever()


if __name__ == "__main__":
  main()
