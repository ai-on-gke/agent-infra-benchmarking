locals {
  bucket_name     = var.bucket_name != "" ? var.bucket_name : "${var.project_id}-bucket"
  bucket_location = var.bucket_location != "" ? var.bucket_location : var.region
}

module "clusters" {
  source = "./modules/gke-cluster"

  for_each = var.clusters

  project_id = var.project_id
  region     = coalesce(each.value.region, var.region)

  cluster_name = each.key

  autopilot            = each.value.autopilot
  network              = each.value.network
  subnetwork           = each.value.subnetwork
  enable_dataplane_v2  = each.value.enable_dataplane_v2
  node_pools           = each.value.node_pools
  k8s_beta_apis        = each.value.k8s_beta_apis
  enable_pod_snapshots = each.value.enable_pod_snapshots
  enable_agent_sandbox = each.value.enable_agent_sandbox
  managed_otel_scope   = each.value.managed_otel_scope
}

# ---------------------------------------------------------------------------
# Artifact Registry
#
# Shared across all clusters in var.clusters.
# ---------------------------------------------------------------------------

resource "google_artifact_registry_repository" "this" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_repository_id
  format        = var.artifact_repository_format
}

# ---------------------------------------------------------------------------
# Storage bucket
#
# Shared across all clusters in var.clusters.
# ---------------------------------------------------------------------------

resource "google_storage_bucket" "this" {
  project  = var.project_id
  name     = local.bucket_name
  location = local.bucket_location



  storage_class               = var.bucket_storage_class
  force_destroy               = var.bucket_force_destroy
  uniform_bucket_level_access = true
}

# ---------------------------------------------------------------------------
# atelet IAM
# ---------------------------------------------------------------------------

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # Keyed to the project number, not the ID. Namespace-pinned: there is no
  # "match by KSA name across all namespaces" IAM selector for Workload
  # Identity Federation principals - each binding has to name the exact
  # namespace/KSA pair.
  #
  # These are four separate KSAs/components, each with its own principal -
  # don't collapse them into one, even if they happen to share a namespace
  # today. atelet and ate-api-server both live in var.atelet_namespace but
  # are still distinct KSAs.
  atelet_wi_principal           = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${var.atelet_namespace}/sa/${var.atelet_ksa_name}"
  ate_api_server_wi_principal   = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${var.atelet_namespace}/sa/${var.ate_api_server_ksa_name}"
  orchestrator_wi_principal     = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${var.orchestrator_namespace}/sa/${var.orchestrator_ksa_name}"
  benchmark_runner_wi_principal = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${var.benchmark_runner_namespace}/sa/${var.benchmark_runner_ksa_name}"
  compute_default_sa            = "serviceAccount:${data.google_project.this.number}-compute@developer.gserviceaccount.com"
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
  count  = var.enable_atelet_iam ? 1 : 0
  bucket = google_storage_bucket.this.name
  role   = "roles/storage.objectAdmin"
  member = local.atelet_wi_principal
}

resource "google_storage_bucket_iam_member" "atelet_bucket_viewer" {
  count  = var.enable_atelet_iam ? 1 : 0
  bucket = google_storage_bucket.this.name
  role   = "roles/storage.bucketViewer"
  member = local.atelet_wi_principal
}

# ate-api-server is a separate KSA from atelet (though typically in the same
# namespace) - it also needs bucket access for actor SuspendActor/ResumeActor
# calls, but no project-level grants.
resource "google_storage_bucket_iam_member" "ate_api_server_bucket_object_admin" {
  count  = var.enable_atelet_iam ? 1 : 0
  bucket = google_storage_bucket.this.name
  role   = "roles/storage.objectAdmin"
  member = local.ate_api_server_wi_principal
}

resource "google_storage_bucket_iam_member" "ate_api_server_bucket_viewer" {
  count  = var.enable_atelet_iam ? 1 : 0
  bucket = google_storage_bucket.this.name
  role   = "roles/storage.bucketViewer"
  member = local.ate_api_server_wi_principal
}

resource "google_project_iam_member" "atelet_node_storage_object_viewer" {
  count   = var.enable_atelet_iam ? 1 : 0
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = local.compute_default_sa
}

