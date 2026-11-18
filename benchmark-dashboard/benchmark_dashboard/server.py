"""A local dashboard over the benchmark reports' BigQuery views and a local reports directory.

Runs as you, with your Application Default Credentials
(gcloud auth application-default login). Serves a fixed set of
parameterized queries; nothing takes SQL from the browser. The local
reports directory (default: the repo's reports/) shows up as the
"local" dataset, read straight from the files.
"""

import argparse
import re
import threading
import time
import webbrowser
from datetime import date, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.cloud import bigquery

from .local import LocalReports, NotFound

STATIC = Path(__file__).parent / "static"
LOCAL = "local"
DEFAULT_REPORTS = Path(__file__).resolve().parents[2] / "reports"
# A dataset is offered when it has the views the charts read.
REQUIRED_VIEWS = {"latency_chart", "pool_chart", "actors_chart", "locust_stats", "locust_steady_window"}
CACHE_TTL_SECS = 600
PERCENTILES = ["p50_ms", "p66_ms", "p75_ms", "p80_ms", "p90_ms", "p95_ms", "p98_ms", "p99_ms", "p99_9_ms", "p99_99_ms", "p100_ms"]

app = FastAPI(title="benchmark-dashboard")
state: dict = {"project": None, "client": None, "local": None, "bq": True, "bq_error": None}
_cache: dict = {}
_cache_lock = threading.Lock()


def client() -> bigquery.Client:
    if state["client"] is None:
        state["client"] = bigquery.Client(project=state["project"])
    return state["client"]


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def cached(key, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECS:
            return hit[1]
    value = fn()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def query(sql: str, **params) -> list[dict]:
    def run():
        qp = [bigquery.ScalarQueryParameter(k, "STRING", v) for k, v in params.items()]
        job = client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=qp))
        return [{k: _jsonable(v) for k, v in row.items()} for row in job.result()]

    return cached((sql, tuple(sorted(params.items()))), run)


def bq_datasets() -> list[dict]:
    def run():
        out = []
        for ds in client().list_datasets():
            tables = {t.table_id for t in client().list_tables(ds.dataset_id)}
            if REQUIRED_VIEWS <= tables:
                out.append({"id": ds.dataset_id, "sandbox": "sandbox_benchmark_comparison" in tables})
        return out

    return cached(("datasets",), run)


def datasets() -> list[dict]:
    """The BigQuery datasets, then the local one. BigQuery failing (no credentials,
    offline) leaves the local one usable."""
    out = []
    if state["bq"]:
        try:
            out = bq_datasets()
            state["bq_error"] = None
        except Exception as e:  # noqa: BLE001 - shown in the UI
            state["bq_error"] = f"{type(e).__name__}: {e}"
    if state["local"] is not None:
        out = out + [{"id": LOCAL, "sandbox": True, "local": str(state["local"].root)}]
    return out


def local_call(fn, *args, **kw):
    try:
        return cached(("local", fn.__name__, args, tuple(sorted(kw.items()))), lambda: fn(*args, **kw))
    except NotFound as e:
        raise HTTPException(404, f"unknown local case {e}")


