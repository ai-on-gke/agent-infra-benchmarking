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

"""Runs one benchmark to completion and exits with its result.

locust runs headless as a master and the boomer-worker Go binary as its only
worker, both as children of this process, so a Job that runs this ends when
the run ends rather than being stopped from outside. The ordering that makes
that work:

  * at locust's -t the master sends the worker a "stop" and waits while it
    suspends and deletes every actor it created (tests/runworkload.py), so
    those calls are in locust's stats. Then it sends a "quit".
  * boomer returns from its run loop on that quit and waits for a last
    Prometheus scrape. The SIGTERM below is the fallback for a master that
    died without sending one; boomer then deletes the actors itself.
  * this process exits once both have, and its status is the run's.

Adapted from substrate's benchmarking/locust/runner.py, minus the two parts
that do not apply here. The Python user classes are gone, and with them the
choice of load generator: runworkload is implemented in boomer, so boomer
always runs. The result upload is gone as well — the results of a run are the
logs of its pod, so there is no --dest, no JSONL conversion and no CSV.

Upstream's load shapes are not carried over either: -f takes one test file,
because this repo ships no shapes/ directory for the second path to name.
"""

import argparse
import base64
import csv
import datetime
import json
import os
import subprocess
import signal
import sys
import threading
import time
from pathlib import Path
from typing import IO

from reports.prometheus import DEFAULT_URL as DEFAULT_PROMETHEUS_URL

# Path inside the locust image to the boomer-worker binary baked in by
# benchmarking/locust/Dockerfile.
BOOMER_BINARY = "/app/boomer-worker"

# Where --test-case looks, copied into the image from benchmarking/tests.yaml.
TESTS_FILE = "/app/tests.yaml"

# Prefix locust's --csv appends _stats.csv, _stats_history.csv, _failures.csv
# and _exceptions.csv to, inside --report-dir so one folder holds everything a
# run produced and the upload collects it in one pass.
LOCUST_CSV_STEM = "locust"

# The case keys that are the run's load rather than the worker's flags. Every
# key in a case that is not one of these, HOST_KEYS or FAILURE_RATIO_KEY goes
# to the worker verbatim.
LOAD_KEYS = ("duration", "users", "spawn-rate")

# Case keys that shape the WorkerPool, which run_locust_job.sh deploys before
# the Job starts. Dropped here rather than passed on: the pool is already up by
# the time this runs, and the worker has no such flags — an unknown one is
# fatal to it, so a case carrying these would never reach a first iteration.
HOST_KEYS = ("worker-count", "sandbox-class")

# Case key for the share of requests that may fail before the run counts as
# failed, and its default. locust alone would fail it on the first one, out of
# tens of thousands.
FAILURE_RATIO_KEY = "max-failure-ratio"
DEFAULT_MAX_FAILURE_RATIO = 0.01

# How long boomer gets to suspend and delete its actors after the run. Longer
# than the worker's own teardown and final scrape wait, so the timeout here is
# reached only by a worker that is not going to finish at all.
BOOMER_STOP_TIMEOUT_SECS = 300

# Prometheus scrapes every 10s, so the run's last samples land after the run
# does. Reports wait this long before querying rather than missing the tail.
REPORT_SETTLE_SECS = 15

# Exit status of a run whose reports did not all upload. Distinct from locust's
# and boomer's own, which say how the run itself went.
UPLOAD_FAILED_EXIT = 3

# Exit status of a run over its max-failure-ratio, or one whose load generator
# raised exceptions.
TOO_MANY_FAILURES_EXIT = 4

# What the run was made of and how it went, written beside the reports; the
# README summarizes it for a reader.
RUN_JSON = "run.json"
README = "README.md"

# How far back refuse_reused_run looks for an earlier run's series: past
# Prometheus's retention, so anything it still holds is found.
PRIOR_RUN_LOOKBACK = "30d"

# Bucket subfolder the reports are uploaded under. Mirrors
# reports.upload.DEFAULT_PREFIX, named here so --help states it without
# importing a package that may be absent.
REPORT_PREFIX = "reports"


