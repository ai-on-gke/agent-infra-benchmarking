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

"""Minimal activation-latency probe.

Stands in for an agent: the benchmark's runworkload package only needs proof
that a freshly provisioned sandbox is up and running its application code,
not that the application does anything real. Kept deliberately smaller than
an actual agent runtime so the benchmark measures agent-sandbox's activation
path rather than an application's own startup cost.

GET  /healthz -> 200 "ok", used as the container's readinessProbe. A Sandbox
                 only reports Ready once this (and its pod IP/Service) are up,
                 so the benchmark's provisioning window ends here.
POST /ping    -> echoes back the JSON body's "nonce" field. The echo is what
                 proves the sandbox ran the instruction rather than merely
                 accepting the connection — see internal/benchmarking/boomer/
                 runworkload/ping.go, which sends and verifies it.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/ping":
            self._reply(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length).decode()) if length else {}
        except json.JSONDecodeError as e:
            self._reply(400, {"error": str(e)})
            return
        self._reply(200, {"nonce": req.get("nonce", "")})

    def log_message(self, *args):  # keep container logs quiet
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8888), Handler).serve_forever()
