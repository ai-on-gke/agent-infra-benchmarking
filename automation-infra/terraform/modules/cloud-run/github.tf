# Lets GitHub Actions workflows of var.github_actions.repository act as
# google_service_account.github, by GitHub's OIDC token, with no key.
resource "google_iam_workload_identity_pool" "github" {
  count = var.github_actions == null ? 0 : 1

  project = var.project_id
  # A deleted pool's ID stays taken for 30 days.
  workload_identity_pool_id = var.default_resource_name
  display_name              = "GitHub Actions"

  depends_on = [google_project_service.services]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count = var.github_actions == null ? 0 : 1

  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  # Without it, any repository's workflow would be let in.
  attribute_condition = "assertion.repository == '${var.github_actions.repository}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "github" {
  count = var.github_actions == null ? 0 : 1

  project = var.project_id
  # account_id is capped at 30 characters, the same as var.default_resource_name.
  account_id   = "${substr(var.default_resource_name, 0, 24)}-gh"
  display_name = "GitHub Actions of ${var.github_actions.repository}"
}

resource "google_service_account_iam_member" "github_workload_identity_user" {
  count = var.github_actions == null ? 0 : 1

  service_account_id = google_service_account.github[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository/${var.github_actions.repository}"
}

# Only under source/: the bucket may also hold Terraform state. objectUser
# rather than objectCreator, so that a rerun can overwrite its tarball.
resource "google_storage_bucket_iam_member" "github_source" {
  count = var.github_actions == null ? 0 : 1

  bucket = var.github_actions.bucket
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.github[0].email}"

  condition {
    title      = "source-only"
    expression = "resource.name.startsWith(\"projects/_/buckets/${var.github_actions.bucket}/objects/source/\")"
  }
}
