output "cluster_names" {
  description = "Names of the deployed GKE clusters, keyed by cluster key."
  value       = { for k, m in module.clusters : k => m.cluster_name }
}

output "cluster_endpoints" {
  description = "Endpoints of the deployed GKE clusters, keyed by cluster key."
  value       = { for k, m in module.clusters : k => m.cluster_endpoint }
  sensitive   = true
}

output "artifact_registry_repository" {
  description = "Full resource name of the Artifact Registry repository."
  value       = google_artifact_registry_repository.this.name
}

output "bucket_name" {
  description = "Name of the GCS bucket."
  value       = google_storage_bucket.this.name
}

output "bucket_url" {
  description = "gs:// URL of the GCS bucket."
  value       = google_storage_bucket.this.url
}

output "atelet_wi_principal" {
  description = "IAM principal atelet authenticates as via Workload Identity Federation."
  value       = local.atelet_wi_principal
}

output "get_credentials_commands" {
  description = "gcloud commands to fetch kubeconfig credentials for each cluster, keyed by cluster key."
  value       = { for k, m in module.clusters : k => m.get_credentials_command }
}