def report_names() -> list[str]:
    """Registered report names, or empty when the reports package cannot
    load. Imported lazily so a broken package costs the reports and not the
    run."""
    try:
        import reports

        return reports.names()
    except Exception:
        return []


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("-f", required=True, dest="file", help="Locust test file (-f)")
    # A case is the whole run: the load and the worker's flags both, named as
    # one word so the run is reproducible from it.
    p.add_argument(
        "--test-case",
        required=True,
        help=f"Name a case in --tests-file. {', '.join(LOAD_KEYS)} are the "
        f"load, {FAILURE_RATIO_KEY} its pass mark, and {', '.join(HOST_KEYS)} "
        f"the pool's; every other key is a worker flag.",
    )
    p.add_argument(
        "--tests-file",
        default=TESTS_FILE,
        help=f"Where --test-case looks (default: {TESTS_FILE}).",
    )
    p.add_argument(
        "--run-id",
        default="",
        help="The run this test case is part of: the run_id label in Prometheus, "
        "and the <run-id> of the upload folder. Reports query on it with the "
        "test_case label, so without it they are skipped.",
    )
    p.add_argument(
        "--report",
        action="append",
        default=[],
        metavar="NAME",
        help=f"Report to write when the run ends; repeatable. Known: "
        f"{', '.join(report_names())}, or 'all'.",
    )
    p.add_argument(
        "--report-dir",
        default="/app/reports",
        help="Where --report writes. Pod-local unless a volume is mounted here.",
    )
    p.add_argument(
        "--prometheus-url",
        default=DEFAULT_PROMETHEUS_URL,
        help="Prometheus the reports read the run back from.",
    )
    p.add_argument(
        "--report-bucket",
        default="",
        help="GCS bucket the reports are uploaded to, as gs://<bucket>/"
        "<prefix>/<run-id>/<test-case>/. Empty leaves them on the pod, where the Job's "
        "TTL eventually takes them.",
    )
    p.add_argument(
        "--report-prefix",
        default=REPORT_PREFIX,
        help="Subfolder under --report-bucket.",
    )
    p.add_argument(
        "--actor-egress-allow",
        default="",
        help="Comma-separated hosts every actor may connect to, passed to the "
        "worker's flag of the same name. Empty leaves the case's own, if any.",
    )
    args, extra = p.parse_known_args()
    case = load_test_case(args.tests_file, args.test_case)
    # Split before anything reads either half: what is left after the load
    # comes out is the worker's, whatever it is named.
    args.duration, args.users, args.spawn_rate = take_load(case, args.test_case)
    args.max_failure_ratio = take_failure_ratio(case, args.test_case)
    for key in HOST_KEYS:
        case.pop(key, None)
    if args.actor_egress_allow:
        case["actor-egress-allow"] = ",".join(
            h for h in (case.get("actor-egress-allow", ""), args.actor_egress_allow) if h
        )
    args.worker_flags = case
    # The manifest passes --report unconditionally, so an unset one arrives as
    # an empty string rather than not at all.
    args.report = [r for r in args.report if r]
    # Everything unrecognized goes to locust verbatim, which is how a run
    # reaches flags this wrapper has no opinion about (--exit-code-on-error,
    # --stop-timeout, and so on).
    args.locust_extra = extra
    return args


def take_failure_ratio(case: dict[str, str], name: str) -> float:
    """Remove FAILURE_RATIO_KEY from `case` and return it validated, or the
    default when the case does not state one."""
    raw = case.pop(FAILURE_RATIO_KEY, None)
    if raw is None:
        return DEFAULT_MAX_FAILURE_RATIO
    try:
        ratio = float(raw)
    except ValueError:
        sys.exit(f"Test case {name!r}: `{FAILURE_RATIO_KEY}` must be a number.")
    if not 0 <= ratio <= 1:
        sys.exit(f"Test case {name!r}: `{FAILURE_RATIO_KEY}` must be between 0 and 1, got {ratio:g}.")
    return ratio


