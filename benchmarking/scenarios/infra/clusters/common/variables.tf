variable "project_id" {
  description = "GCP project ID to deploy resources into."
  type        = string
}

variable "region" {
  description = "Default region for the cluster, the Artifact Registry repo and the bucket."
  type        = string
  default     = "us-central1"
}

# The name every resource this config creates takes unless something more
# specific is set for it. The bucket, the Artifact Registry repository and the
# cluster all take it as-is. Change it per deployment to keep two deployments
# in one project from naming the same resources.
#
# It does not reach the project-level IAM bindings below: those are keyed by
# role and principal rather than by name, and the Workload Identity principal
# is identical in every deployment of the project - so keep enable_atelet_iam
# true in at most one deployment per project.
variable "default_resource_name" {
  description = "Name applied to the resources this config creates: the GCS bucket, the Artifact Registry repository, and the cluster. Per-resource variables override it."
  type        = string
}

variable "short_resource_name" {
  description = "Name for the resources whose names are capped below var.default_resource_name's: the node service account, <name>-node, so at most 25 characters. Defaults to var.default_resource_name, cut to fit, when empty."
  type        = string
  default     = ""
}

variable "labels" {
  description = "Labels put on every resource this config creates that takes labels."
  type        = map(string)
  default     = {}
}

variable "network_name" {
  description = "Name of the dedicated VPC network (and its one subnetwork) created for this deployment. Defaults to var.default_resource_name when empty, the same way the bucket and the Artifact Registry repository do."
  type        = string
  default     = ""
}

variable "subnet_cidr" {
  description = "Primary IP CIDR range of the deployment's subnetwork, allocated to nodes."
  type        = string
  default     = "10.0.0.0/20"
}

variable "pods_cidr" {
  description = "Secondary IP CIDR range of the deployment's subnetwork, allocated to Pods."
  type        = string
  default     = "10.4.0.0/14"
}

variable "services_cidr" {
  description = "Secondary IP CIDR range of the deployment's subnetwork, allocated to Services."
  type        = string
  default     = "10.8.0.0/20"
}

# Bump it deliberately. A floor for the control plane: GKE upgrades it past
# this, and run.json records the version a run saw. The node pools stay put
# until the minor version's end of support.
variable "kubernetes_version" {
  description = "Kubernetes version of the control plane (GKE's min_master_version) and of every node pool."
  type        = string
  default     = "1.35.8-gke.1225000"
}

variable "zone" {
  description = "Zone every node pool is pinned to. Must be in var.region."
  type        = string
  default     = "us-central1-a"
}

variable "sandboxed_machine_type" {
  description = "Machine type of the sandboxed node pool, which runs the workers."
  type        = string
}

# microvm guests boot on /dev/kvm, which only nested virtualization exposes,
# and GCE offers it on no E2 machine type, so sandboxed_machine_type has to be
# n2/c2/c3 or similar.
variable "sandbox_class" {
  description = "Sandbox class of the sandboxed node pool's workers: its ate.dev/sandboxClass taint, and nested virtualization for microvm."
  type        = string
  default     = "gvisor"

  validation {
    condition     = contains(["gvisor", "microvm"], var.sandbox_class)
    error_message = "sandbox_class must be gvisor or microvm."
  }
}

# Every node pool's ate.dev/substrate-version label, which has to be the
# version hack/install-ate.sh stamps on the atelet DaemonSet: a worker pod
# schedules only onto a node whose label matches the dataplane's. The
# scenario-runner passes the submodule's, worked out as the installer does; by
# hand, that is the output of
#   git -C dependency-repos/substrate describe --tags --always --dirty
variable "substrate_version" {
  description = "Build version of the substrate submodule, as the ate.dev/substrate-version label on every node pool."
  type        = string
}

variable "sandboxed_node_count" {
  description = "Initial node count of the sandboxed node pool."
  type        = number
  default     = 1
}

variable "sandboxed_min_node_count" {
  description = "Autoscaling minimum of the sandboxed node pool."
  type        = number
  default     = 1
}

variable "sandboxed_max_node_count" {
  description = "Autoscaling maximum of the sandboxed node pool."
  type        = number
  default     = 1
}

