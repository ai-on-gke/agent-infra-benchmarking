resource "google_service_account" "job" {
  project      = var.project_id
  account_id   = var.default_resource_name
  display_name = "Cloud Run job ${var.default_resource_name}"
}

# The Terraform state of runner/infra, under var.state_bucket_prefix. Listing
# is checked on the bucket rather than an object, hence its own clause.
resource "google_storage_bucket_iam_member" "job_state" {
  bucket = var.state_bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.job.email}"

  condition {
    title = "state-prefix-only"
    expression = join(" || ", [
      "resource.name.startsWith(\"projects/_/buckets/${var.state_bucket}/objects/${var.state_bucket_prefix}/\")",
      "api.getAttribute(\"storage.googleapis.com/objectListPrefix\", \"\").startsWith(\"${var.state_bucket_prefix}/\")",
    ])
  }
}

# The source tarballs the image fetches, which GitHub Actions uploads.
resource "google_storage_bucket_iam_member" "job_source" {
  count = var.github_actions_bucket == null ? 0 : 1

  bucket = var.github_actions_bucket
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.job.email}"

  condition {
    title      = "source-only"
    expression = "resource.name.startsWith(\"projects/_/buckets/${var.github_actions_bucket}/objects/source/\")"
  }
}

# What runner/infra creates in the project.
resource "google_project_iam_member" "job" {
  for_each = toset([
    "roles/serviceusage.serviceUsageAdmin",
    "roles/artifactregistry.admin",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.job.email}"
}

resource "google_cloud_run_v2_job" "job" {
  project  = var.project_id
  name     = var.default_resource_name
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
        image   = data.google_artifact_registry_docker_image.runner.self_link
        command = var.command
        # run.py's infra goes in this deployment's project and region,
        # with its state in the deployment's state bucket.
        args = concat([
          "--project-id=${var.project_id}",
          "--region=${var.region}",
          "--state-bucket=${var.state_bucket}",
          "--state-prefix=${var.state_bucket_prefix}",
        ], var.args)

        dynamic "env" {
          for_each = merge(var.github_actions_bucket == null ? {} : {
            SOURCE_BUCKET = var.github_actions_bucket
            SOURCE_BRANCH = var.source_branch
          }, var.env)
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

  project = var.project_id
  # account_id is capped at 30 characters, the same as var.default_resource_name.
  account_id   = "${substr(var.default_resource_name, 0, 24)}-sched"
  display_name = "Cloud Scheduler trigger of ${var.default_resource_name}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  count = var.schedule == null ? 0 : 1

  project  = var.project_id
  name     = google_cloud_run_v2_job.job.name
  location = google_cloud_run_v2_job.job.location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler[0].email}"
}

resource "google_cloud_scheduler_job" "trigger" {
  count = var.schedule == null ? 0 : 1

  project   = var.project_id
  name      = var.default_resource_name
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
