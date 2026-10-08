locals {
  runner_state_bucket = var.runner_state_bucket != "" ? var.runner_state_bucket : "${var.project_id}-${var.default_resource_name}"

  runner_context = abspath("${path.module}/image")
  runner_sources = fileset(local.runner_context, "**")
  # Rebuilds the image on apply when any of its sources or the build changes,
  # also uncommitted, as the commit it is tagged with does not.
  runner_hash = sha1(join("\n", concat(
    [for f in local.runner_sources : "${f} ${filesha1("${local.runner_context}/${f}")}"],
    [filesha1("${path.module}/cloudbuild.yaml")],
  )))
  runner_image_name = "test-runner:${data.external.git.result.commit}"
  runner_image      = "${google_artifact_registry_repository.images.registry_uri}/${local.runner_image_name}"
}

# The repo's commit, which the image is tagged with like the other images.
data "external" "git" {
  program     = ["sh", "-c", "printf '{\"commit\": \"%s\"}' \"$(git rev-parse HEAD)\""]
  working_dir = path.module
}

resource "google_project_service" "services" {
  for_each = toset(concat(
    ["run.googleapis.com", "storage.googleapis.com", "artifactregistry.googleapis.com", "cloudbuild.googleapis.com"],
    var.schedule == null ? [] : ["cloudscheduler.googleapis.com"],
  ))

  project = var.project_id
  service = each.value
  # Other deployments in the project may rely on these APIs.
  disable_on_destroy = false
}

resource "google_service_account" "job" {
  project      = var.project_id
  account_id   = var.default_resource_name
  display_name = "Cloud Run job ${var.default_resource_name}"
}

# Holds the Terraform state of runner/infra, one prefix per execution.
resource "google_storage_bucket" "runner_state" {
  project  = var.project_id
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

resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = var.default_resource_name
  format        = "DOCKER"

  depends_on = [google_project_service.services]
}

# Runs the image's builds, rather than the project's default Cloud Build
# account, which an org policy may have left without the roles to push.
resource "google_service_account" "build" {
  project = var.project_id
  # account_id is capped at 30 characters, the same as var.default_resource_name.
  account_id   = "${substr(var.default_resource_name, 0, 24)}-build"
  display_name = "Cloud Build of ${var.default_resource_name}'s image"
}

resource "google_artifact_registry_repository_iam_member" "build_writer" {
  project    = var.project_id
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.build.email}"
}

resource "google_project_iam_member" "build_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.build.email}"
}

# Where gcloud builds submit uploads the build's source, in place of the
# project's <project>_cloudbuild bucket.
resource "google_storage_bucket" "build_source" {
  project  = var.project_id
  name     = "${var.default_resource_name}-build-source"
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true

  # A build reads its source once, as it starts.
  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.services]
}

resource "google_storage_bucket_iam_member" "build_source_reader" {
  bucket = google_storage_bucket.build_source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.build.email}"
}

# Builds and pushes the runner image on Cloud Build, so apply needs gcloud but
# no Docker. Replaced, and so run again, whenever the commit or runner_hash
# changes.
resource "terraform_data" "runner_image" {
  input            = local.runner_image
  triggers_replace = [local.runner_image, local.runner_hash]

  provisioner "local-exec" {
    command = "gcloud builds submit \"$CONTEXT\" --project=\"$PROJECT\" --config=\"$CONFIG\" --substitutions=_IMAGE=\"$IMAGE\" --service-account=\"$SERVICE_ACCOUNT\" --gcs-source-staging-dir=\"$SOURCE_DIR\""
    environment = {
      CONTEXT         = local.runner_context
      PROJECT         = var.project_id
      CONFIG          = abspath("${path.module}/cloudbuild.yaml")
      IMAGE           = local.runner_image
      SERVICE_ACCOUNT = google_service_account.build.name
      SOURCE_DIR      = "gs://${google_storage_bucket.build_source.name}/source"
    }
  }

  depends_on = [
    google_artifact_registry_repository_iam_member.build_writer,
    google_project_iam_member.build_log_writer,
    google_storage_bucket_iam_member.build_source_reader,
  ]
}

# The image just pushed, by digest: a rebuild of the same commit keeps the tag
# but still changes the job's image.
data "google_artifact_registry_docker_image" "runner" {
  project       = var.project_id
  location      = google_artifact_registry_repository.images.location
  repository_id = google_artifact_registry_repository.images.repository_id
  image_name    = local.runner_image_name

  depends_on = [terraform_data.runner_image]
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
        # with its state in the runner state bucket.
        args = concat([
          "--project-id=${var.project_id}",
          "--region=${var.region}",
          "--state-bucket=${google_storage_bucket.runner_state.name}",
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