# Pinned to var.zone like the rest, and without the substrate version label the
# pools substrate runs on get, so give one a taint unless substrate's workers
# are meant to land there.
variable "additional_node_pools" {
  description = "Node pools to create besides default, sandboxed and monitoring. name is the pool's name."
  type = list(object({
    name           = string
    machine_type   = optional(string, "e2-standard-4")
    disk_size_gb   = optional(number, 100)
    disk_type      = optional(string, "pd-balanced")
    node_count     = optional(number, 1)
    min_node_count = optional(number, 1)
    max_node_count = optional(number, 1)
    spot           = optional(bool, false)
    labels         = optional(map(string), {})
    taints = optional(list(object({
      key    = string
      value  = string
      effect = string
    })), [])
  }))
  default = []

  validation {
    condition = (
      length(distinct(var.additional_node_pools[*].name)) == length(var.additional_node_pools)
      && length(setintersection(var.additional_node_pools[*].name, ["default", "sandboxed", "monitoring"])) == 0
    )
    error_message = "additional_node_pools names must be unique and not default, sandboxed or monitoring."
  }
}

variable "artifact_repository_id" {
  description = "ID of the Artifact Registry repository. Defaults to var.default_resource_name when empty. Set it only when that name is already taken in this project."
  type        = string
  default     = ""
}
variable "artifact_repository_location" {
  description = "Location of the Artifact Registry repository: a multi-region (\"us\", \"europe\", \"asia\") or a region (\"us-central1\")."
  type        = string
  default     = "us"
}

variable "artifact_repository_format" {
  description = "Format of the Artifact Registry repository (e.g. DOCKER, NPM, PYTHON)."
  type        = string
  default     = "DOCKER"
}


variable "bucket_name" {
  description = "Name of the GCS bucket. Defaults to var.default_resource_name when empty. Bucket names are globally unique across GCP, so expect to set this one."
  type        = string
  default     = ""
}

variable "bucket_location" {
  description = "Location for the GCS bucket. Defaults to var.region if not set."
  type        = string
  default     = ""
}

variable "bucket_storage_class" {
  description = "Storage class of the GCS bucket."
  type        = string
  default     = "STANDARD"
}

variable "bucket_force_destroy" {
  description = "If true, terraform destroy deletes the bucket with everything in it, every run's reports included. If false, destroy fails while the bucket holds anything."
  type        = bool
  default     = false
}

variable "bucket_soft_delete_retention_seconds" {
  description = "How long a deleted object stays recoverable, and billed. 0 disables soft delete; GCS's own default is 604800 (7 days)."
  type        = number
  default     = 0
}

variable "report_retention_days" {
  description = "Delete run reports under reports/ this many days after they were written. 0 keeps them."
  type        = number
  default     = 0
}

variable "enable_apis" {
  description = "Enable the GCP APIs the deployment needs. Set false where they are managed elsewhere."
  type        = bool
  default     = true
}

variable "enable_atelet_bucket_iam" {
  description = "Grants atelet and ate-api-server objectAdmin + bucketViewer on this deployment's snapshot bucket."
  type        = bool
  default     = true
}

variable "enable_atelet_iam" {
  description = "Grants atelet project-level storage and Artifact Registry roles, reaching every bucket and repository in the project. Off by default and rarely wanted: the repository-scoped grants in main.tf are unconditional and cover atelet for this deployment's own repository, and the nodes have their own service account rather than the project's default one. Turn this on only for a deployment that genuinely has to read or write outside what this config created. See cmd/iam.go and tools/setup-gcp/README.md for the authoritative list."
  type        = bool
  default     = false
}

variable "atelet_namespace" {
  description = "Kubernetes namespace atelet's (and ate-api-server's) ServiceAccount runs in. Must match exactly - there's no cross-namespace wildcard for Workload Identity Federation IAM principals or Kubernetes RBAC subjects."
  type        = string
  default     = "ate-system"
}

variable "atelet_ksa_name" {
  description = "Name of atelet's Kubernetes ServiceAccount."
  type        = string
  default     = "atelet"
}

# ate-api-server needs bucket-level access alongside atelet for actor
# SuspendActor/ResumeActor calls used by glutton-based load tests, but no
# project-level grants.
variable "ate_api_server_ksa_name" {
  description = "Name of ate-api-server's Kubernetes ServiceAccount (runs in var.atelet_namespace)."
  type        = string
  default     = "ate-api-server"
}


variable "benchmark_runner_namespace" {
  description = "Kubernetes namespace the benchmark runner Job's ServiceAccount runs in."
  type        = string
  default     = "benchmarking"
}

variable "benchmark_runner_ksa_name" {
  description = "Name of the benchmark runner Job's Kubernetes ServiceAccount."
  type        = string
  default     = "benchmark-runner"
}

