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

  depends_on = [google_project_service.artifactregistry]
}
