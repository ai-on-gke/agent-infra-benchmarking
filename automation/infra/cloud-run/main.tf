locals {
  runner_state_bucket = var.runner_state_bucket != "" ? var.runner_state_bucket : "${var.project_id}-${var.default_resource_name}"

  # automation/, the runner image's build context.
  runner_context = abspath("${path.module}/../..")
  # The files the image is built from, less what .dockerignore leaves out.
  runner_sources = concat(["tests.yaml", ".dockerignore"], [
    for f in fileset(local.runner_context, "runner/**") : f
    if !can(regex("(^|/)(\\.terraform|__pycache__)/|\\.tfstate|\\.tfvars$", f))
  ])
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

  service = each.value
  # Other deployments in the project may rely on these APIs.
  disable_on_destroy = false
}

# Holds only the runner state bucket here: grant it whatever else the job's
# container needs.
resource "google_service_account" "job" {
  account_id   = var.default_resource_name
  display_name = "Cloud Run job ${var.default_resource_name}"
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

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = var.default_resource_name
  format        = "DOCKER"

  depends_on = [google_project_service.services]
}

# Builds and pushes the runner image on Cloud Build, so apply needs gcloud but
# no Docker. Replaced, and so run again, whenever the commit or runner_hash
# changes.
resource "terraform_data" "runner_image" {
  input            = local.runner_image
  triggers_replace = [local.runner_image, local.runner_hash]

  provisioner "local-exec" {
    command = "gcloud builds submit \"$CONTEXT\" --project=\"$PROJECT\" --config=\"$CONFIG\" --ignore-file=.dockerignore --substitutions=_IMAGE=\"$IMAGE\""
    environment = {
      CONTEXT = local.runner_context
      PROJECT = var.project_id
      CONFIG  = abspath("${path.module}/cloudbuild.yaml")
      IMAGE   = local.runner_image
    }
  }
}

# The image just pushed, by digest: a rebuild of the same commit keeps the tag
# but still changes the job's image.
data "google_artifact_registry_docker_image" "runner" {
  location      = google_artifact_registry_repository.images.location
  repository_id = google_artifact_registry_repository.images.repository_id
  image_name    = local.runner_image_name

  depends_on = [terraform_data.runner_image]
}

resource "google_cloud_run_v2_job" "job" {
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

  # account_id is capped at 30 characters, the same as var.default_resource_name.
  account_id   = "${substr(var.default_resource_name, 0, 24)}-sched"
  display_name = "Cloud Scheduler trigger of ${var.default_resource_name}"
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
