terraform {
  required_version = ">= 1.5"

  # State in a GCS bucket, so a deployment can be managed by anyone on the
  # project rather than only from the laptop that created it. Partial: the
  # bucket and prefix are given at init, since the bucket this config creates
  # cannot also hold its state. See README.md.
  backend "gcs" {}

  required_providers {
    google = {
      source = "hashicorp/google"
      # >= 7.33.0 for the pod_snapshot_config addon on google_container_cluster.
      version = ">= 7.33.0, < 8.0.0"
    }
    google-beta = {
      source = "hashicorp/google-beta"
      # managed_opentelemetry_config on google_container_cluster is
      # google-beta only for now; not yet in the GA "google" provider.
      version = ">= 7.33.0, < 8.0.0"
    }
    # Runs substrate-version.sh to read the submodule's build version. Nothing
    # in Terraform can ask git, and the alternative is typing the version into
    # terraform.tfvars by hand and keeping it in step with the submodule.
    external = {
      source  = "hashicorp/external"
      version = ">= 2.3.0, < 3.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}
