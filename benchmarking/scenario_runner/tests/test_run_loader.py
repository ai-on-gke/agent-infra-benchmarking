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

"""run_loader.py, which runs swebench-loader in a case's Job and uploads what
it wrote: with a fake loader that writes the files the real one does, and a
fake of the Cloud Storage endpoint it uploads to."""

import base64
import importlib.util
import io
import json
import os
import signal
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "swebench" / "image" / "run_loader.py"
spec = importlib.util.spec_from_file_location("run_loader", SCRIPT)
run_loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_loader)

FAKE_LOADER = f"""#!{sys.executable}
import json, os, signal, sys, time

argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")


def flag(name):
    for arg in argv:
        if arg.startswith("-" + name + "="):
            return arg.split("=", 1)[1]


if argv[0] == "run":
    if os.environ.get("FAKE_SLEEP"):
        def term(*_):
            open(os.environ["FAKE_MARK"], "w").write("term")
            sys.exit(0)

        signal.signal(signal.SIGTERM, term)
        open(os.environ["FAKE_STARTED"], "w").write("1")
        time.sleep(float(os.environ["FAKE_SLEEP"]))
    if os.environ.get("FAKE_KILL"):
        os.kill(os.getpid(), signal.SIGKILL)
    if not os.environ.get("FAKE_NO_EVENTS"):
        open(flag("out"), "w").write('{{"event": 1}}\\n')
        open(flag("stats-out"), "w").write('{{"metric": "m"}}\\n')
        open(flag("run-window"), "w").write('{{"run_id": "r"}}')
    sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
if argv[0] == "report":
    open(argv[argv.index("-json") + 1], "w").write('{{"count": 1}}')
    print("report of", argv[1])
"""


class Storage(ThreadingHTTPServer):
    """A fake of the Cloud Storage JSON API's upload: what it was sent, and
    the statuses to answer the first uploads with."""

    def __init__(self, statuses: list[int] = ()):
        self.uploads: list[dict] = []
        self.statuses = list(statuses)
        super().__init__(("127.0.0.1", 0), self.Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            body = self.rfile.read(int(self.headers["Content-Length"]))
            status = self.server.statuses.pop(0) if self.server.statuses else 200
            self.server.uploads.append(
                {
                    "path": parsed.path,
                    "name": query["name"][0],
                    "type": self.headers["Content-Type"],
                    "auth": self.headers["Authorization"],
                    "body": body,
                    "status": status,
                }
            )
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"{}" if status == 200 else b'{"error": "no"}')

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def stored(self) -> dict[str, bytes]:
        return {u["name"]: u["body"] for u in self.uploads if u["status"] == 200}


