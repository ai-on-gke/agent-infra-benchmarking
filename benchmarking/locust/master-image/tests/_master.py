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

"""What the master does for every user class: the master's stop at the end of
a run, and its steady-state stats. A user class's locustfile beside this one
imports it.

The leading underscore keeps locust from taking it for a locustfile when it is
given the whole directory.
"""

import csv
import datetime
import json
import logging
import time
from types import SimpleNamespace

import gevent
from locust import events
from locust.runners import MasterRunner
from locust.stats import PERCENTILES_TO_REPORT, RequestStats, StatsCSV, StatsEntry
from locust.util.timespan import parse_timespan

# How long the master waits for the worker's teardown. Above boomer-worker's
# teardownTimeout plus its final stats report.
WORKER_STOP_TIMEOUT_SECS = 60


@events.init_command_line_parser.add_listener
def add_arguments(parser) -> None:
    parser.add_argument(
        "--warm-up",
        type=parse_timespan,
        default=None,
        help="How long after the last user starts the steady-state window opens. "
        "Unset writes no steady-state stats.",
    )


@events.init.add_listener
def track_steady_state(environment, **_kwargs) -> None:
    options = environment.parsed_options
    if isinstance(environment.runner, MasterRunner) and options.warm_up is not None and options.csv_prefix:
        SteadyState(environment, options.warm_up, options.csv_prefix)


class SteadyState:
    """locust's stats over the steady-state window only: from --warm-up after
    the last user started until the run starts to stop, so the teardown is
    left out too. Taken from the same worker reports as locust's own stats.

    A report holds the requests the worker finished since its last one, 3s
    earlier. The first report past the warm-up is partly from before it, so it
    marks the window's start rather than being counted, and the last report
    counted marks its end.

    Writes <csv-prefix>_stats_steady.csv, and the window's bounds to
    <csv-prefix>_steady_window.json.
    """

    def __init__(self, environment, warm_up: int, csv_prefix: str) -> None:
        self.warm_up = warm_up
        self.csv_prefix = csv_prefix
        self.opens_at: float | None = None
        self.stopping = False
        self.reporting: set[str] = set()
        self.start: float | None = None
        self.end: float | None = None
        self.stats: RequestStats | None = None
        environment.events.spawning_complete.add_listener(self.on_spawning_complete)
        environment.events.worker_report.add_listener(self.on_worker_report)
        environment.events.test_stopping.add_listener(self.on_test_stopping)
        environment.events.quitting.add_listener(self.on_quitting)

    def on_spawning_complete(self, **_kwargs) -> None:
        if self.opens_at is None:
            self.opens_at = time.time() + self.warm_up

    def on_worker_report(self, client_id, data, **_kwargs) -> None:
        now = time.time()
        if self.opens_at is None or self.stopping or now < self.opens_at:
            return
        if client_id not in self.reporting:
            self.reporting.add(client_id)
            self.start = self.start or now
            return
        # Created here rather than up front: its start time is what the
        # Requests/s columns divide by.
        if self.stats is None:
            self.stats = RequestStats(use_response_times_cache=False)
        for entry in data["stats"]:
            report = StatsEntry.unserialize(entry)
            self.stats.get(report.name, report.method).extend(report)
        self.stats.total.extend(StatsEntry.unserialize(data["stats_total"]))
        self.end = now

    def on_test_stopping(self, **_kwargs) -> None:
        self.stopping = True

    def on_quitting(self, **_kwargs) -> None:
        if self.stats is None:
            logging.getLogger(__name__).warning(
                "No steady-state window: the run ended within %ss of its last user starting.", self.warm_up
            )
            return
        with open(f"{self.csv_prefix}_stats_steady.csv", "w", newline="") as f:
            StatsCSV(SimpleNamespace(stats=self.stats), PERCENTILES_TO_REPORT).requests_csv(csv.writer(f))
        with open(f"{self.csv_prefix}_steady_window.json", "w") as f:
            json.dump({"warm_up_secs": self.warm_up, "start": _iso(self.start), "end": _iso(self.end)}, f)


def _iso(t: float | None) -> str | None:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat() if t else None


@events.test_stop.add_listener
def stop_workers(environment, **_kwargs) -> None:
    """Have the worker delete its actors while the master still takes its stats.

    A headless run ends by sending the worker "quit" and stops listening half a
    second later, which lost the teardown's DeleteActor rows. test_stop fires
    after the master has left the running state, so a worker that registers
    again once stopped is not given users again.
    """
    runner = environment.runner
    if not isinstance(runner, MasterRunner):
        return
    # Not a custom message: boomer reads it as the master's own stop.
    runner.send_message("stop")
    deadline = time.monotonic() + WORKER_STOP_TIMEOUT_SECS
    while runner.user_count and time.monotonic() < deadline:
        gevent.sleep(1)
    if runner.user_count:
        logging.getLogger(__name__).warning(
            "Workers did not stop within %ss; their teardown may be missing from the stats.",
            WORKER_STOP_TIMEOUT_SECS,
        )
