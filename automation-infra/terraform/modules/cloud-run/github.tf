# Lets GitHub Actions push the runner image and the runs' images. They
# authenticate as it through Workload Identity Federation, below, with no key,
# through the repository variables at the end.
resource "google_service_account" "github" {
  count = var.github_actions ? 1 : 0

  project = var.project_id
  # account_id is capped at 30 characters, the same as var.default_resource_name.
  account_id   = "${substr(var.default_resource_name, 0, 24)}-gh"
  display_name = "GitHub Actions of ${var.default_resource_name}"
}

# .github/workflows/runner-image.yml's pushes, build cache and tags, of the
# runner image and of the images the runs pull.
resource "google_artifact_registry_repository_iam_member" "github_writer" {
  count = var.github_actions ? 1 : 0

  project    = var.project_id
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.github[0].email}"
}

# GitHub Actions authenticate as the account above with the OIDC token GitHub
# issues each workflow run, rather than with a key: only runs of
# var.github_repository can.
resource "google_iam_workload_identity_pool" "github" {
  count = var.github_actions ? 1 : 0

  project                   = var.project_id
  workload_identity_pool_id = "${substr(var.default_resource_name, 0, 25)}-gh"
  display_name              = "GitHub Actions"

  depends_on = [google_project_service.services]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count = var.github_actions ? 1 : 0

  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub Actions"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  attribute_condition = "assertion.repository == \"${var.github_repository}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_workload_identity" {
  count = var.github_actions ? 1 : 0

  service_account_id = google_service_account.github[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository/${var.github_repository}"
}

# The variables the workflows authenticate with, named after the deployment's
# folder, which is default_resource_name.
locals {
  github_variable_suffix = upper(replace(var.default_resource_name, "-", "_"))
}

resource "github_actions_variable" "workload_identity_provider" {
  count = var.github_actions ? 1 : 0

  repository    = split("/", var.github_repository)[1]
  variable_name = "GCP_WORKLOAD_IDENTITY_PROVIDER_${local.github_variable_suffix}"
  value         = google_iam_workload_identity_pool_provider.github[0].name
}

resource "github_actions_variable" "service_account" {
  count = var.github_actions ? 1 : 0

  repository    = split("/", var.github_repository)[1]
  variable_name = "GCP_SERVICE_ACCOUNT_${local.github_variable_suffix}"
  value         = google_service_account.github[0].email
}
