locals {
  # Unless var.runner_image, the image .github/workflows/runner-image.yml
  # pushes for var.source_branch's latest commit, by tag, which Cloud Run
  # resolves as each execution starts: the branch with each character a tag
  # can't hold as a hyphen, as the workflow tags it.
  runner_tag   = replace(var.source_branch, "/[^A-Za-z0-9_.-]/", "-")
  runner_image = coalesce(var.runner_image, "${google_artifact_registry_repository.images.registry_uri}/test-runner:${local.runner_tag}")
}

resource "google_project_service" "services" {
  for_each = toset(concat(
    ["run.googleapis.com", "storage.googleapis.com", "artifactregistry.googleapis.com", "cloudbuild.googleapis.com",
    "monitoring.googleapis.com", "compute.googleapis.com"],
    var.schedule == null ? [] : ["cloudscheduler.googleapis.com"],
    var.github_actions_bucket == null ? [] : ["iam.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com"],
  ))

  project = var.project_id
  service = each.value
  # Other deployments in the project may rely on these APIs.
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = var.default_resource_name
  format        = "DOCKER"

  # A month of commits' runner images, their build cache included, but always
  # the latest few and the one the job runs.
  cleanup_policies {
    id     = "delete-old-runner-images"
    action = "DELETE"
    condition {
      tag_state             = "ANY"
      package_name_prefixes = ["test-runner"]
      older_than            = "2592000s"
    }
  }
  cleanup_policies {
    id     = "keep-recent-runner-images"
    action = "KEEP"
    most_recent_versions {
      package_name_prefixes = ["test-runner"]
      keep_count            = 10
    }
  }
  cleanup_policies {
    id     = "keep-job-runner-image"
    action = "KEEP"
    condition {
      tag_state             = "TAGGED"
      package_name_prefixes = ["test-runner"]
      tag_prefixes          = [local.runner_tag]
    }
  }

  depends_on = [google_project_service.services]
}

# Where the job's gcloud builds submit uploads its builds' source, in place of
# the project's <project>_cloudbuild bucket.
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