class RunLoader(unittest.TestCase):
    ARGS = ["-mode=eval", "-width=2"]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.loader = self.tmp / "swebench-loader"
        self.loader.write_text(FAKE_LOADER)
        self.loader.chmod(self.loader.stat().st_mode | stat.S_IXUSR)
        self.out = self.tmp / "out"
        self.log = self.tmp / "argv.log"
        self.server = Storage()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = {
            "SWEBENCH_LOADER": str(self.loader),
            "OUT_DIR": str(self.out),
            "FAKE_LOG": str(self.log),
            "LOADER_ARGS": self.encode(self.ARGS),
            "BENCHMARK_RUN_INFO": self.encode({"case": "c", "commit": "abc"}),
            "REPORT_BUCKET": "bucket",
            "REPORT_PREFIX": "reports",
            "BENCHMARK_RUN_ID": "run-1",
            "BENCHMARK_REPORT_FOLDER": "c",
            "STORAGE_API": self.server.url,
            "STORAGE_ACCESS_TOKEN": "tok",
        }

    @staticmethod
    def encode(value: object) -> str:
        return base64.b64encode(json.dumps(value).encode()).decode()

    def main(self, **env) -> int:
        environ = {**{k: v for k, v in os.environ.items() if not k.startswith(("FAKE_", "REPORT_", "STORAGE_"))}, **self.env, **env}
        with mock.patch.dict(os.environ, environ, clear=True), redirect_stdout(io.StringIO()) as out:
            self.printed = out
            return run_loader.main()

    def argv(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_it_uploads_what_the_loader_wrote_under_the_runs_folder(self):
        self.assertEqual(self.main(), 0)
        stored = self.server.stored()
        self.assertEqual(
            sorted(stored),
            [
                f"reports/run-1/c/{name}"
                for name in ("events.jsonl", "report.txt", "run.json", "run_window.json", "stats.jsonl", "summary.json")
            ],
        )
        self.assertEqual(stored["reports/run-1/c/events.jsonl"], b'{"event": 1}\n')
        self.assertEqual(json.loads(stored["reports/run-1/c/summary.json"]), {"count": 1})
        self.assertIn("report of", stored["reports/run-1/c/report.txt"].decode())
        for upload in self.server.uploads:
            self.assertEqual(upload["auth"], "Bearer tok")
            self.assertEqual(upload["path"], "/upload/storage/v1/b/bucket/o")
        types = {u["name"].rsplit("/", 1)[1]: u["type"] for u in self.server.uploads}
        self.assertEqual(types["events.jsonl"], "application/x-ndjson")
        self.assertEqual(types["run.json"], "application/json")
        self.assertEqual(types["report.txt"], "text/plain")

    def test_run_json_is_the_run_info_and_what_the_loader_did(self):
        self.main()
        info = json.loads(self.server.stored()["reports/run-1/c/run.json"])
        self.assertEqual((info["case"], info["commit"], info["loader_exit_code"]), ("c", "abc", 0))
        self.assertLessEqual(info["loader_started_at"], info["loader_ended_at"])

    def test_the_loader_is_run_with_the_args_and_its_files_in_the_out_dir(self):
        self.main()
        run, report = self.argv()
        self.assertEqual(run[:3], ["run", "-mode=eval", "-width=2"])
        self.assertEqual(
            run[3:],
            [f"-out={self.out}/events.jsonl", f"-stats-out={self.out}/stats.jsonl", f"-run-window={self.out}/run_window.json"],
        )
        self.assertEqual(report, ["report", f"{self.out}/events.jsonl", "-json", f"{self.out}/summary.json"])
        self.assertIn("report of", self.printed.getvalue())

    def test_a_failed_loader_exit_status_is_the_jobs_and_its_events_are_kept(self):
        self.assertEqual(self.main(FAKE_EXIT="3"), 3)
        stored = self.server.stored()
        self.assertIn("reports/run-1/c/events.jsonl", stored)
        self.assertEqual(json.loads(stored["reports/run-1/c/run.json"])["loader_exit_code"], 3)

    def test_a_loader_killed_by_a_signal_has_the_shells_status(self):
        self.assertEqual(self.main(FAKE_KILL="1"), 128 + signal.SIGKILL)
        # It wrote no events, so there is nothing to summarize, and the rest goes up.
        self.assertEqual(sorted(self.server.stored()), ["reports/run-1/c/run.json"])

    def test_without_events_it_does_not_summarize(self):
        self.assertEqual(self.main(FAKE_NO_EVENTS="1"), 0)
        self.assertEqual([a[0] for a in self.argv()], ["run"])
        self.assertEqual(sorted(self.server.stored()), ["reports/run-1/c/run.json"])

    def test_a_failed_upload_fails_the_job_even_if_the_loader_did_not(self):
        self.server.statuses = [403] * 20
        self.assertEqual(self.main(), 1)

    def test_a_failed_upload_keeps_the_loaders_own_status(self):
        self.server.statuses = [403] * 20
        self.assertEqual(self.main(FAKE_EXIT="3"), 3)

    def test_a_server_error_is_retried(self):
        self.server.statuses = [503, 503]
        with mock.patch.object(run_loader.time, "sleep"):
            self.assertEqual(self.main(), 0)
        self.assertIn("reports/run-1/c/events.jsonl", self.server.stored())
        # The first object took three tries.
        self.assertEqual([u["status"] for u in self.server.uploads[:3]], [503, 503, 200])

    def test_a_client_error_is_not_retried(self):
        self.server.statuses = [403]
        with mock.patch.object(run_loader.time, "sleep") as sleep:
            self.main()
        sleep.assert_not_called()
        self.assertEqual(len([u for u in self.server.uploads if u["status"] == 403]), 1)

    def test_without_a_bucket_nothing_is_uploaded(self):
        del self.env["REPORT_BUCKET"]
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.server.uploads, [])
        self.assertTrue((self.out / "events.jsonl").is_file())

    def test_without_an_access_token_it_fails_after_running(self):
        del self.env["STORAGE_ACCESS_TOKEN"]
        with mock.patch.object(run_loader.urllib.request, "urlopen", side_effect=OSError("no metadata server")):
            self.assertEqual(self.main(), 1)
        self.assertEqual(self.server.uploads, [])
        self.assertTrue((self.out / "events.jsonl").is_file())

    def test_the_loaders_files_are_not_for_the_case_to_name(self):
        for flag in ("-out=/x", "--stats-out", "-run-window=/y"):
            with self.subTest(flag), self.assertRaisesRegex(SystemExit, "which run_loader.py sets"):
                self.main(LOADER_ARGS=self.encode([flag]))
        # Another flag that merely starts like one is fine.
        self.assertEqual(self.main(LOADER_ARGS=self.encode(["-output-width=3"])), 0)

    def test_args_that_are_not_a_list_of_strings_are_refused(self):
        for value in ({"a": 1}, ["-ok", 3], "text"):
            with self.subTest(value), self.assertRaisesRegex(SystemExit, "JSON list of strings"):
                self.main(LOADER_ARGS=self.encode(value))

    def test_args_that_are_not_base64_json_are_refused(self):
        with self.assertRaisesRegex(SystemExit, "LOADER_ARGS is not base64 of JSON"):
            self.main(LOADER_ARGS="***")

    def test_a_sigterm_goes_to_the_loader_which_ends_the_run_and_the_events_are_uploaded(self):
        started, mark = self.tmp / "started", self.tmp / "mark"
        env = {"FAKE_SLEEP": "20", "FAKE_STARTED": str(started), "FAKE_MARK": str(mark)}

        def terminate():
            deadline = time.monotonic() + 15
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        # A signal that arrives before run_loader has taken it is dropped, not fatal.
        previous = signal.signal(signal.SIGTERM, lambda *_: None)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        threading.Thread(target=terminate, daemon=True).start()
        began = time.monotonic()
        self.assertEqual(self.main(**env), 0)
        self.assertLess(time.monotonic() - began, 15)
        self.assertEqual(mark.read_text(), "term")
        # The loader's handler stopped it before it wrote any events; the run is still recorded.
        self.assertIn("reports/run-1/c/run.json", self.server.stored())
        # run_loader leaves the process's own handler as it found it.
        self.assertIsNotNone(signal.getsignal(signal.SIGTERM))


if __name__ == "__main__":
    unittest.main()
