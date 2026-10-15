locals {
  # locust's PERCENTILES_TO_REPORT.
  percentiles = [for p in ["50", "66", "75", "80", "90", "95", "98", "99", "99_9", "99_99", "100"] : { name = "p${p}_ms", type = "FLOAT64" }]
  history_columns = [
    { name = "unix_secs", type = "INT64" },
    { name = "user_count", type = "INT64" },
    { name = "type", type = "STRING" },
    { name = "name", type = "STRING" },
  ]
  stats_columns = concat([
    { name = "type", type = "STRING" },
    { name = "name", type = "STRING" },
    { name = "request_count", type = "INT64" },
    { name = "failure_count", type = "INT64" },
    { name = "median_ms", type = "FLOAT64" },
    { name = "avg_ms", type = "FLOAT64" },
    { name = "min_ms", type = "FLOAT64" },
    { name = "max_ms", type = "FLOAT64" },
    { name = "avg_content_size", type = "FLOAT64" },
    { name = "requests_per_sec", type = "FLOAT64" },
    { name = "failures_per_sec", type = "FLOAT64" },
  ], local.percentiles)

  # Each report CSV's columns, in the file's order: the external tables take
  # them by position, as the headers are no column names. Those starting with
  # unix_secs are a row a second.
  report_csvs = {
    locust_stats_history = concat(local.history_columns, [
      { name = "requests_per_sec", type = "FLOAT64" },
      { name = "failures_per_sec", type = "FLOAT64" },
      ], local.percentiles, [
      { name = "total_request_count", type = "INT64" },
      { name = "total_failure_count", type = "INT64" },
      { name = "total_median_ms", type = "FLOAT64" },
      { name = "total_avg_ms", type = "FLOAT64" },
      { name = "total_min_ms", type = "FLOAT64" },
      { name = "total_max_ms", type = "FLOAT64" },
      { name = "total_avg_content_size", type = "FLOAT64" },
    ])
    custom_stats_history   = concat(local.history_columns, [{ name = "value", type = "FLOAT64" }])
    cluster_vitals_history = concat(local.history_columns, [{ name = "value", type = "FLOAT64" }, { name = "pool", type = "STRING" }])
    locust_stats           = local.stats_columns
    locust_stats_steady    = local.stats_columns
  }
  steady_window_columns = [{ name = "start", type = "TIMESTAMP" }, { name = "end", type = "TIMESTAMP" }]

  report_files = merge(
    { for name, columns in local.report_csvs : name => { file = "${name}.csv", format = "CSV", columns = columns } },
    { locust_steady_window = { file = "locust_steady_window.json", format = "NEWLINE_DELIMITED_JSON", columns = local.steady_window_columns } },
  )
}

resource "google_project_service" "bigquery" {
  project = var.project_id
  service = "bigquery.googleapis.com"
  # Other deployments in the project may rely on it.
  disable_on_destroy = false
}

data "google_project" "runner" {
  for_each = toset(concat([var.project_id], var.writer_project_ids))

  project_id = each.value
}

locals {
  bucket = var.bucket == null ? google_storage_bucket.reports[0].name : var.bucket
  # The benchmark-runner KSA of a project's Workload Identity pool, and so on
  # every cluster in it.
  runner_principal = {
    for id, project in data.google_project.runner :
    id => "principal://iam.googleapis.com/projects/${project.number}/locations/global/workloadIdentityPools/${id}.svc.id.goog/subject/ns/benchmarking/sa/benchmark-runner"
  }
  prefix_only = "resource.name.startsWith(\"projects/_/buckets/${local.bucket}/objects/${var.prefix}/\")"
}

# Each case's locust master and sandbox-benchmark comparator upload their
# reports as the benchmark-runner KSA, by Workload Identity. Whoever runs the
# scenario-runner uploads the Jobs' logs with their own credentials.
resource "google_storage_bucket_iam_member" "reports_runner" {
  bucket = local.bucket
  role   = "roles/storage.objectCreator"
  member = local.runner_principal[var.project_id]

  condition {
    title      = "reports-prefix-only"
    expression = local.prefix_only
  }
}

resource "google_storage_bucket_iam_member" "writer_projects_runner" {
  for_each = toset([for id in var.writer_project_ids : id if id != var.project_id])

  bucket = local.bucket
  role   = "roles/storage.objectCreator"
  member = local.runner_principal[each.value]

  condition {
    title      = "reports-prefix-only"
    expression = local.prefix_only
  }
}

