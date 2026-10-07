locals {
  runner_state_bucket = var.runner_state_bucket != "" ? var.runner_state_bucket : "${var.project_id}-${var.name}"
}

resource "google_project_service" "services" {
  for_each = toset(concat(
    ["run.googleapis.com", "storage.googleapis.com"],
    var.schedule == null ? [] : ["cloudscheduler.googleapis.com"],
  ))

  service = each.value
  # Other deployments in the project may rely on these APIs.
  disable_on_destroy = false
}

# Holds only the runner state bucket here: grant it whatever else the job's
# container needs.
resource "google_service_account" "job" {
  account_id   = var.name
  display_name = "Cloud Run job ${var.name}"
}

# Holds the Terraform state of runner/infra, one prefix per execution.
resource "google_storage_bucket" "runner_state" {
  name     = local.runner_state_bucket
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  # No force_destroy: a state left in it may be the only record of an
  # execution's infra that is still up.

  # Recovers a state an execution overwrote or deleted by mistake.
  versioning {
    enabled = true
  }

  depends_on = [google_project_service.services]
}

resource "google_storage_bucket_iam_member" "job_runner_state" {
  bucket = google_storage_bucket.runner_state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.job.email}"
}

resource "google_cloud_run_v2_job" "job" {
  name     = var.name
  location = var.region
  # Lets terraform destroy delete the job.
  deletion_protection = false
  # start_execution_token and run_execution_token stay unset: either one makes
  # apply start a run.

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.job.email
      timeout         = var.task_timeout
      max_retries     = var.max_retries

      containers {
        image   = var.image
        command = var.command
        args    = var.args

        dynamic "env" {
          for_each = var.env
          content {
            name  = env.key
            value = env.value
          }
        }

        resources {
          limits = {
            cpu    = var.cpu
            memory = var.memory
          }
        }
      }
    }
  }

  depends_on = [google_project_service.services]
}

resource "google_service_account" "scheduler" {
  count = var.schedule == null ? 0 : 1

  # account_id is capped at 30 characters, the same as var.name.
  account_id   = "${substr(var.name, 0, 24)}-sched"
  display_name = "Cloud Scheduler trigger of ${var.name}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  count = var.schedule == null ? 0 : 1

  name     = google_cloud_run_v2_job.job.name
  location = google_cloud_run_v2_job.job.location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler[0].email}"
}

resource "google_cloud_scheduler_job" "trigger" {
  count = var.schedule == null ? 0 : 1

  name      = var.name
  region    = var.region
  schedule  = var.schedule
  time_zone = var.time_zone

  # A retry would start a second execution of the job.
  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.job.name}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler[0].email
    }
  }

  depends_on = [
    google_project_service.services,
    google_cloud_run_v2_job_iam_member.scheduler_invoker,
  ]
}
