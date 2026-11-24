# benchmark-dashboard

Local viewer for the benchmark reports' BigQuery views (latency_chart, pool_chart,
actors_chart, locust_stats_steady, sandbox_benchmark_comparison).

    gcloud auth application-default login   # once
    cd benchmark-dashboard && uv run benchmark-dashboard   # http://127.0.0.1:8080

Flags: --project (default akvelon-gke-aieco), --port, --host, --no-browser,
--reports DIR (local reports, default ../reports), --no-local, --no-bq.

- The local reports directory shows up as the "local reports (files)" dataset,
  read straight from <run_id>/<test_case>/ (locust CSVs) and
  <run_id>/<test_case>/<provider>/ (sandbox-benchmark JSONL), computed like the
  BigQuery views. Local cases can be overlaid with BigQuery ones. A sandbox
  provider is its directory name (substrate-gvisor, substrate-microvm, ...).
  Run and case directory names may only have letters, digits, `.`, `_` and `-`,
  and must not start with a dot; others are skipped, with a warning on the
  console. A case whose files are damaged (an empty steady window, a truncated
  JSONL line) is read as far as it goes, and never hides the other cases.
- If BigQuery is unreachable (no ADC), the local dataset still works; the
  header shows the error. Use --no-bq to skip BigQuery entirely.

- Dataset picker lists every dataset in the project that has the chart views
  (substrate_bench_auto, substrate_bench_staging, ...).
- Click cases to add them (up to 8, across datasets); several overlay for comparison.
- Selection and controls live in the URL hash, so a view can be bookmarked.
- Queries are cached for 10 min server-side; "Refresh data" clears the cache.
