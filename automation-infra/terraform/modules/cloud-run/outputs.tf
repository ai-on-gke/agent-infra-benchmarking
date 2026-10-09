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
  description = "The runner image the job runs, by digest."
  value       = data.google_artifact_registry_docker_image.runner.self_link
}

output "github_service_account_email" {
  description = "Service account GitHub Actions write under source/ as, or null when var.github_actions_bucket is null."
  value       = one(google_service_account.github[*].email)
}
