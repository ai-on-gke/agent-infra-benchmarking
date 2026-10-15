output "dataset" {
  description = "BigQuery dataset of the reports' views, as project.dataset."
  value       = "${google_bigquery_dataset.reports.project}.${google_bigquery_dataset.reports.dataset_id}"
}

output "run_args" {
  description = "The scenario-runner's arguments that send a run's reports here."
  value       = ["--report-bucket=${var.bucket}", "--report-prefix=${var.prefix}"]
}
