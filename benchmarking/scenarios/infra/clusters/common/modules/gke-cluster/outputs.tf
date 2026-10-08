output "cluster_name" {
  description = "Name of the GKE cluster."
  value       = local.cluster_name
}

output "cluster_endpoint" {
  description = "Endpoint of the GKE cluster."
  value       = local.cluster_endpoint
  sensitive   = true
}

output "get_credentials_command" {
  description = "gcloud command to fetch kubeconfig credentials for this cluster."
  value       = "gcloud container clusters get-credentials ${local.cluster_name} --region ${var.region} --project ${var.project_id}"
}
