"""The same rows as the BigQuery views, read from a local reports directory.

Mirrors automation-infra/terraform/modules/reports (report_view.sql.tftpl and the
chart / sandbox_benchmark views) over the files as the runs write them:

    <root>/<run_id>/<test_case>/locust_stats_history.csv, ...        (locust cases)
    <root>/<run_id>/<test_case>/<provider>/sandbox_benchmark_*.jsonl  (sandbox cases)

A sandbox case's provider is its directory name (substrate-gvisor, e2b, ...), not
the provider field, so two substrate variants in one case stay apart.
"""

import csv
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PCT_COLS = ["p50_ms", "p66_ms", "p75_ms", "p80_ms", "p90_ms", "p95_ms", "p98_ms", "p99_ms", "p99_9_ms", "p99_99_ms", "p100_ms"]
STATS_COLS = ["type", "name", "request_count", "failure_count", "median_ms", "avg_ms", "min_ms", "max_ms",
              "avg_content_size", "requests_per_sec", "failures_per_sec", *PCT_COLS]
INT_COLS = {"request_count", "failure_count", "user_count"}
SANDBOX_TOTAL = ["Workspace Creation", "Suspend", "Resume", "Dependency Installation", "Environment Setup", "Code Execution", "Cleanup"]
SAFE = re.compile(r"[A-Za-z0-9._-]+")


class NotFound(Exception):
    pass


def _num(v, as_int=False):
    """SAFE_CAST: N/A, empty and junk become None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return int(f) if as_int else f


def _ts(iso):
    return datetime.fromisoformat(iso).timestamp() if iso else None


def _iso(secs):
    return datetime.fromtimestamp(secs, timezone.utc).isoformat() if secs is not None else None


def _rows(path: Path):
    """A CSV's data rows, positionally (as the external tables read them)."""
    if not path.exists():
        return []
    with path.open(newline="") as f:
        r = csv.reader(f)
        next(r, None)
        return [row for row in r if row]


