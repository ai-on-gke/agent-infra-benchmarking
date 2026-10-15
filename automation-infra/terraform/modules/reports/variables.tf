variable "project_id" {
  description = "GCP project ID of the BigQuery dataset. Its clusters' benchmark-runner KSA may write the reports."
  type        = string
}

variable "dataset_id" {
  description = "ID of the BigQuery dataset of the reports' views."
  type        = string
}

variable "bucket" {
  description = "Existing bucket the cases' reports go to, under prefix, and the dataset reads them from."
  type        = string
}

variable "prefix" {
  description = "Prefix in bucket the reports go under, as <prefix>/<run-id>/<record>/. Each case's benchmark-runner KSA may only write there."
  type        = string
  default     = "data"

  validation {
    condition     = can(regex("^[^/]+(/[^/]+)*$", var.prefix))
    error_message = "prefix must not be empty, nor start or end with a slash."
  }
}

variable "writer_project_ids" {
  description = "Other projects whose clusters' benchmark-runner KSA may write the reports too, as the cluster of a local or dev run in another project."
  type        = list(string)
  default     = []
}
