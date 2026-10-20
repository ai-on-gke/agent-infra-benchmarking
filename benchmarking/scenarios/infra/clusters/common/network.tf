# ---------------------------------------------------------------------------
# Dedicated VPC
# ---------------------------------------------------------------------------
#
# A deployment gets its own VPC rather than landing on the project's "default"
# network, so its firewall rules, routes and IP ranges stay isolated from
# whatever else runs in the project. Custom-mode networks start with no
# firewall rules at all (unlike "default", which is pre-seeded with
# default-allow-internal/-ssh/-icmp/-rdp), so those are recreated below scoped
# to just this deployment's ranges.

locals {
  network_name   = var.network_name != "" ? var.network_name : var.default_resource_name
  create_network = var.existing_network == null

  network             = local.create_network ? google_compute_network.this[0].self_link : var.existing_network.network
  subnetwork          = local.create_network ? google_compute_subnetwork.this[0].self_link : var.existing_network.subnetwork
  pods_range_name     = local.create_network ? "pods" : var.existing_network.pods_range_name
  services_range_name = local.create_network ? "services" : var.existing_network.services_range_name
}

# GKE takes an internal endpoint only with authorized networks on; the
# subnetwork's primary range, which holds the nodes and a runner inside the
# network, is one of them.
data "google_compute_subnetwork" "existing" {
  count = var.enable_private_endpoint && !local.create_network ? 1 : 0

  project = local.subnetwork_project
  region  = var.region
  name    = reverse(split("/", local.subnetwork))[0]
}

locals {
  subnetwork_path    = split("/", local.create_network ? "" : var.existing_network.subnetwork)
  subnetwork_project = contains(local.subnetwork_path, "projects") ? local.subnetwork_path[index(local.subnetwork_path, "projects") + 1] : var.project_id
  subnetwork_cidr    = local.create_network ? var.subnet_cidr : one(data.google_compute_subnetwork.existing[*].ip_cidr_range)

  master_authorized_networks = !var.enable_private_endpoint ? var.master_authorized_networks : concat(
    var.master_authorized_networks == null ? [] : var.master_authorized_networks,
    [{ cidr_block = local.subnetwork_cidr, display_name = "cluster subnetwork" }],
  )
}

moved {
  from = google_compute_network.this
  to   = google_compute_network.this[0]
}

moved {
  from = google_compute_subnetwork.this
  to   = google_compute_subnetwork.this[0]
}

moved {
  from = google_compute_firewall.allow_internal
  to   = google_compute_firewall.allow_internal[0]
}

moved {
  from = google_compute_firewall.allow_iap_ssh
  to   = google_compute_firewall.allow_iap_ssh[0]
}

resource "google_compute_network" "this" {
  count = local.create_network ? 1 : 0

  project                 = var.project_id
  name                    = local.network_name
  auto_create_subnetworks = false

  depends_on = [google_project_service.this]
}

resource "google_compute_subnetwork" "this" {
  count = local.create_network ? 1 : 0

  project       = var.project_id
  name          = local.network_name
  network       = google_compute_network.this[0].id
  region        = var.region
  ip_cidr_range = var.subnet_cidr

  # Referenced by name from the cluster's ip_allocation_policy. Declaring them
  # here rather than leaving ip_allocation_policy to auto-allocate keeps them
  # in this subnetwork's state instead of being added to it out-of-band by
  # GKE, which would otherwise fight Terraform over the subnet's secondary
  # ranges on every apply.
  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pods_cidr
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.services_cidr
  }

  # Lets nodes without an external IP still reach Google APIs (Artifact
  # Registry, GCS, logging/monitoring) over Google's internal network.
  private_ip_google_access = true
}

# Node-to-node, pod-to-pod and pod-to-service traffic all cross this firewall
# once nodes and pods live on a custom-mode network instead of "default"'s
# permissive default-allow-internal rule.
resource "google_compute_firewall" "allow_internal" {
  count = local.create_network ? 1 : 0

  project = var.project_id
  name    = "${local.network_name}-allow-internal"
  network = google_compute_network.this[0].id

  direction = "INGRESS"

  allow {
    protocol = "tcp"
    ports    = ["0-65535"]
  }

  allow {
    protocol = "udp"
    ports    = ["0-65535"]
  }

  allow {
    protocol = "icmp"
  }

  source_ranges = [var.subnet_cidr, var.pods_cidr, var.services_cidr]
}

# Lets operators reach nodes with `gcloud compute ssh --tunnel-through-iap`
# without opening SSH to the internet, which is what "default"'s
# default-allow-ssh rule does.
resource "google_compute_firewall" "allow_iap_ssh" {
  count = local.create_network ? 1 : 0

  project = var.project_id
  name    = "${local.network_name}-allow-iap-ssh"
  network = google_compute_network.this[0].id

  direction = "INGRESS"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  # IAP's fixed TCP forwarding range - the only source SSH should ever be
  # allowed from now that the network has no default-allow-ssh rule.
  source_ranges = ["35.235.240.0/20"]
}

# Private nodes have no external IP, so this is how they reach anything outside
# Google's APIs, such as public image registries. Google's APIs themselves go
# through the subnetwork's Private Google Access.
resource "google_compute_router" "nat" {
  count = local.create_network && var.enable_private_nodes ? 1 : 0

  project = var.project_id
  name    = "${local.network_name}-nat"
  region  = var.region
  network = google_compute_network.this[0].id
}

resource "google_compute_router_nat" "this" {
  count = local.create_network && var.enable_private_nodes ? 1 : 0

  project                            = var.project_id
  name                               = "${local.network_name}-nat"
  router                             = google_compute_router.nat[0].name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}