def take_load(case: dict[str, str], name: str) -> tuple[str, int, float]:
    """Remove the load keys from `case` and return them validated.

    The checks are here because nothing downstream makes them: locust reads -u
    and -r as numbers and a wrong one becomes a traceback inside the pod, long
    after the Job was accepted. The duration is left to locust, which knows its
    own spelling of one and rejects anything else at startup.
    """
    for key in ("duration", "users"):
        if key not in case:
            sys.exit(f"Test case {name!r} has no `{key}`; a case states the load it runs at.")

    duration = case.pop("duration")
    try:
        users = int(case.pop("users"))
    except ValueError:
        sys.exit(f"Test case {name!r}: `users` must be a whole number.")
    if users < 1:
        sys.exit(f"Test case {name!r}: `users` must be at least 1, got {users}.")

    # Locust fills in 1 user/sec when -r is absent, which makes the ramp of a
    # large run longer than the run: 100 users would spend 100s climbing, and
    # every sample taken during the climb is at a concurrency the run never
    # reports. Defaulting to the user count puts the whole load on at the first
    # dispatch instead, so the measurement covers one load level.
    raw = case.pop("spawn-rate", None)
    if raw is None:
        return duration, users, float(users)
    try:
        spawn_rate = float(raw)
    except ValueError:
        sys.exit(f"Test case {name!r}: `spawn-rate` must be a number.")
    if spawn_rate <= 0:
        sys.exit(f"Test case {name!r}: `spawn-rate` must be positive, got {spawn_rate}.")
    return duration, users, spawn_rate


def load_test_case(path: str, name: str) -> dict[str, str]:
    """The named case from `path`, or exit saying which names it does hold."""
    try:
        import yaml
    except ModuleNotFoundError:
        sys.exit("--test-case needs PyYAML; add it to locust/requirements.txt.")

    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except OSError as e:
        sys.exit(f"Cannot read {path}: {e}")
    except yaml.YAMLError as e:
        sys.exit(f"{path} is not valid YAML: {e}")

    cases = doc.get("cases")
    if not isinstance(cases, dict):
        sys.exit(f"{path} has no `cases` mapping.")
    if name not in cases:
        sys.exit(f"No test case {name!r} in {path}. Known: {', '.join(sorted(cases)) or '(none)'}")

    case = cases[name] or {}
    if not isinstance(case, dict):
        sys.exit(f"Test case {name!r} in {path} is not a mapping of flags.")
    # str() rather than the YAML type: every one of these is about to be a
    # command-line argument, and a bare 0.05 or 2s parses as float and str
    # respectively for no difference that survives the argv.
    return {str(k): str(v) for k, v in case.items()}


def user_class(test_path: str) -> str:
    """The boomer --user-class that `test_path` names.

    The file name is the class name throughout this repo: deploy.sh accepts a
    --user-class only when tests/NAME.py exists, locust is given that file,
    and NAME is the entry the Go package registered with userclass.Add.
    """
    return Path(test_path).stem


def pump_stream(prefix: str, stream: IO[str]) -> None:
    """Forward each line of `stream` to stdout under a per-source prefix, so
    the two subprocesses stay tellable apart in one stream of pod logs."""
    for line in stream:
        sys.stdout.write(f"[{prefix}] {line.rstrip()}\n")
        sys.stdout.flush()


def log_run_config(args: argparse.Namespace) -> None:
    for line in (
        "==== Run config ====",
        f"  file:         {args.file}",
        f"  user_class:   {user_class(args.file)}",
        f"  duration:     {args.duration}",
        f"  users:        {args.users}",
        f"  spawn_rate:   {args.spawn_rate:g}/s"
        + (" (all at once)" if args.spawn_rate >= args.users else ""),
        f"  test_case:    {args.test_case or '(none)'}",
        f"  worker flags: {' '.join(f'--{k}={v}' for k, v in args.worker_flags.items()) or '(none)'}",
        f"  max failures: {args.max_failure_ratio:.2%} of requests",
        f"  reports:      {', '.join(args.report) or '(none)'}",
        f"  run_id:       {args.run_id or '(none)'}",
        f"  report dest:  {f'gs://{args.report_bucket}/{args.report_prefix}/{args.run_id}/{args.test_case}' if args.report_bucket else args.report_dir + ' (pod-local)'}",
        f"  extra flags:  {' '.join(args.locust_extra) or '(none)'}",
        "====================",
    ):
        print(line, flush=True)


