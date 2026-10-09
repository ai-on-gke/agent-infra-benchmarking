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

  report_files = var.reports_bucket == null ? {} : merge(
    { for name, columns in local.report_csvs : name => { file = "${name}.csv", format = "CSV", columns = columns } },
    { locust_steady_window = { file = "locust_steady_window.json", format = "NEWLINE_DELIMITED_JSON", columns = local.steady_window_columns } },
  )
}

data "google_project" "this" {
  project_id = var.project_id
}

# Each case's locust master uploads its reports as the benchmark-runner KSA of
# benchmarking/locust/manifests/, by Workload Identity. The pool is the
# project's, so this is that KSA on every cluster in it. The job's own account
# uploads the Jobs' logs with its storage.admin.
resource "google_storage_bucket_iam_member" "reports_runner" {
  count = var.reports_bucket == null ? 0 : 1

  bucket = var.reports_bucket
  role   = "roles/storage.objectCreator"
  member = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/benchmarking/sa/benchmark-runner"

  condition {
    title      = "reports-prefix-only"
    expression = "resource.name.startsWith(\"projects/_/buckets/${var.reports_bucket}/objects/${var.reports_prefix}/\")"
  }
}

data "google_storage_bucket" "reports" {
  count = var.reports_bucket == null ? 0 : 1

  name = var.reports_bucket
}

resource "google_bigquery_dataset" "reports" {
  count = var.reports_bucket == null ? 0 : 1

  project     = var.project_id
  dataset_id  = replace(var.default_resource_name, "-", "_")
  description = "The reports in gs://${var.reports_bucket}/${var.reports_prefix}/, a view of each file across every run."
  # An external table reads only from a bucket in its dataset's location.
  location = lower(data.google_storage_bucket.reports[0].location)
  # Its tables only point at the bucket, so deleting them loses nothing.
  delete_contents_on_destroy = true

  depends_on = [google_project_service.services]
}

# Every column a string, as locust writes N/A for a value it has none of yet;
# the views cast them.
resource "google_bigquery_table" "report_files" {
  for_each = local.report_files

  project             = var.project_id
  dataset_id          = google_bigquery_dataset.reports[0].dataset_id
  table_id            = "${each.key}_files"
  description         = "Every run's ${each.value.file}, as written. Query the ${each.key} view instead."
  deletion_protection = false

  external_data_configuration {
    autodetect    = false
    source_format = each.value.format
    source_uris   = ["gs://${var.reports_bucket}/${var.reports_prefix}/*/${each.value.file}"]
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
  count = var.reports_bucket == null ? 0 : 1

  project             = var.project_id
  dataset_id          = google_bigquery_dataset.reports[0].dataset_id
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
  dataset_ref = var.reports_bucket == null ? "" : "${var.project_id}.${google_bigquery_dataset.reports[0].dataset_id}"
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
  for_each = var.reports_bucket == null ? {} : local.chart_views

  project             = var.project_id
  dataset_id          = google_bigquery_dataset.reports[0].dataset_id
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
  for_each = var.reports_bucket == null ? {} : local.report_csvs

  project             = var.project_id
  dataset_id          = google_bigquery_dataset.reports[0].dataset_id
  table_id            = each.key
  description         = "Every run's ${each.key}.csv, by run_id and test_case."
  deletion_protection = false

  view {
    use_legacy_sql = false
    query = templatefile("${path.module}/report_view.sql.tftpl", {
      files   = "${var.project_id}.${google_bigquery_table.report_files[each.key].dataset_id}.${google_bigquery_table.report_files[each.key].table_id}"
      columns = each.value
      steady_window = each.value[0].name != "unix_secs" ? null : (
        "${var.project_id}.${google_bigquery_table.steady_window[0].dataset_id}.${google_bigquery_table.steady_window[0].table_id}"
      )
    })
  }
}