resource "google_storage_bucket" "reports" {
  count = var.bucket == null ? 1 : 0

  project  = var.project_id
  name     = "${var.name}-reports"
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
}

data "google_storage_bucket" "existing" {
  count = var.bucket == null ? 0 : 1

  name = var.bucket
}

resource "google_bigquery_dataset" "reports" {
  project     = var.project_id
  dataset_id  = replace(var.name, "-", "_")
  description = "The reports in gs://${local.bucket}/${var.prefix}/, a view of each file across every run."
  # An external table reads only from a bucket in its dataset's location.
  location = lower(one(concat(google_storage_bucket.reports[*].location, data.google_storage_bucket.existing[*].location)))
  # Its tables only point at the bucket, so deleting them loses nothing.
  delete_contents_on_destroy = true

  depends_on = [google_project_service.bigquery]
}

# Every column a string, as locust writes N/A for a value it has none of yet;
# the views cast them.
resource "google_bigquery_table" "report_files" {
  for_each = local.report_files

  project             = var.project_id
  dataset_id          = google_bigquery_dataset.reports.dataset_id
  table_id            = "${each.key}_files"
  description         = "Every run's ${each.value.file}, as written. Query the ${each.key} view instead."
  deletion_protection = false

  external_data_configuration {
    autodetect    = false
    source_format = each.value.format
    source_uris   = ["gs://${local.bucket}/${var.prefix}/*/${each.value.file}"]
    schema        = jsonencode([for c in each.value.columns : { name = c.name, type = "STRING" }])
    # locust_steady_window.json's warm_up_secs.
    ignore_unknown_values = true

    dynamic "csv_options" {
      for_each = each.value.format == "CSV" ? [1] : []
      content {
        quote             = "\""
        skip_leading_rows = 1
        # cluster_vitals_history.csv of an older run has no pool.
        allow_jagged_rows = true
      }
    }
  }
}

resource "google_bigquery_table" "steady_window" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.reports.dataset_id
  table_id            = "locust_steady_window"
  description         = "Each run's steady-state window, by run_id and test_case."
  deletion_protection = false

  view {
    use_legacy_sql = false
    query = templatefile("${path.module}/report_view.sql.tftpl", {
      files         = "${var.project_id}.${google_bigquery_table.report_files["locust_steady_window"].dataset_id}.${google_bigquery_table.report_files["locust_steady_window"].table_id}"
      columns       = local.steady_window_columns
      steady_window = null
    })
  }
}

locals {
  # The panels of benchmark-charts, a view each, for charting in Looker Studio.
  # A row a second, its elapsed_secs counted from the repeat's locust start
  # in all three, so their charts line up.
  dataset_ref = "${var.project_id}.${google_bigquery_dataset.reports.dataset_id}"
  chart_start = <<-SQL
    start AS (
      SELECT run_id, test_case, MIN(time) AS first
      FROM `${local.dataset_ref}.locust_stats_history`
      GROUP BY run_id, test_case
    )
  SQL
  chart_views = {
    # Each request's latency percentiles and throughput. locust writes 0 for a
    # second whose window held no requests, which is null here so a line
    # breaks there rather than dropping to zero.
    latency_chart = <<-SQL
      WITH ${local.chart_start}
      SELECT
        run_id, test_case, scenario, name AS request, time,
        TIMESTAMP_DIFF(time, start.first, SECOND) AS elapsed_secs, steady, user_count,
        ${join(", ", [for p in local.percentiles : "NULLIF(${p.name}, 0) AS ${p.name}"])},
        requests_per_sec, failures_per_sec,
        requests_per_sec - failures_per_sec AS successful_per_sec
      FROM `${local.dataset_ref}.locust_stats_history`
      JOIN start USING (run_id, test_case)
      WHERE name != 'Aggregated'
    SQL
    # Each worker-pool node's CPU and memory utilization, by the last two parts
    # of its name, as GKE's gke-<cluster>-<pool>-<hash>-<id>.
    pool_chart = <<-SQL
      WITH ${local.chart_start}
      SELECT
        run_id, test_case, scenario, REGEXP_EXTRACT(type, r'([^-]+-[^-]+)$') AS node, time,
        TIMESTAMP_DIFF(time, ANY_VALUE(start.first), SECOND) AS elapsed_secs, steady,
        MAX(IF(name = 'CPUUtilizationPercent', value, NULL)) AS cpu_percent,
        MAX(IF(name = 'MemoryUtilizationPercent', value, NULL)) AS memory_percent
      FROM `${local.dataset_ref}.cluster_vitals_history`
      JOIN start USING (run_id, test_case)
      WHERE pool = 'worker'
      GROUP BY run_id, test_case, scenario, node, time, steady
    SQL
    # Every actor, which is locust's user count, and the worker's active and
    # idle ones.
    actors_chart = <<-SQL
      WITH ${local.chart_start}
      SELECT
        run_id, test_case, scenario, time,
        TIMESTAMP_DIFF(time, ANY_VALUE(start.first), SECOND) AS elapsed_secs, steady,
        ANY_VALUE(user_count) AS actors,
        MAX(IF(name = 'ActiveActors', value, NULL)) AS active_actors,
        MAX(IF(name = 'IdleActors', value, NULL)) AS idle_actors
      FROM `${local.dataset_ref}.custom_stats_history`
      JOIN start USING (run_id, test_case)
      GROUP BY run_id, test_case, scenario, time, steady
    SQL
  }
}

