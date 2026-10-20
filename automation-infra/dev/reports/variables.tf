variable "project_id" {
  description = "GCP project ID of the bucket and the dataset. Its clusters may write the reports."
  type        = string
}

variable "region" {
  description = "Location of the bucket and the dataset."
  type        = string
  default     = "us-central1"
}

variable "name" {
  description = "Name of the deployment: the bucket is <name>-reports, the dataset <name> with underscores for hyphens."
  type        = string
}

variable "writer_project_ids" {
  description = "Other projects whose clusters may write the reports too, as your runs' clusters if they are not in project_id."
  type        = list(string)
  default     = []
}
