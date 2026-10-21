# The repository benchmarking/tools/build-images builds a dev infra's images
# into, which outlives its runs: built once per version, they are pulled by
# every cluster of the dev infra, or of a run whose --image-repo names it. A run
# from a machine without --image-repo applies one for itself, and destroys it
# after its clusters.

resource "google_project_service" "artifactregistry" {
  service = "artifactregistry.googleapis.com"
  # Other deployments in the project may rely on it.
  disable_on_destroy = false
}

# Destroying it deletes the images in it too.
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = var.default_resource_name
  format        = "DOCKER"

  # A month of builds, but always each image's latest few.
  cleanup_policies {
    id     = "delete-old-images"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "2592000s"
    }
  }
  cleanup_policies {
    id     = "keep-recent-images"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  depends_on = [google_project_service.artifactregistry]
}

data "google_project" "this" {}

# atelet's pulls of an ActorTemplate's image, granted here rather than by the
# clusters: its principal is the same for every cluster in the project, so one
# cluster's destroy would take it from the others. The namespace and account
# are the cluster config's atelet_namespace and atelet_ksa_name defaults.
resource "google_artifact_registry_repository_iam_member" "atelet_reader" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/ate-system/sa/atelet"
}
