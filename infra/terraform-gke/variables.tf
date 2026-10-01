variable "project_id" {
  description = "GCP project ID to deploy resources into."
  type        = string
}

variable "region" {
  description = "Default region for the cluster, the Artifact Registry repo and the bucket. The cluster can override it."
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

variable "cluster" {
  description = "The GKE cluster to deploy. Named var.default_resource_name unless `name` says otherwise."
  type = object({
    # Cluster name. Empty takes var.default_resource_name, the same way the
    # bucket and the Artifact Registry repository do.
    name = optional(string, "")

    region              = optional(string)
    autopilot           = optional(bool, false)
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
    # On by default because substrate's install, the benchmark Job and the
    # actors all export to the managed collector; without it every one of them
    # logs a failed export. Substrate's own tools/setup-gcp enables it too.
    managed_otel_scope = optional(string, "COLLECTION_AND_INSTRUMENTATION_COMPONENTS")

    # Only used when autopilot = false.
    node_pools = optional(map(object({
      machine_type   = optional(string, "e2-medium")
      disk_size_gb   = optional(number, 100)
      disk_type      = optional(string, "pd-balanced")
      node_count     = optional(number, 1)
      min_node_count = optional(number, 1)
      max_node_count = optional(number, 3)
      spot           = optional(bool, false)
      labels         = optional(map(string), {})
      # Node version, pinned rather than following the control plane.
      version = optional(string, "1.35.8-gke.1225000")
      # auto_upgrade has to stay true on a cluster in a release channel, and
      # moves nodes off `version` when one is set.
      auto_upgrade = optional(bool, false)
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
      # Exposes /dev/kvm on the pool's nodes, which the microvm (kata +
      # cloud-hypervisor) sandbox class needs to boot a guest. Not supported on
      # E2 machine types; pick n2/c2/c3 or similar. Independent of
      # sandbox_gvisor - a microvm pool is not a gVisor pool.
      enable_nested_virtualization = optional(bool, false)
    })), { default = {} })
  })
  default = {}
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

