# ---------------------------------------------------------------------------
# GKE cluster
# ---------------------------------------------------------------------------

# Split into two resources because "remove_default_node_pool" conflicts with
# "enable_autopilot" as soon as it's present in config, even when set to null.
resource "google_container_cluster" "autopilot" {
  count = var.autopilot ? 1 : 0

  # managed_opentelemetry_config is google-beta only for now.
  provider = google-beta

  name     = var.cluster_name
  location = var.region
  project  = var.project_id

  network    = var.network
  subnetwork = var.subnetwork

  enable_autopilot = true

  ip_allocation_policy {}

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  addons_config {
    pod_snapshot_config {
      enabled = var.enable_pod_snapshots
    }

    agent_sandbox_config {
      enabled = var.enable_agent_sandbox
    }
  }

  managed_opentelemetry_config {
    scope = var.managed_otel_scope
  }

  dynamic "enable_k8s_beta_apis" {
    for_each = length(var.k8s_beta_apis) > 0 ? [var.k8s_beta_apis] : []
    content {
      enabled_apis = enable_k8s_beta_apis.value
    }
  }

  deletion_protection = false
}

resource "google_container_cluster" "standard" {
  count = var.autopilot ? 0 : 1

  # managed_opentelemetry_config is google-beta only for now.
  provider = google-beta

  name     = var.cluster_name
  location = var.region
  project  = var.project_id

  network    = var.network
  subnetwork = var.subnetwork

  # Standard clusters manage their own node pool(s) below, so remove the
  # default one created alongside the cluster.
  remove_default_node_pool = true
  initial_node_count       = 1

  datapath_provider = var.enable_dataplane_v2 ? "ADVANCED_DATAPATH" : null

  ip_allocation_policy {}

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  addons_config {
    pod_snapshot_config {
      enabled = var.enable_pod_snapshots
    }

    agent_sandbox_config {
      enabled = var.enable_agent_sandbox
    }
  }

  managed_opentelemetry_config {
    scope = var.managed_otel_scope
  }

  dynamic "enable_k8s_beta_apis" {
    for_each = length(var.k8s_beta_apis) > 0 ? [var.k8s_beta_apis] : []
    content {
      enabled_apis = enable_k8s_beta_apis.value
    }
  }

  deletion_protection = false
}

locals {
  cluster_id       = var.autopilot ? google_container_cluster.autopilot[0].id : google_container_cluster.standard[0].id
  cluster_name     = var.autopilot ? google_container_cluster.autopilot[0].name : google_container_cluster.standard[0].name
  cluster_endpoint = var.autopilot ? google_container_cluster.autopilot[0].endpoint : google_container_cluster.standard[0].endpoint
}

resource "google_container_node_pool" "this" {
  for_each = var.autopilot ? {} : var.node_pools

  name     = "${var.cluster_name}-${each.key}"
  location = var.region
  project  = var.project_id
  cluster  = local.cluster_name

  node_locations = length(each.value.node_locations) > 0 ? each.value.node_locations : null

  node_count = each.value.node_count

  autoscaling {
    min_node_count = each.value.min_node_count
    max_node_count = each.value.max_node_count
  }

  node_config {
    machine_type = each.value.machine_type
    disk_size_gb = each.value.disk_size_gb
    disk_type    = each.value.disk_type
    spot         = each.value.spot
    labels       = each.value.labels

    # gVisor only runs on Container-Optimized OS with containerd.
    image_type = each.value.sandbox_gvisor ? "COS_CONTAINERD" : null

    dynamic "taint" {
      for_each = each.value.taints
      content {
        key    = taint.value.key
        value  = taint.value.value
        effect = taint.value.effect
      }
    }

    dynamic "sandbox_config" {
      for_each = each.value.sandbox_gvisor ? [1] : []
      content {
        type = "GVISOR"
      }
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }
}
