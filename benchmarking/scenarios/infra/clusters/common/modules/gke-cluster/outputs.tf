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
  value       = "gcloud container clusters get-credentials ${local.cluster_name} --location ${local.location} --project ${var.project_id}${var.enable_private_endpoint ? " --internal-ip" : ""}"
}

output "location" {
  description = "Location of the cluster: its region, or its zone for a zonal one."
  value       = local.location
}
