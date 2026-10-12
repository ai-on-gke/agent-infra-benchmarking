output "artifact_registry_repository" {
  description = "Full resource name of the Artifact Registry repository."
  value       = google_artifact_registry_repository.images.id
}

output "docker_repo" {
  description = "Image path prefix of the repository, e.g. us-central1-docker.pkg.dev/my-project/my-run."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}
