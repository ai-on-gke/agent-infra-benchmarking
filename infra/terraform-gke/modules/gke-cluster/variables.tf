variable "project_id" {
  description = "GCP project ID to deploy resources into."
  type        = string
}

variable "region" {
  description = "Region for the GKE cluster."
  type        = string
}

variable "cluster_name" {
  description = "Name of the GKE cluster."
  type        = string
}

variable "autopilot" {
  description = "If true, deploy a GKE Autopilot cluster. If false, deploy a Standard cluster with a managed node pool."
  type        = bool
  default     = true
}

variable "network" {
  description = "VPC network to deploy the cluster into."
  type        = string
  default     = "default"
}

variable "subnetwork" {
  description = "Subnetwork to deploy the cluster into."
  type        = string
  default     = "default"
}

variable "enable_dataplane_v2" {
  description = "Enables GKE Dataplane V2 (eBPF-based networking). Ignored for Autopilot clusters, which always use Dataplane V2."
  type        = bool
  default     = false
}

# Opt-in Kubernetes beta API groups (e.g. "certificates.k8s.io/v1beta1/clustertrustbundles").
# GKE only serves beta API groups that are both present in the cluster's
# Kubernetes minor version and explicitly listed here, so the cluster must
# already be running a version that supports the requested APIs.
variable "k8s_beta_apis" {
  description = "Kubernetes beta APIs to enable on the cluster (GKE's enable_k8s_beta_apis)."
  type        = list(string)
  default     = []
}

# Requires Workload Identity (always enabled by this module) and, to actually
# use snapshots on a pod, GKE Sandbox (gVisor) on a non-E2 machine type.
variable "enable_pod_snapshots" {
  description = "Enables the GKE Pod Snapshots addon on the cluster."
  type        = bool
  default     = false
}

# Provides the SandboxTemplate/SandboxClaim CRDs used to request and
# manage gVisor-sandboxed pods (including the ones Pod Snapshots restores
# into). Requires GKE 1.35.2-gke.1269000+ and a node pool with
# sandbox_gvisor = true.
variable "enable_agent_sandbox" {
  description = "Enables the GKE Agent Sandbox addon on the cluster."
  type        = bool
  default     = false
}

# One of "SCOPE_UNSPECIFIED", "NONE", "COLLECTION_AND_INSTRUMENTATION_COMPONENTS".
variable "managed_otel_scope" {
  description = "Scope of the GKE Managed Service for OpenTelemetry pipeline."
  type        = string
  default     = "SCOPE_UNSPECIFIED"

  validation {
    condition     = contains(["SCOPE_UNSPECIFIED", "NONE", "COLLECTION_AND_INSTRUMENTATION_COMPONENTS"], var.managed_otel_scope)
    error_message = "managed_otel_scope must be one of SCOPE_UNSPECIFIED, NONE, COLLECTION_AND_INSTRUMENTATION_COMPONENTS."
  }
}

# Only used when autopilot = false. Keyed map so pools can be added/removed
# independently without shifting other pools (as a list would on index changes).
variable "node_pools" {
  description = "Map of node pools to create (Standard mode only). Key is used as the pool name suffix."
  type = map(object({
    machine_type   = optional(string, "e2-medium")
    disk_size_gb   = optional(number, 100)
    disk_type      = optional(string, "pd-standard")
    node_count     = optional(number, 1)
    min_node_count = optional(number, 1)
    max_node_count = optional(number, 3)
    spot           = optional(bool, false)
    labels         = optional(map(string), {})
    # Zones the pool's nodes are created in, e.g. ["us-central1-a"] to pin a
    # pool to a single zone. Defaults to all zones in var.region when empty.
    node_locations = optional(list(string), [])
    taints = optional(list(object({
      key    = string
      value  = string
      effect = string
    })), [])
    # Enables GKE Sandbox (gVisor) on this pool's nodes. Forces
    # image_type = "COS_CONTAINERD" (the only image type gVisor supports).
    # Only schedules sandboxed pods (those requesting runtimeClassName:
    # gvisor) - keep at least one non-sandboxed pool for GKE-managed system
    # workloads.
    sandbox_gvisor = optional(bool, false)
  }))
  default = {
    default = {}
  }
}
