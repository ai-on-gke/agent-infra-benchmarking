locals {
  bucket_name = var.bucket_name != "" ? var.bucket_name : var.default_resource_name

  bucket_location = var.bucket_location != "" ? var.bucket_location : var.region
}

# The APIs everything below needs. Never disabled on destroy: the project is
# shared, and other deployments in it use the same APIs. Already-enabled APIs
# only need listing, so this works without the permission to enable one.
resource "google_project_service" "this" {
  for_each = var.enable_apis ? toset([
    "artifactregistry.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudtrace.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "storage.googleapis.com",
    "telemetry.googleapis.com",
  ]) : toset([])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

locals {
  # Substrate's dataplane schedules only onto nodes carrying this label, and
  # hack/install-ate.sh stamps it on the nodes that exist when it runs. Setting
  # it on the pools instead covers the nodes GKE adds afterwards — autoscaling,
  # auto-repair, an upgrade — which are born with their pool's labels and would
  # otherwise host no workers.
  #
  # Every pool substrate runs on gets it, which is what the installer does to
  # every node. Bumping the submodule therefore changes a node label, and GKE
  # rolls the pool's nodes to apply it; that is the same node-by-node move the
  # version is there to describe, so it belongs in the plan rather than out of
  # it. The monitoring pool goes without, so a bump leaves Prometheus running.
  substrate_version_label = var.substrate_version != null && var.substrate_version != "" ? { "ate.dev/substrate-version" = var.substrate_version } : {}
}

module "cluster" {
  source = "./modules/gke-cluster"

  project_id = var.project_id
  region     = var.region
  location   = var.regional_control_plane ? var.region : var.zone

  cluster_name = var.default_resource_name

  enable_private_nodes       = var.enable_private_nodes
  enable_private_endpoint    = var.enable_private_endpoint
  master_authorized_networks = local.master_authorized_networks

  network             = local.network
  subnetwork          = local.subnetwork
  pods_range_name     = local.pods_range_name
  services_range_name = local.services_range_name
  kubernetes_version  = var.kubernetes_version
  release_channel     = "REGULAR"
  block_upgrades      = true

  # Without these, substrate's podcertificate-controller never creates the
  # ClusterTrustBundles and hack/install-ate.sh waits for them forever.
  k8s_beta_apis = [
    "certificates.k8s.io/v1beta1/clustertrustbundles",
    "certificates.k8s.io/v1beta1/podcertificaterequests",
  ]

  # Substrate's install, the benchmark Job and the actors all export to the
  # managed collector; without it every one of them logs a failed export.
  # Substrate's own tools/setup-gcp enables it too.
  managed_otel_scope = "COLLECTION_AND_INSTRUMENTATION_COMPONENTS"

  # Every pool leaves auto_upgrade off: an upgrade drains the workers, which
  # crashes the actors still awake on them; see substrate's
  # tools/setup-gcp/README.md.
  node_pools = merge({
    default = {
      machine_type   = var.default_machine_type
      node_count     = 1
      min_node_count = 1
      max_node_count = 1
      node_locations = [var.zone]
      labels         = local.substrate_version_label
    }
    sandboxed = {
      machine_type   = var.sandboxed_machine_type
      node_count     = var.sandboxed_node_count
      min_node_count = var.sandboxed_min_node_count
      max_node_count = var.sandboxed_max_node_count
      node_locations = [var.zone]
      labels         = merge(local.substrate_version_label, { "benchmarking-worker" = "true" })
      # The microvm guests boot on /dev/kvm.
      enable_nested_virtualization = var.sandbox_class == "microvm"
      # worker-pool.yaml.tmpl's WorkerPool already tolerates this taint; without
      # it actually applied to the nodes, nothing stops substrate's own
      # control-plane pods (and anything else) from also landing here
      # alongside the workers, which is how a 100-worker run blows past GKE's
      # 110-pods-per-node cap on this single pinned node.
      taints = [{
        key    = "ate.dev/sandboxClass"
        value  = var.sandbox_class
        effect = "NO_SCHEDULE"
      }]
    }
    # Prometheus alone, so it neither takes from what it measures nor gets
    # starved by a heavy run. The taint keeps atelet off, which tolerates just
    # the sandbox taint.
    # Pinned to var.zone with the rest, since its data volume is a zonal disk.
    monitoring = {
      machine_type   = "e2-standard-4"
      node_count     = 1
      min_node_count = 1
      max_node_count = 1
      node_locations = [var.zone]
      labels         = { "benchmarking-monitoring" = "true" }
      taints = [{
        key    = "benchmarking-monitoring"
        value  = "true"
        effect = "NO_SCHEDULE"
      }]
    }
    }, {
    for pool in var.additional_node_pools : pool.name => {
      machine_type   = pool.machine_type
      disk_size_gb   = pool.disk_size_gb
      disk_type      = pool.disk_type
      node_count     = pool.node_count
      min_node_count = pool.min_node_count
      max_node_count = pool.max_node_count
      spot           = pool.spot
      node_locations = [var.zone]
      labels         = pool.labels
      taints         = pool.taints
    }
  })

  node_service_account = local.node_service_account

  depends_on = [google_project_service.this]
}

# ---------------------------------------------------------------------------
# Storage bucket
# ---------------------------------------------------------------------------

resource "google_storage_bucket" "this" {
  project  = var.project_id
  name     = local.bucket_name
  location = local.bucket_location

  storage_class               = var.bucket_storage_class
  force_destroy               = var.bucket_force_destroy
  uniform_bucket_level_access = true

  # Substrate writes a snapshot on every suspend and deletes the ones nothing
  # refers to, so with GCS's default seven days of soft delete most of what
  # the bucket bills for is snapshots already deleted.
  soft_delete_policy {
    retention_duration_seconds = var.bucket_soft_delete_retention_seconds
  }

  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }

  dynamic "lifecycle_rule" {
    for_each = var.report_retention_days > 0 ? [var.report_retention_days] : []
    content {
      condition {
        age            = lifecycle_rule.value
        matches_prefix = ["reports/"]
      }
      action {
        type = "Delete"
      }
    }
  }

  depends_on = [google_project_service.this]
}

