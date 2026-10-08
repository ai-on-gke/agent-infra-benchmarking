terraform {
  required_version = ">= 1.5"

  # The runner gives terraform init the bucket and the execution's prefix.
  backend "gcs" {}

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
