terraform {
  required_version = ">= 1.5"

  # The backend, local or gcs, is in the backend_override.tf the scenario-runner
  # writes.

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.33.0, < 8.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