resource "google_project_iam_member" "atelet_node_artifact_registry_reader" {
  count   = var.enable_atelet_iam ? 1 : 0
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = local.compute_default_sa
}

# Grants the orchestrator identity (typically a distinct KSA from atelet
# itself, e.g. a benchmark-orchestrator running in its own namespace) full
# GCP-level control over every cluster in the project (create/delete/update
# clusters and node pools, fetch kubeconfig, etc.), e.g. to orchestrate
# workload clusters from an orchestrator cluster. This is IAM-level control
# only - it does not by itself grant any Kubernetes RBAC inside the
# clusters, see the ClusterRoleBinding below for that.
resource "google_project_iam_member" "orchestrator_cluster_admin" {
  count   = var.enable_atelet_cluster_admin ? 1 : 0
  project = var.project_id
  role    = "roles/container.admin"
  member  = local.orchestrator_wi_principal
}

# The orchestrator builds and pushes test images at runtime (e.g.
# testtypes/locust.py's build_and_push), including to the legacy
# gcr.io-compatibility Artifact Registry repo
# (projects/PROJECT/locations/us/repositories/gcr.io) that images tagged
# gcr.io/PROJECT/... resolve to - a separate repo from google_artifact_registry_repository.this.
# Pulling from it only needs reader access (granted implicitly/elsewhere);
# pushing needs uploadArtifacts, hence writer here.
resource "google_project_iam_member" "orchestrator_artifact_registry_writer" {
  count   = var.enable_atelet_cluster_admin ? 1 : 0
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = local.orchestrator_wi_principal
}

# Grants the orchestrator identity cluster-admin on every cluster's
# Kubernetes API, not just IAM-level kubeconfig access above. GKE maps a
# caller's IAM/Workload Identity Federation principal straight to an RBAC
# "User" of the same name, so this is a plain ClusterRoleBinding - no
# third-party identity setup needed on the target clusters.
#
# Terraform's kubernetes provider can't target a dynamically-sized set of
# cluster endpoints (provider configs aren't for_each-able), so this shells
# out instead. Requires gcloud and kubectl (plus the gke-gcloud-auth-plugin)
# on the machine running `terraform apply`, authenticated as a principal
# with enough IAM/RBAC on each target cluster to create the binding.
resource "null_resource" "orchestrator_cluster_admin_rbac" {
  for_each = var.enable_atelet_cluster_admin ? var.clusters : {}

  triggers = {
    cluster_name = each.key
    region       = coalesce(each.value.region, var.region)
    project_id   = var.project_id
    principal    = local.orchestrator_wi_principal
    # Forces re-provisioning if the cluster is replaced.
    cluster_endpoint = module.clusters[each.key].cluster_endpoint
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -euo pipefail
      gcloud container clusters get-credentials "${self.triggers.cluster_name}" \
        --region "${self.triggers.region}" --project "${self.triggers.project_id}"
      cat <<'YAML' | kubectl apply -f -
      apiVersion: rbac.authorization.k8s.io/v1
      kind: ClusterRoleBinding
      metadata:
        name: orchestrator-cluster-admin
      subjects:
      - kind: User
        name: ${self.triggers.principal}
        apiGroup: rbac.authorization.k8s.io
      roleRef:
        kind: ClusterRole
        name: cluster-admin
        apiGroup: rbac.authorization.k8s.io
      YAML
    EOT
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      set -euo pipefail
      gcloud container clusters get-credentials "${self.triggers.cluster_name}" \
        --region "${self.triggers.region}" --project "${self.triggers.project_id}"
      kubectl delete clusterrolebinding orchestrator-cluster-admin --ignore-not-found
    EOT
  }
}

# ---------------------------------------------------------------------------
# benchmark-runner IAM
#
# The automation runner Job's identity - only needs to write results, not
# read/list/manage them.
# ---------------------------------------------------------------------------

resource "google_storage_bucket_iam_member" "benchmark_runner_object_creator" {
  count  = var.enable_benchmark_runner_iam ? 1 : 0
  bucket = google_storage_bucket.this.name
  role   = "roles/storage.objectCreator"
  member = local.benchmark_runner_wi_principal
}
