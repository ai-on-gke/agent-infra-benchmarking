# A failed execution of the job or of its cleanup. The job is not retried, so
# without this a failed night goes unnoticed until someone looks.
resource "google_monitoring_notification_channel" "email" {
  for_each = toset(var.alert_emails)

  project      = var.project_id
  display_name = "${var.default_resource_name}: ${each.value}"
  type         = "email"
  labels       = { email_address = each.value }

  depends_on = [google_project_service.services]
}

resource "google_monitoring_alert_policy" "failed_executions" {
  project      = var.project_id
  display_name = "${var.default_resource_name}: failed execution"
  combiner     = "OR"

  conditions {
    display_name = "An execution of ${var.default_resource_name} failed"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_job\"",
        "resource.labels.job_name = starts_with(\"${var.default_resource_name}\")",
        "metric.type = \"run.googleapis.com/job/completed_execution_count\"",
        "metric.labels.result = \"failed\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.job_name"]
      }
    }
  }

  notification_channels = [for channel in google_monitoring_notification_channel.email : channel.id]

  documentation {
    content   = "An execution of the Cloud Run job $${resource.label.job_name} failed. Its logs: gcloud run jobs executions list --job=$${resource.label.job_name} --project=${var.project_id} --region=${var.region}, then gcloud logging read for that execution."
    mime_type = "text/markdown"
  }

  depends_on = [google_project_service.services]
}