# ---------------------------------------------------------------------------
# atelet IAM
# ---------------------------------------------------------------------------

data "google_project" "this" {
  project_id = var.project_id

  depends_on = [google_project_service.this]
}


locals {
  atelet_wi_principal           = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${var.atelet_namespace}/sa/${var.atelet_ksa_name}"
  ate_api_server_wi_principal   = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${var.atelet_namespace}/sa/${var.ate_api_server_ksa_name}"
  benchmark_runner_wi_principal = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${var.benchmark_runner_namespace}/sa/${var.benchmark_runner_ksa_name}"
}

resource "google_project_iam_member" "atelet_storage_object_admin" {
  count   = var.enable_atelet_iam ? 1 : 0
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = local.atelet_wi_principal

  depends_on = [google_project_service.this]
}

resource "google_project_iam_member" "atelet_artifact_registry_reader" {
  count   = var.enable_atelet_iam ? 1 : 0
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = local.atelet_wi_principal

  depends_on = [google_project_service.this]
}

resource "google_project_iam_member" "atelet_artifact_registry_writer" {
  count   = var.enable_atelet_iam ? 1 : 0
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = local.atelet_wi_principal

  depends_on = [google_project_service.this]
}

resource "google_storage_bucket_iam_member" "atelet_bucket_object_admin" {
  count  = var.enable_atelet_bucket_iam ? 1 : 0
  bucket = google_storage_bucket.this.name
  role   = "roles/storage.objectAdmin"
  member = local.atelet_wi_principal
}