class LocalReports:
    def __init__(self, root: Path):
        self.root = root

    # ---------- discovery ----------
    def case_dir(self, run_id: str, test_case: str) -> Path:
        if not (SAFE.fullmatch(run_id) and SAFE.fullmatch(test_case)):
            raise NotFound(f"{run_id}/{test_case}")
        d = self.root / run_id / test_case
        if not d.is_dir():
            raise NotFound(f"{run_id}/{test_case}")
        return d

    def runs(self) -> list[dict]:
        rows = []
        if not self.root.is_dir():
            return rows
        for run in sorted(p for p in self.root.iterdir() if p.is_dir()):
            for case in sorted(p for p in run.iterdir() if p.is_dir()):
                scenario = re.sub(r"-[0-9]+$", "", case.name)
                if (case / "locust_stats_history.csv").exists():
                    w = self._window(case)
                    names = sorted({r[3] for r in _rows(case / "locust_stats_history.csv") if len(r) > 3 and r[3] != "Aggregated"})
                    rows.append({"kind": "locust", "run_id": run.name, "test_case": case.name, "scenario": scenario,
                                 "requests": names, "start": _iso(w[0]) if w else None, "end": _iso(w[1]) if w else None})
                provs = self._providers(case)
                if provs:
                    starts, ends = [], []
                    for p in provs:
                        for r in self._jsonl(p / "sandbox_benchmark_run.jsonl"):
                            starts.append(r.get("started")); ends.append(r.get("ended"))
                    starts = [s for s in starts if s]; ends = [e for e in ends if e]
                    rows.append({"kind": "sandbox", "run_id": run.name, "test_case": case.name, "scenario": scenario,
                                 "requests": [p.name for p in provs],
                                 "start": min(starts, key=_ts) if starts else None, "end": max(ends, key=_ts) if ends else None})
        return rows

    @staticmethod
    def _providers(case: Path) -> list[Path]:
        return sorted(p for p in case.iterdir() if p.is_dir() and (p / "sandbox_benchmark_runs.jsonl").exists())

    @staticmethod
    def _jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    @staticmethod
    def _window(case: Path):
        p = case / "locust_steady_window.json"
        if not p.exists():
            return None
        w = json.loads(p.read_text().splitlines()[0])
        return (_ts(w.get("start")), _ts(w.get("end"))) if w.get("start") and w.get("end") else None

    def _timebase(self, case: Path):
        """chart_start's first second (locust_stats_history, every row) and the steady test."""
        times = [_num(r[0], True) for r in _rows(case / "locust_stats_history.csv")]
        times = [t for t in times if t is not None]
        first = min(times) if times else 0
        w = self._window(case)
        steady = (lambda t: w[0] <= t <= w[1]) if w else (lambda t: None)
        return first, steady

    # ---------- views ----------
    def latency(self, run_id, test_case):
        case = self.case_dir(run_id, test_case)
        first, steady = self._timebase(case)
        out = []
        for r in _rows(case / "locust_stats_history.csv"):
            if len(r) < 17 or r[3] == "Aggregated":
                continue
            t = _num(r[0], True)
            rps, fps = _num(r[4]), _num(r[5])
            row = {"request": r[3], "elapsed_secs": t - first, "steady": steady(t), "user_count": _num(r[1], True)}
            for i, p in enumerate(PCT_COLS):
                v = _num(r[6 + i])
                row[p] = v or None  # NULLIF(p, 0)
            row.update(requests_per_sec=rps, failures_per_sec=fps,
                       successful_per_sec=None if rps is None or fps is None else rps - fps)
            out.append(row)
        out.sort(key=lambda x: (x["request"], x["elapsed_secs"]))
        return out

    def _pool_rows(self, case):
        first, steady = self._timebase(case)
        by = defaultdict(dict)
        for r in _rows(case / "cluster_vitals_history.csv"):
            if len(r) < 6 or r[5] != "worker" or r[3] not in ("CPUUtilizationPercent", "MemoryUtilizationPercent"):
                continue
            t = _num(r[0], True)
            m = re.search(r"([^-]+-[^-]+)$", r[2])
            node = m.group(1) if m else r[2]
            by[(node, t)]["cpu_percent" if r[3].startswith("CPU") else "memory_percent"] = _num(r[4])
        return [{"node": n, "elapsed_secs": t - first, "steady": steady(t), **v} for (n, t), v in by.items()]

    def pool(self, run_id, test_case, per_node=False):
        rows = self._pool_rows(self.case_dir(run_id, test_case))
        if per_node:
            rows.sort(key=lambda x: (x["node"], x["elapsed_secs"]))
            return [{k: r.get(k) for k in ("node", "elapsed_secs", "cpu_percent", "memory_percent")} for r in rows]
        by = defaultdict(list)
        for r in rows:
            by[r["elapsed_secs"]].append(r)
        out = []
        for t in sorted(by):
            g = by[t]
            cpu = [r["cpu_percent"] for r in g if r.get("cpu_percent") is not None]
            mem = [r["memory_percent"] for r in g if r.get("memory_percent") is not None]
            out.append({"elapsed_secs": t, "nodes": len({r["node"] for r in g}),
                        "cpu_avg": sum(cpu) / len(cpu) if cpu else None, "cpu_max": max(cpu) if cpu else None,
                        "memory_avg": sum(mem) / len(mem) if mem else None, "memory_max": max(mem) if mem else None})
        return out

    def actors(self, run_id, test_case):
        case = self.case_dir(run_id, test_case)
        first, steady = self._timebase(case)
        names = {"ActiveActors": "active_actors", "IdleActors": "idle_actors", "Density": "density", "ActiveDensity": "active_density"}
        by = {}
        for r in _rows(case / "custom_stats_history.csv"):
            if len(r) < 5:
                continue
            t = _num(r[0], True)
            row = by.setdefault(t, {"elapsed_secs": t - first, "steady": steady(t), "actors": _num(r[1], True),
                                    **{v: None for v in names.values()}})
            if r[3] in names:
                row[names[r[3]]] = _num(r[4])
        return [by[t] for t in sorted(by)]

    def summary(self, run_id, test_case, steady=True):
        case = self.case_dir(run_id, test_case)
        out = []
        for r in _rows(case / ("locust_stats_steady.csv" if steady else "locust_stats.csv")):
            row = {}
            for i, c in enumerate(STATS_COLS):
                v = r[i] if i < len(r) else None
                row[c] = v if c in ("type", "name") else _num(v, c in INT_COLS)
            out.append(row)
        out.sort(key=lambda x: (x["name"] == "Aggregated", x["name"] or ""))
        return out

    def sandbox(self, run_id, test_case):
        """sandbox_benchmark_comparison: per-run means, averaged, population stddev; Total Time summed."""
        case = self.case_dir(run_id, test_case)
        out = []
        for pdir in self._providers(case):
            provider = pdir.name
            per_run = defaultdict(list)  # (test, run, metric) -> values
            tests = defaultdict(lambda: [0, 0])
            for r in self._jsonl(pdir / "sandbox_benchmark_runs.jsonl"):
                tests[r["test"]][0] += 1
                tests[r["test"]][1] += bool(r.get("failed"))
                for m in r.get("metrics") or []:
                    if m.get("value_ms") is not None:
                        per_run[(r["test"], r["run"], m["name"])].append(m["value_ms"])
            means = {k: sum(v) / len(v) for k, v in per_run.items()}
            totals = defaultdict(float)
            has_total = set()
            for (test, run, metric), v in means.items():
                if metric in SANDBOX_TOTAL:
                    totals[(test, run)] += v
                    has_total.add((test, run))
            for (test, run) in has_total:
                means[(test, run, "Total Time")] = totals[(test, run)]
            by_metric = defaultdict(list)
            for (test, run, metric), v in means.items():
                by_metric[(test, metric)].append(v)
            for (test, metric), vals in by_metric.items():
                mean = sum(vals) / len(vals)
                sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
                out.append({"test": test, "provider": provider, "metric": metric, "mean_ms": mean, "stddev_ms": sd,
                            "runs": len(vals), "test_runs": tests[test][0], "failed_runs": tests[test][1]})
        out.sort(key=lambda x: (x["test"], x["metric"], x["provider"]))
        return out
