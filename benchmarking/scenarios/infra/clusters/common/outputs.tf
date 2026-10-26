# Echoes of the inputs, so that a consumer of these outputs - the
# scenario-runner configures the scripts it runs on the cluster from them -
# needs nothing but them to learn which project and region the deployment
# landed in.

output "project_id" {
  description = "GCP project the resources were created in."
  value       = var.project_id
}

output "region" {
  description = "Default region of the deployment: the bucket's, and the cluster's or its zone's."
  value       = var.region
}

output "location" {
  description = "Location of the cluster: var.zone, or var.region for a regional control plane."
  value       = module.cluster.location
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
  value       = var.substrate_version
}

output "image_docker_repo" {
  description = "Image path prefix of var.image_repository, which substrate's images and the benchmark's are pulled from; null for none."
  value = (var.image_repository == null ? null :
  "${local.image_repository[3]}-docker.pkg.dev/${local.image_repository[1]}/${local.image_repository[5]}")
}

output "sandboxed_kvm" {
  description = "Whether the sandboxed node pool has /dev/kvm, which microvm workers boot their guests on."
  value       = var.nested_virtualization || local.sandboxed_bare_metal
}

output "sandboxed_gke_sandbox" {
  description = "Whether the sandboxed node pool runs GKE Sandbox, which agent-sandbox's gvisor pods run in."
  value       = var.gke_sandbox
}

output "network_name" {
  description = "Name of the VPC network the cluster is on: the dedicated one, or var.existing_network's."
  value       = reverse(split("/", local.network))[0]
}

output "subnetwork_name" {
  description = "Name of the subnetwork nodes, Pods and Services were allocated from."
  value       = reverse(split("/", local.subnetwork))[0]
}

# The same repository spelled as an image path prefix, which is the form a
# docker push, a ko build or an ActorTemplate image field takes. Composed from
# the inputs rather than from the resource so the spelling is the one that was
# asked for, independent of how the registry echoes it back.
output "bucket_name" {
  description = "Name of the GCS bucket."
  value       = google_storage_bucket.this.name
}

output "bucket_url" {
  description = "gs:// URL of the GCS bucket."
  value       = google_storage_bucket.this.url
}

output "benchmark_runner_ksa_name" {
  description = "Kubernetes ServiceAccount the benchmark runner Jobs run as, which the bucket grants object creation."
  value       = var.benchmark_runner_ksa_name
}

output "atelet_wi_principal" {
  description = "IAM principal atelet authenticates as via Workload Identity Federation."
  value       = local.atelet_wi_principal
}

output "node_service_account" {
  description = "Service account the node pools run as. Holds the GKE minimum at project level, and read on this deployment's bucket, and on var.image_repository when this config created it. An image this deployment's pods have to pull belongs in that repository, or this account needs naming on whatever holds it instead."
  value       = local.node_service_account
}

output "private_endpoint" {
  description = "Whether the control plane has an internal endpoint only, which get-credentials needs --internal-ip for."
  value       = var.enable_private_endpoint
}

output "get_credentials_command" {
  description = "gcloud command to fetch kubeconfig credentials for the cluster."
  value       = module.cluster.get_credentials_command
}