resource "google_storage_bucket_iam_member" "atelet_bucket_viewer" {
  count  = var.enable_atelet_bucket_iam ? 1 : 0
  bucket = google_storage_bucket.this.name
  role   = "roles/storage.bucketViewer"
  member = local.atelet_wi_principal
}

# ate-api-server is a separate KSA from atelet (though typically in the same
# namespace) - it also needs bucket access for actor SuspendActor/ResumeActor
# calls, but no project-level grants.
resource "google_storage_bucket_iam_member" "ate_api_server_bucket_object_admin" {
  count  = var.enable_atelet_bucket_iam ? 1 : 0
  bucket = google_storage_bucket.this.name
  role   = "roles/storage.objectAdmin"
  member = local.ate_api_server_wi_principal
}

resource "google_storage_bucket_iam_member" "ate_api_server_bucket_viewer" {
  count  = var.enable_atelet_bucket_iam ? 1 : 0
  bucket = google_storage_bucket.this.name
  role   = "roles/storage.bucketViewer"
  member = local.ate_api_server_wi_principal
}

locals {
  node_service_account = var.node_service_account != "" ? var.node_service_account : google_service_account.node[0].email
}

moved {
  from = google_service_account.node
  to   = google_service_account.node[0]
}

resource "google_service_account" "node" {
  count = var.node_service_account == "" ? 1 : 0

  project      = var.project_id
  account_id   = var.short_resource_name != "" ? "${var.short_resource_name}-node" : substr("${var.default_resource_name}-node", 0, 30)
  display_name = "GKE nodes for ${var.default_resource_name}"
  description  = "Runs the node pools of the ${var.default_resource_name} deployment's cluster. Holds the minimum GKE requires, plus read on this deployment's snapshot bucket and the runner's image repository."

  depends_on = [google_project_service.this]
}

# An existing var.node_service_account holds these already.
resource "google_project_iam_member" "node_minimal" {
  for_each = var.node_service_account != "" ? toset([]) : toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${local.node_service_account}"

  depends_on = [google_project_service.this]
}

resource "google_storage_bucket_iam_member" "node_bucket_viewer" {
  bucket = google_storage_bucket.this.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${local.node_service_account}"
}

locals {
  # projects/P/locations/L/repositories/R
  image_repository = var.image_repository == null ? null : split("/", var.image_repository)
}

moved {
  from = google_artifact_registry_repository_iam_member.substrate_reader
  to   = google_artifact_registry_repository_iam_member.image_reader
}

moved {
  from = google_artifact_registry_repository_iam_member.image_reader["node"]
  to   = google_artifact_registry_repository_iam_member.node_image_reader[0]
}

# atelet's read on the repository, which the repository's owner grants now:
# atelet's principal is the same for every cluster in the project, so one
# cluster's destroy would take it from the others sharing the repository.
removed {
  from = google_artifact_registry_repository_iam_member.image_reader

  lifecycle {
    destroy = false
  }
}

# Pulls of substrate's images and the benchmark's by the nodes for their pods,
# for an account of this deployment's own; an existing var.node_service_account
# is shared with other deployments, so the repository's owner grants it.
resource "google_artifact_registry_repository_iam_member" "node_image_reader" {
  count = var.image_repository != null && var.node_service_account == "" ? 1 : 0

  project    = local.image_repository[1]
  location   = local.image_repository[3]
  repository = local.image_repository[5]
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${local.node_service_account}"
}

resource "google_storage_bucket_iam_member" "benchmark_runner_object_creator" {
  bucket = google_storage_bucket.this.name
  role   = "roles/storage.objectCreator"
  member = local.benchmark_runner_wi_principal
}

# The binding names a principal by namespace and ServiceAccount, so the
# account has to exist under exactly those names for it to grant anything.
# benchmarking/locust/manifests/worker-job.yaml creates it; the two have to agree on
# benchmark_runner_namespace and benchmark_runner_ksa_name.
