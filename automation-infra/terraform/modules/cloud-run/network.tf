# The network the runs' clusters go into, and the job reaches them in: each
# cluster has an internal endpoint only (CLUSTER_NETWORK and
# CLUSTER_SUBNETWORK in the job's environment), and its nodes no external IP.
resource "google_compute_network" "runs" {
  project                 = var.project_id
  name                    = var.default_resource_name
  auto_create_subnetworks = false

  depends_on = [google_project_service.services]
}

resource "google_compute_subnetwork" "runs" {
  project       = var.project_id
  name          = var.default_resource_name
  network       = google_compute_network.runs.id
  region        = var.region
  ip_cidr_range = var.network_cidr
  # The nodes reach Google's APIs without external IPs.
  private_ip_google_access = true

  lifecycle {
    # GKE adds each cluster's Pod and Service ranges here, and removes them
    # with the cluster.
    ignore_changes = [secondary_ip_range]
  }
}

# For the private nodes to reach anything outside Google's APIs, such as
# public image registries.
resource "google_compute_router" "runs" {
  project = var.project_id
  name    = var.default_resource_name
  region  = var.region
  network = google_compute_network.runs.id
}

resource "google_compute_router_nat" "runs" {
  project                            = var.project_id
  name                               = var.default_resource_name
  router                             = google_compute_router.runs.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}