def build_locust_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable, "-m", "locust",
        "--headless",
        "-f", args.file,
        "-t", args.duration,
        "-u", str(args.users),
        # Without this locust defaults to 1 user/sec; see parse_args.
        "-r", f"{args.spawn_rate:g}",
        # Master mode so boomer can connect as a worker on localhost:5557,
        # and --expect-workers so the clock does not start until it has. The
        # seconds before a worker attaches would otherwise be counted as part
        # of the run rather than as part of its setup.
        "--master", "--expect-workers", "1",
        # The run's latency percentiles. The worker's Prometheus histogram has
        # power-of-two buckets, too coarse to report them from.
        "--csv", str(Path(args.report_dir) / LOCUST_CSV_STEM),
        # A row per second per request name, rather than one Aggregated row.
        # The user count is recorded in the row with the percentiles, so a
        # ramp needs nothing inferred to say what load a sample was taken at.
        "--csv-full-history",
        # Failed requests are judged by within_limits against the case's
        # max-failure-ratio instead.
        "--exit-code-on-error", "0",
    ]
    return cmd + list(args.locust_extra)


def build_boomer_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        BOOMER_BINARY,
        "--user-class", user_class(args.file),
    ]
    return cmd + [f"--{name}={value}" for name, value in args.worker_flags.items()]


def stop_boomer(proc: subprocess.Popen) -> int:
    """Shut boomer down and return its exit status.

    Normally it is already on its way out by the time this runs, having been
    told to quit by the master. The signal is for the case where it was not,
    and is safe in either: the worker keeps handling SIGTERM after its run
    loop returns, so one arriving mid-teardown is ignored rather than cutting
    the actor cleanup short.
    """
    print("Stopping boomer...", flush=True)
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=BOOMER_STOP_TIMEOUT_SECS)
    except subprocess.TimeoutExpired:
        print(
            f"Boomer did not exit within {BOOMER_STOP_TIMEOUT_SECS}s; killing. "
            "Actors it created may be left behind.",
            flush=True,
        )
        proc.kill()
        proc.wait()
    print(f"Boomer exited with code {proc.returncode}", flush=True)
    return proc.returncode


def run(args: argparse.Namespace) -> int:
    # locust opens its CSVs at startup, so the directory has to exist before
    # it launches rather than when the reports are written.
    Path(args.report_dir).mkdir(parents=True, exist_ok=True)

    locust_cmd = build_locust_cmd(args)
    print(f"Running: {' '.join(locust_cmd)}", flush=True)
    locust_proc = subprocess.Popen(
        locust_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, text=True,
    )

    # This is PID 1 in the pod, which ignores any signal it has no handler for:
    # without one, deleting the Job left the pod to be killed at the end of its
    # grace period, with the actors undeleted and nothing uploaded. Stopping
    # locust instead lets the path below wind the run down as it would at the
    # end of its duration.
    stopped_by: list[int] = []

    def stop(signum: int, _frame: object) -> None:
        if not stopped_by:
            print(f"Received {signal.Signals(signum).name}; stopping locust.", flush=True)
            stopped_by.append(signum)
        if locust_proc.poll() is None:
            locust_proc.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    boomer_cmd = build_boomer_cmd(args)
    print(f"Running: {' '.join(boomer_cmd)}", flush=True)
    boomer_proc = subprocess.Popen(
        boomer_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, text=True,
    )

    pumps = [
        threading.Thread(target=pump_stream, args=(name, proc.stdout), daemon=True)
        for name, proc in (("locust", locust_proc), ("boomer", boomer_proc))
    ]
    for t in pumps:
        t.start()

    locust_exit = locust_proc.wait()
    print(f"Locust exited with code {locust_exit}", flush=True)
    boomer_exit = stop_boomer(boomer_proc)

    for t in pumps:
        t.join(timeout=5)

    # A stopped run did not run its duration, so whatever locust concluded it
    # is not a result.
    if stopped_by:
        return 128 + stopped_by[0]

    # Both statuses matter, because the exit code is the only result this
    # carries: a worker that died took the load generator with it whatever
    # locust concluded. Locust's own is preferred when both failed, as the more
    # specific of the two.
    if locust_exit != 0:
        return locust_exit
    return boomer_exit


