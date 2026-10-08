# The inputs of common that the scenario-runner and scenarios.yaml's cases pass,
# so a case can switch infra.cluster.name between the two. Nothing here
# reaches GCP.

variable "project_id" {
  description = "Unused; taken for the scenario-runner's -var."
  type        = string
}

variable "region" {
  description = "Unused; taken for the scenario-runner's -var."
  type        = string
  default     = "us-central1"
}

variable "default_resource_name" {
  description = "Name of the fake cluster, as of common's real one."
  type        = string
}

variable "kubernetes_version" {
  description = "Unused; taken as common takes it."
  type        = string
  default     = ""
}

variable "zone" {
  description = "Unused; taken as common takes it."
  type        = string
  default     = ""
}

variable "sandboxed_machine_type" {
  description = "Unused; taken as common takes it."
  type        = string
  default     = ""
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
