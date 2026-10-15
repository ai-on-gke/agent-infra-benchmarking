module "reports" {
  source = "../reports"
  count  = var.reports_bucket == null ? 0 : 1

  project_id         = var.project_id
  dataset_id         = replace(var.default_resource_name, "-", "_")
  bucket             = var.reports_bucket
  prefix             = var.reports_prefix
  writer_project_ids = var.reports_writer_project_ids
}

# Where the reports' resources were before they had a module of their own.
moved {
  from = google_project_service.services["bigquery.googleapis.com"]
  to   = module.reports[0].google_project_service.bigquery
}

moved {
  from = google_storage_bucket_iam_member.reports_runner[0]
  to   = module.reports[0].google_storage_bucket_iam_member.reports_runner
}

moved {
  from = google_bigquery_dataset.reports[0]
  to   = module.reports[0].google_bigquery_dataset.reports
}

moved {
  from = google_bigquery_table.steady_window[0]
  to   = module.reports[0].google_bigquery_table.steady_window
}

moved {
  from = google_bigquery_table.report_files
  to   = module.reports[0].google_bigquery_table.report_files
}

moved {
  from = google_bigquery_table.charts
  to   = module.reports[0].google_bigquery_table.charts
}

moved {
  from = google_bigquery_table.reports
  to   = module.reports[0].google_bigquery_table.reports
}

moved {
  from = google_bigquery_table.sandbox_benchmark_files
  to   = module.reports[0].google_bigquery_table.sandbox_benchmark_files
}

moved {
  from = google_bigquery_table.sandbox_benchmark
  to   = module.reports[0].google_bigquery_table.sandbox_benchmark
}
