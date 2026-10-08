# Lets GitHub Actions write under source/ in var.github_actions_bucket. Its
# key is made by automation-infra/scripts/get-gh-actions-gcs-config, outside
# of the state.
resource "google_service_account" "github" {
  count = var.github_actions_bucket == null ? 0 : 1

  project = var.project_id
  # account_id is capped at 30 characters, the same as var.default_resource_name.
  account_id   = "${substr(var.default_resource_name, 0, 24)}-gh"
  display_name = "GitHub Actions of ${var.default_resource_name}"
}

# Only under source/: the bucket may also hold Terraform state. objectUser
# rather than objectCreator, so that a rerun can overwrite its tarball.
resource "google_storage_bucket_iam_member" "github_source" {
  count = var.github_actions_bucket == null ? 0 : 1

  bucket = var.github_actions_bucket
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.github[0].email}"

  condition {
    title      = "source-only"
    expression = "resource.name.startsWith(\"projects/_/buckets/${var.github_actions_bucket}/objects/source/\")"
  }
}
