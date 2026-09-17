terraform {
  required_version = ">= 1.5"

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
