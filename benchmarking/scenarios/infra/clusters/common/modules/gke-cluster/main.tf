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

  min_master_version = var.kubernetes_version != "" ? var.kubernetes_version : null

  dynamic "release_channel" {
    for_each = var.release_channel != "" ? [var.release_channel] : []
    content {
      channel = release_channel.value
    }
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pods_range_name
    services_secondary_range_name = var.services_range_name
  }

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

  # SCOPE_UNSPECIFIED is the proto zero value, so the provider serializes it to
  # nothing and the block reaches GKE empty. That is fine in the create request
  # and fatal in an update, which comes back "Must specify a field to update"
  # and fails every apply that touches this cluster. Emitting no block at all is
  # what "leave managed OTel alone" has to look like.
  dynamic "managed_opentelemetry_config" {
    for_each = var.managed_otel_scope == "SCOPE_UNSPECIFIED" ? [] : [1]
    content {
      scope = var.managed_otel_scope
    }
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

  min_master_version = var.kubernetes_version != "" ? var.kubernetes_version : null

  dynamic "release_channel" {
    for_each = var.release_channel != "" ? [var.release_channel] : []
    content {
      channel = release_channel.value
    }
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pods_range_name
    services_secondary_range_name = var.services_range_name
  }

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

  # SCOPE_UNSPECIFIED is the proto zero value, so the provider serializes it to
  # nothing and the block reaches GKE empty. That is fine in the create request
  # and fatal in an update, which comes back "Must specify a field to update"
  # and fails every apply that touches this cluster. Emitting no block at all is
  # what "leave managed OTel alone" has to look like.
  dynamic "managed_opentelemetry_config" {
    for_each = var.managed_otel_scope == "SCOPE_UNSPECIFIED" ? [] : [1]
    content {
      scope = var.managed_otel_scope
    }
  }

  dynamic "enable_k8s_beta_apis" {
    for_each = length(var.k8s_beta_apis) > 0 ? [var.k8s_beta_apis] : []
    content {
      enabled_apis = enable_k8s_beta_apis.value
    }
  }

  # Keeps the cluster on its minor version and its nodes where they are until
  # that version's end of support. GKE still applies control-plane patches, in
  # the daily window, which GKE requires alongside an exclusion.
  dynamic "maintenance_policy" {
    for_each = var.block_upgrades ? [1] : []
    content {
      daily_maintenance_window {
        start_time = "03:00"
      }
      maintenance_exclusion {
        exclusion_name = "pinned-version"
        start_time     = time_static.upgrade_exclusion[0].rfc3339
        exclusion_options {
          scope             = "NO_MINOR_OR_NODE_UPGRADES"
          end_time_behavior = "UNTIL_END_OF_SUPPORT"
        }
      }
    }
  }

  deletion_protection = false

  lifecycle {
    precondition {
      condition     = !var.block_upgrades || contains(["RAPID", "REGULAR", "STABLE", "EXTENDED"], var.release_channel)
      error_message = "block_upgrades needs release_channel set to RAPID, REGULAR, STABLE or EXTENDED: GKE takes this exclusion only on a cluster in a channel."
    }
  }
}

# The exclusion's start, fixed when it is first created so that later plans
# leave it alone.
resource "time_static" "upgrade_exclusion" {
  count = !var.autopilot && var.block_upgrades ? 1 : 0
}

locals {
  cluster_id       = var.autopilot ? google_container_cluster.autopilot[0].id : google_container_cluster.standard[0].id
  cluster_name     = var.autopilot ? google_container_cluster.autopilot[0].name : google_container_cluster.standard[0].name
  cluster_endpoint = var.autopilot ? google_container_cluster.autopilot[0].endpoint : google_container_cluster.standard[0].endpoint
}

resource "google_container_node_pool" "this" {
  for_each = var.autopilot ? {} : var.node_pools

  # Unique only within the cluster, so the cluster's name can take all 40
  # characters a pool's name can.
  name     = each.key
  location = var.region
  project  = var.project_id
  cluster  = local.cluster_name
  version  = each.value.version

  node_locations = length(each.value.node_locations) > 0 ? each.value.node_locations : null

  node_count = each.value.node_count

  autoscaling {
    min_node_count = coalesce(each.value.min_node_count, each.value.node_count)
    max_node_count = coalesce(each.value.max_node_count, each.value.node_count)
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

    # Nested virtualization is what exposes /dev/kvm on the node; without it
    # the microvm (kata + cloud-hypervisor) sandbox class has no hypervisor to
    # boot a guest on. GCE only offers it on non-E2 machine types, so a pool
    # that sets this must also pick an n2/c2/c3 (etc.) machine_type above. A
    # bare_metal pool skips this block entirely: there's no Google hypervisor
    # layer on a "-metal" machine type to begin with, so /dev/kvm is already
    # there and GCE rejects the feature flag on that machine type.
    dynamic "advanced_machine_features" {
      for_each = each.value.enable_nested_virtualization && !each.value.bare_metal ? [1] : []
      content {
        enable_nested_virtualization = true
        # 0 leaves SMT at the machine type's default; the block just needs it
        # set, and nested virtualization is the only feature this pool is after.
        threads_per_core = 0
      }
    }

    service_account = var.node_service_account

    # cloud-platform is the scope Google pairs with a dedicated service
    # account: the scope is the coarse filter and IAM is the fine one, so the
    # roles held by the account above are what a node can actually do.
    # Narrowing the scope is what you do instead when the nodes are stuck on
    # the default account and its project-wide roles.
    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]
  }

  management {
    auto_repair  = each.value.auto_repair
    auto_upgrade = each.value.auto_upgrade
  }
}
