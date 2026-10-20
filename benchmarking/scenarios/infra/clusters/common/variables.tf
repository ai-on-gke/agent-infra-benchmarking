variable "project_id" {
  description = "GCP project ID to deploy resources into."
  type        = string
}

variable "region" {
  description = "Default region for the cluster and the bucket."
  type        = string
  default     = "us-central1"
}

# The name every resource this config creates takes unless something more
# specific is set for it. The bucket and the cluster take it as-is. Change it
# per deployment to keep two deployments in one project from naming the same
# resources.
#
# It does not reach the project-level IAM bindings below: those are keyed by
# role and principal rather than by name, and the Workload Identity principal
# is identical in every deployment of the project - so keep enable_atelet_iam
# true in at most one deployment per project.
variable "default_resource_name" {
  description = "Name applied to the resources this config creates: the GCS bucket and the cluster. Per-resource variables override it."
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
  description = "Name of the dedicated VPC network (and its one subnetwork) created for this deployment. Defaults to var.default_resource_name when empty, the same way the bucket does."
  type        = string
  default     = ""
}

variable "existing_network" {
  description = "An existing VPC to deploy into instead of creating a dedicated one. network and subnetwork take a name or a self link; the subnetwork has to be in the cluster's region. Pods and Services take the subnetwork's secondary ranges named by pods_range_name and services_range_name, or without them ranges GKE adds to the subnetwork for the cluster, so clusters can share it. Its firewall rules and NAT are left to whoever owns the network: nodes, Pods and Services need to reach each other, and private nodes need a NAT to reach public registries. Null, the default, creates the VPC from network_name and the CIDRs below, which are then unused."
  type = object({
    network             = string
    subnetwork          = string
    pods_range_name     = optional(string)
    services_range_name = optional(string)
  })
  default = null
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

# A minor version, as REGULAR stops offering each exact build within weeks;
# run.json records the build a run got. It has to be one REGULAR offers in
# var.zone, and in var.region too for a regional control plane:
#   gcloud container get-server-config --location=<zone or region> \
#     --flatten=channels --filter=channels.channel=REGULAR \
#     --format="value(channels.validVersions)"
# The cluster's upgrade exclusion keeps it on this minor version and the node
# pools where they are until end of support; GKE still patches the control
# plane.
variable "kubernetes_version" {
  description = "GKE version of the control plane (GKE's min_master_version), which the node pools are created at: a minor such as 1.35.8-gke.1439001, for which GKE picks a build the REGULAR channel offers, or an exact build."
  type        = string
  default     = "1.35.8-gke.1439001"
}

variable "zone" {
  description = "Zone every node pool is pinned to, and the zonal cluster's location. Must be in var.region."
  type        = string
  default     = "us-central1-a"
}

variable "regional_control_plane" {
  description = "Whether the control plane is regional, replicated across var.region's zones, rather than in var.zone with the node pools."
  type        = bool
  default     = false
}

# Without external IPs the nodes reach anything outside Google's APIs, such as
# public image registries, through the Cloud NAT network.tf creates with the
# network; an existing_network needs its own.
variable "enable_private_nodes" {
  description = "Whether the nodes have internal IPs only."
  type        = bool
  default     = true
}

variable "enable_private_endpoint" {
  description = "Whether the control plane has an internal endpoint only, for a scenario-runner inside the cluster's network, such as automation-infra's Cloud Run job with existing_network. False, the default, gives it a public one too, which IAM still guards, for a scenario-runner outside."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_private_endpoint || var.enable_private_nodes
    error_message = "enable_private_endpoint needs enable_private_nodes."
  }
}

# Null leaves the public endpoint open to any address; access still takes IAM.
# With enable_private_endpoint, the cluster's subnetwork is added to them.
variable "master_authorized_networks" {
  description = "CIDR blocks allowed to reach the control plane's public endpoint, with a display name each; null for any."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = null
}

variable "default_machine_type" {
  description = "Machine type of the default pool, which runs GKE's system workloads and substrate's control plane."
  type        = string
  default     = "c2-standard-16"
}

