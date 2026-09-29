locals {
  bucket_name            = var.bucket_name != "" ? var.bucket_name : var.default_resource_name
  artifact_repository_id = var.artifact_repository_id != "" ? var.artifact_repository_id : var.default_resource_name

  bucket_location = var.bucket_location != "" ? var.bucket_location : var.region
  cluster_name    = var.cluster.name != "" ? var.cluster.name : var.default_resource_name
}

# The build version of the substrate submodule, read from its git checkout by
# substrate-version.sh rather than written into terraform.tfvars, so bumping
# the submodule is the only step in moving to another version.
data "external" "substrate_version" {
  program = ["${path.module}/substrate-version.sh"]
}

locals {
  substrate_version = data.external.substrate_version.result.version

  # Substrate's dataplane schedules only onto nodes carrying this label, and
  # hack/install-ate.sh stamps it on the nodes that exist when it runs. Setting
  # it on the pools instead covers the nodes GKE adds afterwards — autoscaling,
  # auto-repair, an upgrade — which are born with their pool's labels and would
  # otherwise host no workers.
  #
  # Every pool gets it, which is what the installer does to every node. Bumping
  # the submodule therefore changes a node label, and GKE rolls the pool's nodes
  # to apply it; that is the same node-by-node move the version is there to
  # describe, so it belongs in the plan rather than out of it.
  node_pools = {
    for key, pool in var.cluster.node_pools : key => merge(pool, {
      labels = merge(pool.labels, {
        "ate.dev/substrate-version" = local.substrate_version
      })
    })
  }
}

module "cluster" {
  source = "./modules/gke-cluster"

  project_id = var.project_id
  region     = coalesce(var.cluster.region, var.region)

  cluster_name = local.cluster_name

  autopilot            = var.cluster.autopilot
  network              = var.cluster.network
  subnetwork           = var.cluster.subnetwork
  enable_dataplane_v2  = var.cluster.enable_dataplane_v2
  node_pools           = local.node_pools
  kubernetes_version   = var.cluster.kubernetes_version
  release_channel      = var.cluster.release_channel
  k8s_beta_apis        = var.cluster.k8s_beta_apis
  enable_pod_snapshots = var.cluster.enable_pod_snapshots
  enable_agent_sandbox = var.cluster.enable_agent_sandbox
  managed_otel_scope   = var.cluster.managed_otel_scope

  node_service_account = google_service_account.node.email
}

# ---------------------------------------------------------------------------
# Artifact Registry
# ---------------------------------------------------------------------------

resource "google_artifact_registry_repository" "this" {
  project       = var.project_id
  location      = var.artifact_repository_location
  repository_id = local.artifact_repository_id
  format        = var.artifact_repository_format
}

# ---------------------------------------------------------------------------
# Storage bucket
# ---------------------------------------------------------------------------

resource "google_storage_bucket" "this" {
  project  = var.project_id
  name     = local.bucket_name
  location = local.bucket_location



  storage_class               = var.bucket_storage_class
  force_destroy               = true
  uniform_bucket_level_access = true
}

# ---------------------------------------------------------------------------
# atelet IAM
# ---------------------------------------------------------------------------

data "google_project" "this" {
  project_id = var.project_id
}


locals {
  atelet_wi_principal         = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${var.atelet_namespace}/sa/${var.atelet_ksa_name}"
  ate_api_server_wi_principal = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${var.atelet_namespace}/sa/${var.ate_api_server_ksa_name}"
  # Disabled along with the orchestrator-cluster resources below.
  # orchestrator_wi_principal     = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${var.orchestrator_namespace}/sa/${var.orchestrator_ksa_name}"
  benchmark_runner_wi_principal = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${var.benchmark_runner_namespace}/sa/${var.benchmark_runner_ksa_name}"
}

resource "google_project_iam_member" "atelet_storage_object_admin" {
  count   = var.enable_atelet_iam ? 1 : 0
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = local.atelet_wi_principal
}

resource "google_project_iam_member" "atelet_artifact_registry_reader" {
  count   = var.enable_atelet_iam ? 1 : 0
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = local.atelet_wi_principal
}

resource "google_project_iam_member" "atelet_artifact_registry_writer" {
  count   = var.enable_atelet_iam ? 1 : 0
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = local.atelet_wi_principal
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

resource "google_service_account" "node" {
  project      = var.project_id
  account_id   = substr("${var.default_resource_name}-node", 0, 30)
  display_name = "GKE nodes for ${var.default_resource_name}"
  description  = "Runs the node pools of the ${var.default_resource_name} deployment's cluster. Holds the minimum GKE requires, plus read on this deployment's Artifact Registry repository and snapshot bucket."
}

resource "google_project_iam_member" "node_minimal" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.node.email}"
}

# Image pulls for every pod the nodes run.
resource "google_artifact_registry_repository_iam_member" "node_reader" {
  project    = var.project_id
  location   = google_artifact_registry_repository.this.location
  repository = google_artifact_registry_repository.this.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.node.email}"
}

resource "google_storage_bucket_iam_member" "node_bucket_viewer" {
  bucket = google_storage_bucket.this.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.node.email}"
}

# atelet pulls the image an ActorTemplate names and pushes what it builds.
# Unconditional and repository-scoped, unlike the project-level pair under
# enable_atelet_iam above, which is the broad form kept for a deployment that
# has to grant across every repository in its project.
resource "google_artifact_registry_repository_iam_member" "atelet_reader" {
  project    = var.project_id
  location   = google_artifact_registry_repository.this.location
  repository = google_artifact_registry_repository.this.name
  role       = "roles/artifactregistry.reader"
  member     = local.atelet_wi_principal
}

resource "google_artifact_registry_repository_iam_member" "atelet_writer" {
  project    = var.project_id
  location   = google_artifact_registry_repository.this.location
  repository = google_artifact_registry_repository.this.name
  role       = "roles/artifactregistry.writer"
  member     = local.atelet_wi_principal
}

resource "google_storage_bucket_iam_member" "benchmark_runner_object_creator" {
  bucket = google_storage_bucket.this.name
  role   = "roles/storage.objectCreator"
  member = local.benchmark_runner_wi_principal
}

# The binding names a principal by namespace and ServiceAccount, so the
# account has to exist under exactly those names for it to grant anything.
# benchmarking/locust/manifests/job.yaml creates it; the two have to agree on
# benchmark_runner_namespace and benchmark_runner_ksa_name.
