output "dataset" {
  description = "BigQuery dataset of the reports' views, as project.dataset."
  value       = "${google_bigquery_dataset.reports.project}.${google_bigquery_dataset.reports.dataset_id}"
}

output "bucket" {
  description = "Bucket the reports go to."
  value       = local.bucket
}

output "prefix" {
  description = "Prefix in bucket the reports go under."
  value       = var.prefix
}
