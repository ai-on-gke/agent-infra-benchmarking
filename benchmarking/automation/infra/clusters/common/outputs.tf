# Echoes of the inputs, so that a consumer of these outputs - the orchestrator
# configures the scripts it runs on the cluster from them - needs nothing but
# them to learn which project and region the deployment landed in.

output "project_id" {
  description = "GCP project the resources were created in."
  value       = var.project_id
}

output "region" {
  description = "Default region of the deployment: the Artifact Registry repo, the bucket, and the cluster unless it overrode it."
  value       = var.region
}

output "cluster_name" {
  description = "Name of the deployed GKE cluster."
  value       = module.cluster.cluster_name
}

output "cluster_endpoint" {
  description = "Endpoint of the deployed GKE cluster."
  value       = module.cluster.cluster_endpoint
  sensitive   = true
}

# What the nodes are labeled with, so a dataplane that schedules nowhere can be
# checked against what the install put on the atelet DaemonSet:
#   kubectl get ds -n ate-system -l app=atelet -L ate.dev/substrate-version
output "substrate_version" {
  description = "Build version of the substrate submodule, as the ate.dev/substrate-version label on every node pool."
  value       = local.substrate_version
}

output "network_name" {
  description = "Name of the dedicated VPC network created for this deployment."
  value       = google_compute_network.this.name
}

output "subnetwork_name" {
  description = "Name of the subnetwork nodes, Pods and Services were allocated from."
  value       = google_compute_subnetwork.this.name
}

output "artifact_registry_repository" {
  description = "Full resource name of the Artifact Registry repository."
  value       = google_artifact_registry_repository.this.name
}

# The same repository spelled as an image path prefix, which is the form a
# docker push, a ko build or an ActorTemplate image field takes. Composed from
# the inputs rather than from the resource so the spelling is the one that was
# asked for, independent of how the registry echoes it back.
output "artifact_registry_docker_repo" {
  description = "Image path prefix for the Artifact Registry repository, e.g. us-docker.pkg.dev/my-project/my-repo."
  value       = "${var.artifact_repository_location}-docker.pkg.dev/${var.project_id}/${local.artifact_repository_id}"
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

output "node_service_account" {
  description = "Service account the node pools run as. Holds the GKE minimum at project level, and read on this deployment's repository and bucket. An image this deployment's pods have to pull belongs in that repository, or this account needs naming on whatever holds it instead."
  value       = google_service_account.node.email
}

output "get_credentials_command" {
  description = "gcloud command to fetch kubeconfig credentials for the cluster."
  value       = module.cluster.get_credentials_command
}
