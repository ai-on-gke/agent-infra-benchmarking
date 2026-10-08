# The inputs of common that the orchestrator and tests.yaml's cases pass, so a
# case can switch infra.cluster.name between the two. Nothing here reaches GCP.

variable "project_id" {
  description = "Unused; taken for the orchestrator's -var."
  type        = string
}

variable "region" {
  description = "Unused; taken for the orchestrator's -var."
  type        = string
  default     = "us-central1"
}

variable "default_resource_name" {
  description = "Name of the fake cluster, as of common's real one."
  type        = string
}

variable "sandboxed_node_count" {
  description = "Number of fake nodes in the sandboxed pool."
  type        = number
  default     = 1
}

variable "sandboxed_min_node_count" {
  description = "Autoscaling minimum of the sandboxed pool, checked as GKE would."
  type        = number
  default     = 1
}

variable "sandboxed_max_node_count" {
  description = "Autoscaling maximum of the sandboxed pool, checked as GKE would."
  type        = number
  default     = 1
}