def count_failures(args: argparse.Namespace) -> dict | None:
    """The run's requests, failed requests and load generator exceptions, from
    the CSVs locust wrote on exit. None, and said why, if they cannot be read."""
    stats = Path(args.report_dir) / f"{LOCUST_CSV_STEM}_stats.csv"
    exceptions = Path(args.report_dir) / f"{LOCUST_CSV_STEM}_exceptions.csv"
    try:
        with stats.open(newline="") as f:
            total = next(row for row in csv.DictReader(f) if row["Name"] == "Aggregated")
        requests, failed = int(total["Request Count"]), int(total["Failure Count"])
        with exceptions.open(newline="") as f:
            raised = sum(int(row["Count"]) for row in csv.DictReader(f))
    except (OSError, StopIteration, KeyError, ValueError) as exc:
        print(f"Cannot count the run's failures: {type(exc).__name__}: {exc}", flush=True)
        return None
    return {
        "requests": requests,
        "failed": failed,
        "ratio": failed / requests if requests else None,
        "exceptions": raised,
    }


def within_limits(failures: dict | None, max_ratio: float) -> bool:
    """Whether the run stayed within its max-failure-ratio and its load
    generator raised no exceptions. --exit-code-on-error 0 keeps both out of
    locust's own exit code, so this is what fails a run over them."""
    if failures is None:
        return False
    if not failures["requests"]:
        print("The run made no requests.", flush=True)
        return False
    print(
        f"Failed requests: {failures['failed']} of {failures['requests']} ({failures['ratio']:.3%}); "
        f"{FAILURE_RATIO_KEY} allows {max_ratio:.3%}.",
        flush=True,
    )
    if failures["exceptions"]:
        print(f"The load generator raised {failures['exceptions']} exception(s).", flush=True)
    return failures["ratio"] <= max_ratio and not failures["exceptions"]


def locust_csvs(args: argparse.Namespace) -> list[Path]:
    """The CSVs locust wrote for this run, already complete by the time it
    exited. Collected by glob rather than by name so a file locust adds in a
    later version is uploaded without this list being updated."""
    return sorted(Path(args.report_dir).glob(f"{LOCUST_CSV_STEM}_*.csv"))


def refuse_reused_run(args: argparse.Namespace) -> None:
    """Exit if Prometheus already holds series for this run id and case.

    The report queries select on those two labels and sum across pods, so a
    second run under them would be added to the first. run_locust_job.sh
    refuses an id whose reports are in the bucket; this also catches an earlier
    run that stopped before it uploaded. Runs before locust starts, so none of
    the series can be this run's own."""
    if not args.run_id:
        return
    query = (
        "count(last_over_time(locust_requests_total"
        f'{{run_id="{args.run_id}",test_case="{args.test_case}"}}[{PRIOR_RUN_LOOKBACK}]))'
    )
    try:
        from reports import prometheus

        found = prometheus.instant_query(args.prometheus_url, query)
    except Exception as exc:
        print(f"Warning: could not check Prometheus for an earlier run under this id: {exc}", flush=True)
        return
    if found:
        sys.exit(
            f"Prometheus already has series for run {args.run_id!r}, case "
            f"{args.test_case!r}; their numbers would be added to this run's. "
            "Use another --run-id."
        )


def write_run_json(
    args: argparse.Namespace, started: float, ended: float, status: int, failures: dict | None
) -> dict:
    """Write run.json into --report-dir and return what it holds: how the run
    went, followed by what run_locust_job.sh passed in BENCHMARK_RUN_INFO (the
    resolved case, the cluster, the WorkerPool, the images by digest and the
    commits). write_reports uploads it with the other files."""
    passed: dict = {}
    raw = os.environ.get("BENCHMARK_RUN_INFO", "")
    if raw:
        try:
            passed = json.loads(base64.b64decode(raw))
        except (ValueError, TypeError) as exc:
            passed = {"run_info_error": f"cannot decode BENCHMARK_RUN_INFO: {exc}"}
    else:
        passed = {"run_info_error": "BENCHMARK_RUN_INFO is unset"}

    def iso(t: float) -> str:
        return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat()

    info = {
        "run_id": args.run_id,
        "test_case": args.test_case,
        "status": status,
        "started": iso(started),
        "ended": iso(ended),
        "users": args.users,
        "spawn_rate": args.spawn_rate,
        "duration": args.duration,
        "user_class": user_class(args.file),
        "max_failure_ratio": args.max_failure_ratio,
        "failures": failures,
        "worker_flags": args.worker_flags,
        **passed,
    }
    path = Path(args.report_dir) / RUN_JSON
    path.write_text(json.dumps(info, indent=2) + "\n")
    print(f"reports: wrote {path}", flush=True)
    return info