# microvm guests boot on /dev/kvm, which only nested virtualization exposes,
# and GCE offers it only on Intel machine families: not E2, AMD (n2d, c2d, ...)
# or Arm (t2a, c4a, ...).
variable "sandboxed_machine_type" {
  description = "Machine type of the sandboxed node pool, which runs the workers."
  type        = string

  validation {
    condition = var.sandbox_class != "microvm" || !(
      split("-", var.sandboxed_machine_type)[0] == "e2"
      || endswith(split("-", var.sandboxed_machine_type)[0], "d")
      || endswith(split("-", var.sandboxed_machine_type)[0], "a")
    )
    error_message = "sandbox_class microvm needs nested virtualization, which GCE offers only on Intel machine families such as n2 or c2, not E2, AMD or Arm."
  }
}

# The scenario-runner sets it from the case's sandbox-class.
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
  default     = null
}

# The scenario-runner builds substrate's images and the benchmark's once per
# execution, into its own repository rather than each cluster's.
variable "image_repository" {
  description = "Full resource name of the Artifact Registry repository the cluster's images come from, projects/P/locations/L/repositories/R: substrate's and the benchmark's. The nodes and atelet are granted to pull from it; null for none."
  type        = string
  default     = null
}

# automation-infra's Cloud Run job sets it, so that the job only has to act as
# one account, which its deployment created, rather than create one per run
# and grant it project roles.
variable "node_service_account" {
  description = "Email of an existing service account for the node pools, which holds the project roles GKE nodes need (logging.logWriter, monitoring.metricWriter, monitoring.viewer, stackdriver.resourceMetadata.writer) already. Empty creates one for this deployment and grants it those."
  type        = string
  default     = ""
}

variable "sandboxed_node_count" {
  description = "Initial node count of the sandboxed node pool."
  type        = number
  default     = 1

  validation {
    condition = (
      coalesce(var.sandboxed_min_node_count, var.sandboxed_node_count) <= var.sandboxed_node_count
      && var.sandboxed_node_count <= coalesce(var.sandboxed_max_node_count, var.sandboxed_node_count)
    )
    error_message = "sandboxed_node_count must be between sandboxed_min_node_count and sandboxed_max_node_count."
  }
}

# Autoscaling bounds. Unset pins the pool at sandboxed_node_count, so runs
# compare the same hardware.
variable "sandboxed_min_node_count" {
  description = "Autoscaling minimum of the sandboxed node pool. Defaults to var.sandboxed_node_count."
  type        = number
  default     = null
}

variable "sandboxed_max_node_count" {
  description = "Autoscaling maximum of the sandboxed node pool. Defaults to var.sandboxed_node_count."
  type        = number
  default     = null
}

# Pinned to var.zone like the rest, and without the substrate version label the
# pools substrate runs on get, so give one a taint unless substrate's workers
# are meant to land there.
variable "additional_node_pools" {
  description = "Node pools to create besides default, sandboxed and monitoring. name is the pool's name."
  type = list(object({
    name         = string
    machine_type = optional(string, "e2-standard-4")
    disk_size_gb = optional(number, 100)
    disk_type    = optional(string, "pd-balanced")
    node_count   = optional(number, 1)
    # Unset pins the pool at node_count.
    min_node_count = optional(number)
    max_node_count = optional(number)
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

  validation {
    condition = alltrue([
      for pool in var.additional_node_pools :
      coalesce(pool.min_node_count, pool.node_count) <= pool.node_count && pool.node_count <= coalesce(pool.max_node_count, pool.node_count)
    ])
    error_message = "Each of additional_node_pools needs node_count between its min_node_count and max_node_count."
  }
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
  description = "Grants atelet project-level storage and Artifact Registry roles, reaching every bucket and repository in the project. Off by default and rarely wanted: the grants in main.tf on this deployment's bucket and on var.image_repository cover atelet, and the nodes have their own service account rather than the project's default one. Turn this on only for a deployment that genuinely has to read or write outside what this config created. See cmd/iam.go and tools/setup-gcp/README.md for the authoritative list."
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
