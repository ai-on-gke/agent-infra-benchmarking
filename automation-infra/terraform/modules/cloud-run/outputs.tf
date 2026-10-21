output "job_name" {
  description = "Name of the Cloud Run job."
  value       = google_cloud_run_v2_job.job.name
}

output "service_account_email" {
  description = "Service account the job runs as, and so the one scenario_runner/infra is applied as."
  value       = google_service_account.job.email
}

output "scheduler_job_name" {
  description = "Name of the Cloud Scheduler job, or null when var.schedule is null."
  value       = one(google_cloud_scheduler_job.trigger[*].name)
}

output "execute_command" {
  description = "Runs the job once, now."
  value       = "gcloud run jobs execute ${google_cloud_run_v2_job.job.name} --project=${var.project_id} --region=${var.region}"
}

output "runner_image" {
  description = "The image the jobs run: unless runner_image, by the tag .github/workflows/runner-image.yml moves to source_branch's latest commit."
  value       = local.runner_image
}

output "reports_dataset" {
  description = "BigQuery dataset of the reports' views, as project.dataset, or null when var.reports_bucket is null."
  value       = one(module.reports[*].dataset)
}

output "github_service_account_email" {
  description = "Service account GitHub Actions push the runner image and write under source/ as, or null when var.github_actions_bucket is null."
  value       = one(google_service_account.github[*].email)
}

output "github_workload_identity_provider" {
  description = "Workload Identity provider GitHub Actions authenticate through, as the github_service_account_email account, or null when var.github_actions_bucket is null."
  value       = one(google_iam_workload_identity_pool_provider.github[*].name)
}

output "cleanup_job_name" {
  description = "Name of the Cloud Run job that runs destroy-leftovers, or null when there is none."
  value       = one(google_cloud_run_v2_job.cleanup[*].name)
}