def table(dataset: str, view: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", dataset) or dataset not in {d["id"] for d in datasets()}:
        raise HTTPException(404, f"unknown dataset {dataset!r}")
    return f"`{state['project']}.{dataset}.{view}`"


@app.get("/api/config")
def api_config():
    return {"project": state["project"] if state["bq"] else None, "datasets": datasets(),
            "percentiles": PERCENTILES, "bq_error": state["bq_error"]}


@app.get("/api/runs")
def api_runs(dataset: str):
    """Every case of every run: locust ones from locust_stats, and the sandbox-benchmark ones."""
    if dataset == LOCAL and state["local"]:
        return group_runs(local_call(state["local"].runs))
    sql = f"""
      WITH s AS (
        SELECT run_id, test_case, ANY_VALUE(scenario) AS scenario,
          ARRAY_AGG(DISTINCT name IGNORE NULLS ORDER BY name) AS requests
        FROM {table(dataset, "locust_stats")}
        WHERE name != 'Aggregated'
        GROUP BY run_id, test_case
      )
      SELECT 'locust' AS kind, s.*, w.start, w.`end`
      FROM s LEFT JOIN {table(dataset, "locust_steady_window")} AS w USING (run_id, test_case)
    """
    rows = query(sql)
    if any(d["id"] == dataset and d["sandbox"] for d in datasets()):
        rows = rows + query(f"""
          SELECT 'sandbox' AS kind, run_id, test_case, ANY_VALUE(scenario) AS scenario,
            ARRAY_AGG(DISTINCT provider IGNORE NULLS ORDER BY provider) AS requests,
            MIN(started) AS start, MAX(ended) AS `end`
          FROM {table(dataset, "sandbox_benchmark_run")}
          GROUP BY run_id, test_case
        """)
    return group_runs(rows)


def group_runs(rows: list[dict]) -> list[dict]:
    runs: dict[str, dict] = {}
    for r in rows:
        run = runs.setdefault(r["run_id"], {"run_id": r["run_id"], "cases": []})
        run["cases"].append(r)
    for run in runs.values():
        run["cases"].sort(key=lambda c: c["start"] or "")
        starts = [c["start"] for c in run["cases"] if c["start"]]
        run["start"] = min(starts) if starts else None
    # run-YYYYMMDDtHHMMSSz sorts by time; a named run without a start goes last.
    return sorted(runs.values(), key=lambda r: (r["start"] or "", r["run_id"]), reverse=True)


@app.get("/api/latency")
def api_latency(dataset: str, run_id: str, test_case: str):
    if dataset == LOCAL and state["local"]:
        return local_call(state["local"].latency, run_id, test_case)
    cols = ", ".join(PERCENTILES)
    return query(f"""
      SELECT request, elapsed_secs, steady, user_count, {cols},
        requests_per_sec, successful_per_sec, failures_per_sec
      FROM {table(dataset, "latency_chart")}
      WHERE run_id = @run_id AND test_case = @test_case
      ORDER BY request, elapsed_secs
    """, run_id=run_id, test_case=test_case)


@app.get("/api/pool")
def api_pool(dataset: str, run_id: str, test_case: str, per_node: bool = Query(False)):
    if dataset == LOCAL and state["local"]:
        return local_call(state["local"].pool, run_id, test_case, per_node=per_node)
    if per_node:
        sql = f"""
          SELECT node, elapsed_secs, cpu_percent, memory_percent
          FROM {table(dataset, "pool_chart")}
          WHERE run_id = @run_id AND test_case = @test_case
          ORDER BY node, elapsed_secs
        """
    else:
        sql = f"""
          SELECT elapsed_secs, COUNT(DISTINCT node) AS nodes,
            AVG(cpu_percent) AS cpu_avg, MAX(cpu_percent) AS cpu_max,
            AVG(memory_percent) AS memory_avg, MAX(memory_percent) AS memory_max
          FROM {table(dataset, "pool_chart")}
          WHERE run_id = @run_id AND test_case = @test_case
          GROUP BY elapsed_secs
          ORDER BY elapsed_secs
        """
    return query(sql, run_id=run_id, test_case=test_case)


@app.get("/api/actors")
def api_actors(dataset: str, run_id: str, test_case: str):
    if dataset == LOCAL and state["local"]:
        return local_call(state["local"].actors, run_id, test_case)
    return query(f"""
      SELECT elapsed_secs, steady, actors, active_actors, idle_actors, density, active_density
      FROM {table(dataset, "actors_chart")}
      WHERE run_id = @run_id AND test_case = @test_case
      ORDER BY elapsed_secs
    """, run_id=run_id, test_case=test_case)


@app.get("/api/summary")
def api_summary(dataset: str, run_id: str, test_case: str, steady: bool = True):
    if dataset == LOCAL and state["local"]:
        return local_call(state["local"].summary, run_id, test_case, steady=steady)
    view = "locust_stats_steady" if steady else "locust_stats"
    return query(f"""
      SELECT * EXCEPT (run_id, test_case, scenario)
      FROM {table(dataset, view)}
      WHERE run_id = @run_id AND test_case = @test_case
      ORDER BY name = 'Aggregated', name
    """, run_id=run_id, test_case=test_case)


@app.get("/api/sandbox")
def api_sandbox(dataset: str, run_id: str, test_case: str):
    if dataset == LOCAL and state["local"]:
        return local_call(state["local"].sandbox, run_id, test_case)
    return query(f"""
      SELECT test, provider, metric, mean_ms, stddev_ms, runs, test_runs, failed_runs
      FROM {table(dataset, "sandbox_benchmark_comparison")}
      WHERE run_id = @run_id AND test_case = @test_case
      ORDER BY test, metric, provider
    """, run_id=run_id, test_case=test_case)


@app.post("/api/refresh")
def api_refresh():
    with _cache_lock:
        _cache.clear()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", default="akvelon-gke-aieco")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--reports", type=Path, default=DEFAULT_REPORTS,
                   help=f"local reports directory, <run_id>/<test_case>/... (default {DEFAULT_REPORTS})")
    p.add_argument("--no-local", action="store_true", help="don't offer the local reports directory")
    p.add_argument("--no-bq", action="store_true", help="local reports only; don't touch BigQuery")
    args = p.parse_args()
    state["project"] = args.project
    state["bq"] = not args.no_bq
    if not args.no_local:
        state["local"] = LocalReports(args.reports.resolve())
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [f"http://{args.host}:{args.port}/"]).start()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
