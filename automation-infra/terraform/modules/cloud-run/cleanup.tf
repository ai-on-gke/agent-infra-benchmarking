# automation-infra/tools/destroy-leftovers on a schedule, in the job's image:
# destroys what an execution of the job left when it was killed before its own
# destroy, once that execution has ended.
resource "google_cloud_run_v2_job" "cleanup" {
  count = var.schedule == null || var.cleanup_schedule == null ? 0 : 1

  project             = var.project_id
  name                = "${var.default_resource_name}-cleanup"
  location            = var.region
  deletion_protection = false

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.job.email
      timeout         = "10800s"
      max_retries     = 0

      containers {
        image = data.google_artifact_registry_docker_image.runner.self_link
        args = [
          "destroy-leftovers",
          "--project-id=${var.project_id}",
          "--region=${var.region}",
          "--job=${google_cloud_run_v2_job.job.name}",
          "--state-bucket=${var.state_bucket}",
          "--state-prefix=${var.state_bucket_prefix}",
        ]

        dynamic "env" {
          for_each = var.github_actions_bucket == null ? {} : {
            SOURCE_BUCKET = var.github_actions_bucket
            SOURCE_BRANCH = var.source_branch
          }
          content {
            name  = env.key
            value = env.value
          }
        }

        resources {
          limits = {
            cpu    = "2"
            memory = "8Gi"
          }
        }
      }
    }
  }

  depends_on = [google_project_service.services]
}

resource "google_cloud_run_v2_job_iam_member" "cleanup_invoker" {
  count = length(google_cloud_run_v2_job.cleanup)

  project  = var.project_id
  name     = google_cloud_run_v2_job.cleanup[0].name
  location = google_cloud_run_v2_job.cleanup[0].location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler[0].email}"
}

resource "google_cloud_scheduler_job" "cleanup" {
  count = length(google_cloud_run_v2_job.cleanup)

  project   = var.project_id
  name      = "${var.default_resource_name}-cleanup"
  region    = var.region
  schedule  = var.cleanup_schedule
  time_zone = var.time_zone

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.cleanup[0].name}:run"
    headers     = { "Content-Type" = "application/json" }

    oauth_token {
      service_account_email = google_service_account.scheduler[0].email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.cleanup_invoker]
}
