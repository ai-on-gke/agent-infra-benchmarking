resource "google_service_account" "job" {
  project      = var.project_id
  account_id   = var.default_resource_name
  display_name = "Cloud Run job ${var.default_resource_name}"
}

# The Terraform state of scenario_runner/infra, under var.state_bucket_prefix.
# Listing is checked on the bucket rather than an object, hence its own clause.
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

# What scenario_runner/infra creates in the project, and what the run does on
# it: substrate's install on the cluster and the images' Cloud Build. The job
# creates no service account and grants no project role: its clusters' nodes
# run as the nodes account below, and its builds as the images one.
resource "google_project_iam_member" "job" {
  for_each = toset([
    "roles/serviceusage.serviceUsageAdmin",
    "roles/artifactregistry.admin",
    "roles/container.admin",
    "roles/compute.networkAdmin",
    "roles/compute.securityAdmin",
    "roles/cloudbuild.builds.editor",
    # For gcloud builds submit to stream the build's log.
    "roles/logging.viewer",
    # For destroy-leftovers to tell a running execution from an ended one.
    "roles/run.viewer",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.job.email}"
}

locals {
  # The scenario-runner names an execution's buckets after its run id,
  # run-<UTC timestamp> on this job.
  run_bucket_prefix = "projects/_/buckets/${var.project_id}-run-"
}

# The account every run's cluster nodes run as (NODE_SERVICE_ACCOUNT, which the
# scenario-runner passes to the cluster config), with the project roles GKE
# nodes need, granted here once rather than by each run.
resource "google_service_account" "nodes" {
  project      = var.project_id
  account_id   = "${substr(var.default_resource_name, 0, 24)}-nodes"
  display_name = "GKE nodes of ${var.default_resource_name}'s runs"
}

resource "google_project_iam_member" "nodes" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.nodes.email}"
}

resource "google_service_account_iam_member" "job_act_as_nodes" {
  service_account_id = google_service_account.nodes.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.job.email}"
}

# The runs' buckets, and creating them, which is checked on the project.
resource "google_project_iam_member" "job_run_buckets" {
  project = var.project_id
  role    = "roles/storage.admin"
  member  = "serviceAccount:${google_service_account.job.email}"

  condition {
    title = "run-buckets-only"
    expression = join(" || ", [
      "resource.type == \"cloudresourcemanager.googleapis.com/Project\"",
      "resource.name.startsWith(\"${local.run_bucket_prefix}\")",
    ])
  }
}

# The reports under var.reports_prefix: the runner checks a run id is new,
# uploads the Jobs' logs and cleanup.json beside the master's reports, and
# reads them back.
resource "google_storage_bucket_iam_member" "job_reports" {
  count = var.reports_bucket == null ? 0 : 1

  bucket = var.reports_bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.job.email}"

  condition {
    title = "reports-prefix-only"
    expression = join(" || ", [
      "resource.name.startsWith(\"projects/_/buckets/${var.reports_bucket}/objects/${var.reports_prefix}/\")",
      "api.getAttribute(\"storage.googleapis.com/objectListPrefix\", \"\").startsWith(\"${var.reports_prefix}/\")",
    ])
  }
}

# The account the job's Cloud Builds run as (images.py and substrate.py in the
# scenario-runner), rather than the project's default, which the job would
# otherwise have to act as. Each run's repository grants it push itself
# (scenario_runner/infra's image_writers), so it holds no repository role here.
resource "google_service_account" "images" {
  project      = var.project_id
  account_id   = "${substr(var.default_resource_name, 0, 23)}-images"
  display_name = "Cloud Build of ${var.default_resource_name}'s benchmark images"
}

resource "google_project_iam_member" "images_logs" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.images.email}"
}

resource "google_service_account_iam_member" "job_act_as_images" {
  service_account_id = google_service_account.images.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.job.email}"
}

# Where the job stages the source of those builds, for the images account to
# read.
resource "google_storage_bucket_iam_member" "job_build_source" {
  bucket = google_storage_bucket.build_source.name
  role   = "roles/storage.admin"
  member = "serviceAccount:${google_service_account.job.email}"
}

resource "google_storage_bucket_iam_member" "images_build_source" {
  bucket = google_storage_bucket.build_source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.images.email}"
}

locals {
  # The scenario-runner's infra goes in this deployment's project and region,
  # with its state in the deployment's state bucket. The image has no Docker,
  # so the benchmark images build on Cloud Build.
  module_args = concat([
    "--project-id=${var.project_id}",
    "--region=${var.region}",
    "--state-backend=gcs",
    "--state-bucket=${var.state_bucket}",
    "--state-prefix=${var.state_bucket_prefix}",
    "--cloud-build",
  ], flatten([for r in module.reports : ["--report-bucket=${r.bucket}", "--report-prefix=${r.prefix}"]]))
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
        image   = local.runner_image
        command = var.command
        args    = concat(local.module_args, var.args)

        # The scenario-runner labels the infra with BENCHMARK_TRIGGER, which
        # the scheduler's run overrides with scheduled.
        dynamic "env" {
          for_each = merge({
            BENCHMARK_TRIGGER           = "manual"
            CLOUD_BUILD_SERVICE_ACCOUNT = google_service_account.images.email
            NODE_SERVICE_ACCOUNT        = google_service_account.nodes.email
            CLOUD_BUILD_STAGING_DIR     = "gs://${google_storage_bucket.build_source.name}/benchmark"
            CLUSTER_NETWORK             = google_compute_network.runs.id
            CLUSTER_SUBNETWORK          = google_compute_subnetwork.runs.id
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

      # Into the runs' network for their clusters' internal endpoints; the
      # rest, such as GitHub and Google's APIs, goes out directly.
      vpc_access {
        network_interfaces {
          network    = google_compute_network.runs.id
          subnetwork = google_compute_subnetwork.runs.id
        }
        egress = "PRIVATE_RANGES_ONLY"
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
  # Not run.invoker, which lacks run.jobs.runWithOverrides.
  role   = "roles/run.jobsExecutorWithOverrides"
  member = "serviceAccount:${google_service_account.scheduler[0].email}"
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
    # env is merged into the job's, while args replace its own, hence the
    # module's again.
    body = base64encode(jsonencode({
      overrides = { containerOverrides = [merge(
        { env = [{ name = "BENCHMARK_TRIGGER", value = "scheduled" }] },
        var.schedule_args == null ? {} : { args = concat(local.module_args, var.schedule_args) },
      )] }
    }))
    headers = { "Content-Type" = "application/json" }

    oauth_token {
      service_account_email = google_service_account.scheduler[0].email
    }
  }

  depends_on = [
    google_project_service.services,
    google_cloud_run_v2_job_iam_member.scheduler_invoker,
  ]
}
