variable "project_id" {
  description = "GCP project ID to deploy resources into."
  type        = string
}

variable "region" {
  description = "Default region for clusters, the Artifact Registry repo and the bucket. Can be overridden per cluster."
  type        = string
  default     = "us-central1"
}

# The name every resource this config creates takes unless something more
# specific is set for it. The bucket and the Artifact Registry repository take
# it as-is; the clusters prefix it onto their var.clusters key, since there can
# be several of those. Change it per deployment to keep two deployments in one
# project from naming the same resources.
#
# It does not reach the project-level IAM bindings below: those are keyed by
# role and principal rather than by name, and the Workload Identity principal
# is identical in every deployment of the project - so keep enable_atelet_iam
# true in at most one deployment per project.
variable "default_resource_name" {
  description = "Name applied to the resources this config creates: the GCS bucket, the Artifact Registry repository, and the prefix of every cluster name. Per-resource variables override it."
  type        = string
}

# Keyed map so clusters can be added/removed independently without shifting
# other clusters (as a list would on index changes). The key is the cluster
# name, prefixed with var.default_resource_name.
variable "clusters" {
  description = "Map of GKE clusters to deploy. Each cluster is named \"<default_resource_name>-<key>\"."
  type = map(object({
    region              = optional(string)
    autopilot           = optional(bool, true)
    network             = optional(string, "default")
    subnetwork          = optional(string, "default")
    enable_dataplane_v2 = optional(bool, false)

    # Minimum control-plane version (GKE's min_master_version), e.g.
    # "1.36.3-gke.1767000". Empty takes the release channel's default, which
    # is how a cluster ends up below what k8s_beta_apis below needs.
    kubernetes_version = optional(string, "")

    # UNSPECIFIED, RAPID, REGULAR, STABLE or EXTENDED. UNSPECIFIED means no
    # channel, which is what holds kubernetes_version exactly; Autopilot
    # clusters reject it. Empty leaves GKE's default.
    release_channel = optional(string, "")

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
      # Node version. Unset follows the control plane.
      version = optional(string)
      # auto_upgrade has to stay true on a cluster in a release channel, and
      # moves nodes off `version` when one is set.
      auto_upgrade = optional(bool, true)
      auto_repair  = optional(bool, true)
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
  description = "ID of the Artifact Registry repository. Defaults to var.default_resource_name when empty. Set it only when that name is already taken in this project."
  type        = string
  default     = ""
}

# The location is the image host: "us" gives us-docker.pkg.dev, "us-central1"
# gives us-central1-docker.pkg.dev. The multi-region default keeps the host the
# one `gcloud auth configure-docker` is usually already set up for; a region
# here needs that command run again for the new host. Changing it replaces the
# repository, dropping every image in it.
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

# ---------------------------------------------------------------------------
# Storage bucket
#
# One bucket shared across all clusters.
# ---------------------------------------------------------------------------

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

# Split from the project-level flag below because the two carry different
# risks. These name this deployment's own bucket, so two deployments in one
# project cannot collide over them, and without them SuspendActor fails with
# "storage.objects.list denied" on the snapshot bucket. Hence: on by default.
variable "enable_atelet_bucket_iam" {
  description = "Grants atelet and ate-api-server objectAdmin + bucketViewer on this deployment's snapshot bucket."
  type        = bool
  default     = true
}

# Project-level, and the Workload Identity principal is identical in every
# deployment of a project, so two deployments managing these fight over one
# binding - a destroy of either revokes it for both. Off by default: turn it on
# in at most one deployment per project.
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

# ---------------------------------------------------------------------------
# Cluster orchestrator IAM - DISABLED
#
# A separate identity from atelet/ate-api-server above - typically a
# benchmark/workload orchestrator running in its own namespace, needing
# full control over every cluster in the project rather than snapshot
# storage access. There is no orchestrator cluster in this setup, so these
# variables are commented out together with the resources they feed in
# main.tf. Uncommenting them here is not enough on its own - the resources
# and local.orchestrator_wi_principal in main.tf have to come back too.
# ---------------------------------------------------------------------------

# # Grants the orchestrator full control over every cluster in the project:
# # GCP-level (roles/container.admin: create/delete/update clusters and node
# # pools, fetch kubeconfig) AND Kubernetes-level (a cluster-admin
# # ClusterRoleBinding applied to each cluster via kubectl). Also grants
# # roles/artifactregistry.writer, since the orchestrator builds and pushes
# # test images at runtime.
# variable "enable_atelet_cluster_admin" {
#   description = "Grants the orchestrator WI principal roles/container.admin and roles/artifactregistry.writer project-wide, plus a cluster-admin RBAC binding on every cluster in var.clusters."
#   type        = bool
#   default     = false
# }

# variable "orchestrator_namespace" {
#   description = "Kubernetes namespace the cluster-orchestrator's ServiceAccount runs in."
#   type        = string
#   default     = "ate-system"
# }

# variable "orchestrator_ksa_name" {
#   description = "Name of the cluster-orchestrator's Kubernetes ServiceAccount."
#   type        = string
#   default     = "atelet"
# }

# ---------------------------------------------------------------------------
# benchmark-runner IAM
#
# The automation runner Job's identity (benchmarking/automation) - only
# needs to write results to the bucket, not read/list/manage them.
# ---------------------------------------------------------------------------

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