def write_readme(args: argparse.Namespace, info: dict) -> None:
    """Write README.md into --report-dir: run.json summarized for a reader,
    and what each file beside it holds. Skipped, and said so, if it cannot be
    rendered; run.json is the record either way."""
    try:
        from reports import readme

        text = readme.render(info)
    except Exception as exc:
        print(f"reports: {README} skipped ({type(exc).__name__}: {exc})", flush=True)
        return
    path = Path(args.report_dir) / README
    path.write_text(text)
    print(f"reports: wrote {path}", flush=True)


def write_reports(args: argparse.Namespace, started: float, ended: float) -> bool:
    """Write the selected reports and upload them with locust's own CSVs, and
    say whether every file that should have uploaded did.

    A Prometheus report that cannot be produced does not change the result:
    it is a by-product of the run. An upload that fails does, because the
    bucket is the only record that outlives the pod, and run_locust_job.sh
    would otherwise report success and download nothing."""
    if not args.run_id:
        print("reports: --run-id is unset, so there is nothing to query on; skipping", flush=True)
        return True

    # Locust's files are on disk already, whatever the Prometheus reports do.
    collected = locust_csvs(args)
    for path in collected:
        print(f"reports: locust wrote {path} ({path.stat().st_size} bytes)", flush=True)
    for name in (RUN_JSON, README):
        path = Path(args.report_dir) / name
        if path.exists():
            collected.append(path)

    if args.report:
        collected += generate_reports(args, started)

    if collected and args.report_bucket:
        return upload_reports(collected, args)
    return True


def generate_reports(args: argparse.Namespace, started: float) -> list[Path]:
    """The Prometheus-derived reports, or an empty list if they cannot be
    produced. Each failure is named; none of them is fatal."""
    print(f"Waiting {REPORT_SETTLE_SECS}s for the run's last scrape...", flush=True)
    time.sleep(REPORT_SETTLE_SECS)

    try:
        import reports

        ctx = reports.RunContext(
            run_id=args.run_id,
            test_case=args.test_case,
            users=args.users,
            spawn_rate=args.spawn_rate,
            duration=args.duration,
            workload=args.worker_flags.get("workload-name", ""),
            template=args.worker_flags.get("workload-template", ""),
            user_class=user_class(args.file),
            started=started,
            ended=time.time(),
            prometheus_url=args.prometheus_url,
        )
        results = reports.generate(args.report, ctx, Path(args.report_dir))
    except Exception as exc:
        print(f"reports: skipped ({type(exc).__name__}: {exc})", flush=True)
        return []

    written = []
    for name, path, error in results:
        if error:
            print(f"reports: {name} failed: {error}", flush=True)
        else:
            written.append(path)
            print(f"reports: wrote {path} ({path.stat().st_size} bytes)", flush=True)
    return written


def upload_reports(paths: list[Path], args: argparse.Namespace) -> bool:
    try:
        import reports

        results = reports.upload(
            paths,
            args.report_bucket,
            f"{args.run_id}/{args.test_case}",
            Path(args.report_dir),
            args.report_prefix,
        )
    except ImportError as exc:
        print(f"reports: upload failed ({exc})", flush=True)
        return False
    except reports.UploadError as exc:
        print(f"reports: upload failed ({exc})", flush=True)
        return False
    ok = True
    for path, uri, error in results:
        if error:
            print(f"reports: upload of {path.name} failed: {error}", flush=True)
            ok = False
        else:
            print(f"reports: uploaded {uri}", flush=True)
    return ok


def main() -> None:
    args = parse_args()
    log_run_config(args)
    refuse_reused_run(args)
    started = time.time()
    status = run(args)
    ended = time.time()
    failures = count_failures(args)
    if status == 0 and not within_limits(failures, args.max_failure_ratio):
        status = TOO_MANY_FAILURES_EXIT
    info = write_run_json(args, started, ended, status, failures)
    write_readme(args, info)
    if not write_reports(args, started, ended) and status == 0:
        print("The reports did not all upload, so the run is failed.", flush=True)
        status = UPLOAD_FAILED_EXIT
    sys.exit(status)


if __name__ == "__main__":
    main()