resource "google_bigquery_table" "charts" {
  for_each = local.chart_views

  project             = var.project_id
  dataset_id          = google_bigquery_dataset.reports.dataset_id
  table_id            = each.key
  description         = "benchmark-charts' ${trimsuffix(each.key, "_chart")} panel, a row a second, for Looker Studio."
  deletion_protection = false

  view {
    use_legacy_sql = false
    query          = each.value
  }

  depends_on = [google_bigquery_table.reports]
}

resource "google_bigquery_table" "reports" {
  for_each = local.report_csvs

  project             = var.project_id
  dataset_id          = google_bigquery_dataset.reports.dataset_id
  table_id            = each.key
  description         = "Every run's ${each.key}.csv, by run_id and test_case."
  deletion_protection = false

  view {
    use_legacy_sql = false
    query = templatefile("${path.module}/report_view.sql.tftpl", {
      files   = "${var.project_id}.${google_bigquery_table.report_files[each.key].dataset_id}.${google_bigquery_table.report_files[each.key].table_id}"
      columns = each.value
      steady_window = each.value[0].name != "unix_secs" ? null : (
        "${var.project_id}.${google_bigquery_table.steady_window.dataset_id}.${google_bigquery_table.steady_window.table_id}"
      )
    })
  }
}

locals {
  # The sandbox-benchmark comparator's newline-delimited JSON, as
  # benchmarking/sandbox-benchmark/image/run_comparator.py writes it under
  # <prefix>/<run-id>/<record>/<provider>/. Typed, as the runs' metrics are a
  # nested list. Each schema as JSON, as their shapes differ.
  sandbox_benchmark_files = {
    sandbox_benchmark_runs = jsonencode([
      { name = "provider", type = "STRING" },
      { name = "test", type = "STRING" },
      { name = "run", type = "INT64" },
      { name = "failed", type = "BOOL" },
      { name = "errors", type = "STRING", mode = "REPEATED" },
      {
        name   = "metrics", type = "RECORD", mode = "REPEATED",
        fields = [{ name = "name", type = "STRING" }, { name = "value_ms", type = "FLOAT64" }]
      },
    ])
    sandbox_benchmark_run = jsonencode(concat(
      [
        { name = "provider", type = "STRING" },
        { name = "status", type = "INT64" },
        { name = "started", type = "TIMESTAMP" },
        { name = "ended", type = "TIMESTAMP" },
        { name = "tests", type = "STRING", mode = "REPEATED" },
        { name = "runs", type = "INT64" },
        { name = "warmup_runs", type = "INT64" },
        { name = "total_runs", type = "INT64" },
        { name = "failed_runs", type = "INT64" },
        { name = "max_failure_ratio", type = "FLOAT64" },
      ],
      [for name in ["sandbox_class", "actor_memory", "comparator_image", "commit", "substrate_commit", "env_commit", "ai_sandbox_benchmark_commit"] :
      { name = name, type = "STRING" }],
    ))
  }
  # Each file's rows, with the run, record and case from its path,
  # <run-id>/<record>/<provider>/<file>. The views read the files tables
  # rather than one another, so they can be created at once.
  sandbox_benchmark_rows = {
    for name in ["sandbox_benchmark_run", "sandbox_benchmark_runs"] : name => <<-SQL
      SELECT
        REGEXP_EXTRACT(_FILE_NAME, r'/([^/]+)/[^/]+/[^/]+/[^/]+$') AS run_id,
        REGEXP_EXTRACT(_FILE_NAME, r'/([^/]+)/[^/]+/[^/]+$') AS test_case,
        REGEXP_REPLACE(REGEXP_EXTRACT(_FILE_NAME, r'/([^/]+)/[^/]+/[^/]+$'), r'-[0-9]+$', '') AS scenario,
        *
      FROM `${local.dataset_ref}.${name}_files`
    SQL
  }
  sandbox_benchmark_metrics = <<-SQL
    SELECT r.* EXCEPT (metrics), m.name AS metric, m.value_ms
    FROM (${local.sandbox_benchmark_rows.sandbox_benchmark_runs}) AS r, UNNEST(r.metrics) AS m
  SQL
  # The comparator's own total time adds these up.
  sandbox_benchmark_total = ["Workspace Creation", "Suspend", "Resume", "Dependency Installation", "Environment Setup", "Code Execution", "Cleanup"]
  sandbox_benchmark_views = {
    sandbox_benchmark_run = {
      description = "Each provider's run of each sandbox-benchmark record: how it went, and the commits."
      query       = local.sandbox_benchmark_rows.sandbox_benchmark_run
    }
    sandbox_benchmark_runs = {
      description = "Each run of each sandbox-benchmark test on each provider, with its errors and metrics."
      query       = local.sandbox_benchmark_rows.sandbox_benchmark_runs
    }
    sandbox_benchmark_metrics = {
      description = "Each metric value of each run of each sandbox-benchmark test on each provider, in ms."
      query       = local.sandbox_benchmark_metrics
    }
    # As the comparator's table: each run's mean of a metric, failed runs too,
    # averaged across the runs, with their population deviation; Total Time
    # the sum of a run's means of sandbox_benchmark_total.
    sandbox_benchmark_comparison = {
      description = "The sandbox-benchmark comparator's table: each metric of each test on each provider, per record."
      query       = <<-SQL
        WITH per_run AS (
          SELECT run_id, test_case, scenario, provider, test, run, metric, AVG(value_ms) AS run_mean
          FROM (${local.sandbox_benchmark_metrics})
          GROUP BY run_id, test_case, scenario, provider, test, run, metric
        ),
        with_total AS (
          SELECT * FROM per_run
          UNION ALL
          SELECT run_id, test_case, scenario, provider, test, run, 'Total Time', SUM(run_mean)
          FROM per_run
          WHERE metric IN (${join(", ", [for m in local.sandbox_benchmark_total : "'${m}'"])})
          GROUP BY run_id, test_case, scenario, provider, test, run
        ),
        tests AS (
          SELECT run_id, test_case, provider, test, COUNT(*) AS test_runs, COUNTIF(failed) AS failed_runs
          FROM (${local.sandbox_benchmark_rows.sandbox_benchmark_runs})
          GROUP BY run_id, test_case, provider, test
        )
        SELECT
          run_id, test_case, scenario, test, provider, metric,
          AVG(run_mean) AS mean_ms, STDDEV_POP(run_mean) AS stddev_ms, COUNT(*) AS runs,
          ANY_VALUE(test_runs) AS test_runs, ANY_VALUE(failed_runs) AS failed_runs
        FROM with_total
        JOIN tests USING (run_id, test_case, provider, test)
        GROUP BY run_id, test_case, scenario, test, provider, metric
      SQL
    }
  }
}

resource "google_bigquery_table" "sandbox_benchmark_files" {
  for_each = local.sandbox_benchmark_files

  project             = var.project_id
  dataset_id          = google_bigquery_dataset.reports.dataset_id
  table_id            = "${each.key}_files"
  description         = "Every sandbox-benchmark run's ${each.key}.jsonl, as written. Query the ${each.key} view instead."
  deletion_protection = false

  external_data_configuration {
    autodetect            = false
    source_format         = "NEWLINE_DELIMITED_JSON"
    source_uris           = ["gs://${local.bucket}/${var.prefix}/*/${each.key}.jsonl"]
    schema                = each.value
    ignore_unknown_values = true
  }
}

resource "google_bigquery_table" "sandbox_benchmark" {
  for_each = local.sandbox_benchmark_views

  project             = var.project_id
  dataset_id          = google_bigquery_dataset.reports.dataset_id
  table_id            = each.key
  description         = each.value.description
  deletion_protection = false

  view {
    use_legacy_sql = false
    query          = each.value.query
  }

  depends_on = [google_bigquery_table.sandbox_benchmark_files]
}
