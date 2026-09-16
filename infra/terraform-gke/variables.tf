variable "project_id" {
  description = "GCP project ID to deploy resources into."
  type        = string
}

variable "region" {
  description = "Default region for clusters, the Artifact Registry repo and the bucket. Can be overridden per cluster."
  type        = string
  default     = "us-central1"
}

# Keyed map so clusters can be added/removed independently without shifting
# other clusters (as a list would on index changes). The key is used as the
# cluster name.
variable "clusters" {
  description = "Map of GKE clusters to deploy. Key is used as the cluster name."
  type = map(object({
    region              = optional(string)
    autopilot           = optional(bool, true)
    network             = optional(string, "default")
    subnetwork          = optional(string, "default")
    enable_dataplane_v2 = optional(bool, false)

    # Kubernetes beta APIs to enable, e.g.
    # ["certificates.k8s.io/v1beta1/clustertrustbundles", "certificates.k8s.io/v1beta1/podcertificaterequests"].
    # Requires the cluster to be running a Kubernetes minor version that
    # serves these APIs as beta.
    k8s_beta_apis = optional(list(string), [])

    # Requires Workload Identity (always enabled by this module) and, to
    # actually use snapshots on a pod, GKE Sandbox (gVisor) on a non-E2
    # machine type.
    enable_pod_snapshots = optional(bool, false)

    # Provides the SandboxTemplate/SandboxClaim CRDs used to request and
    # manage gVisor-sandboxed pods. Requires GKE 1.35.2-gke.1269000+ and a
    # node pool with sandbox_gvisor = true.
    enable_agent_sandbox = optional(bool, false)

    # One of "SCOPE_UNSPECIFIED", "NONE", "COLLECTION_AND_INSTRUMENTATION_COMPONENTS".
    managed_otel_scope = optional(string, "SCOPE_UNSPECIFIED")

    # Only used when autopilot = false.
    node_pools = optional(map(object({
      machine_type   = optional(string, "e2-medium")
      disk_size_gb   = optional(number, 100)
      disk_type      = optional(string, "pd-standard")
      node_count     = optional(number, 1)
      min_node_count = optional(number, 1)
      max_node_count = optional(number, 3)
      spot           = optional(bool, false)
      labels         = optional(map(string), {})
      # Zones the pool's nodes are created in, e.g. ["us-central1-a"] to pin
      # a pool to a single zone. Defaults to all zones in the cluster's
      # region when empty.
      node_locations = optional(list(string), [])
      taints = optional(list(object({
        key    = string
        value  = string
        effect = string
      })), [])
      # Enables GKE Sandbox (gVisor) on this pool's nodes. Forces
      # image_type = "COS_CONTAINERD". Keep at least one non-sandboxed
      # pool for GKE-managed system workloads.
      sandbox_gvisor = optional(bool, false)
    })), { default = {} })
  }))
  default = {}
}

# ---------------------------------------------------------------------------
# Artifact Registry
#
# One repository shared across all clusters.
# ---------------------------------------------------------------------------

variable "artifact_repository_id" {
  description = "ID of the Artifact Registry repository."
  type        = string
  default     = "app-repo"
}

variable "artifact_repository_format" {
  description = "Format of the Artifact Registry repository (e.g. DOCKER, NPM, PYTHON)."
  type        = string
  default     = "DOCKER"
}

# ---------------------------------------------------------------------------
# Storage bucket
#
# One bucket shared across all clusters.
# ---------------------------------------------------------------------------

variable "bucket_name" {
  description = "Name of the GCS bucket. Must be globally unique. Defaults to '<project_id>-bucket' if not set."
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
  description = "If true, allows Terraform to delete the bucket even if it contains objects."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# atelet + ate-api-server IAM
#
# atelet and ate-api-server authenticate via GKE Workload Identity
# Federation: each Kubernetes ServiceAccount is addressed directly as an IAM
# principal, no Google service account involved. Requires Workload Identity
# enabled on the cluster they run on (always enabled by this module's
# clusters). Without these bindings, atelet gets 403s from GCS on
# suspend/resume, nodes can't pull images, and ate-api-server can't touch
# actor snapshots (breaking SuspendActor/ResumeActor for glutton-based
# load tests).
# ---------------------------------------------------------------------------

variable "enable_atelet_iam" {
  description = "Grants the IAM bindings atelet, ate-api-server, and their nodes need (project + bucket level). See cmd/iam.go and tools/setup-gcp/README.md for the authoritative list."
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

# ---------------------------------------------------------------------------
# Cluster orchestrator IAM
#
# A separate identity from atelet/ate-api-server above - typically a
# benchmark/workload orchestrator running in its own namespace, needing
# full control over every cluster in the project rather than snapshot
# storage access. Don't reuse atelet_namespace/atelet_ksa_name for this;
# collapsing the two breaks atelet's own bindings if the orchestrator KSA
# is ever renamed.
# ---------------------------------------------------------------------------

# Grants the orchestrator full control over every cluster in the project:
# GCP-level (roles/container.admin: create/delete/update clusters and node
# pools, fetch kubeconfig) AND Kubernetes-level (a cluster-admin
# ClusterRoleBinding applied to each cluster via kubectl). Also grants
# roles/artifactregistry.writer, since the orchestrator builds and pushes
# test images at runtime.
variable "enable_atelet_cluster_admin" {
  description = "Grants the orchestrator WI principal roles/container.admin and roles/artifactregistry.writer project-wide, plus a cluster-admin RBAC binding on every cluster in var.clusters."
  type        = bool
  default     = false
}

variable "orchestrator_namespace" {
  description = "Kubernetes namespace the cluster-orchestrator's ServiceAccount runs in."
  type        = string
  default     = "ate-system"
}

variable "orchestrator_ksa_name" {
  description = "Name of the cluster-orchestrator's Kubernetes ServiceAccount."
  type        = string
  default     = "atelet"
}

# ---------------------------------------------------------------------------
# benchmark-runner IAM
#
# The automation runner Job's identity (benchmarking/automation) - only
# needs to write results to the bucket, not read/list/manage them.
# ---------------------------------------------------------------------------

variable "enable_benchmark_runner_iam" {
  description = "Grants the benchmark-runner WI principal roles/storage.objectCreator on the bucket."
  type        = bool
  default     = false
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
