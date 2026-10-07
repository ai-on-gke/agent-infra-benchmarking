terraform {
  required_version = ">= 1.5"

  # No backend here, so state is local by default. A backend_override.tf
  # (gitignored) moves it to a GCS bucket instead.

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
