region = "us-central1"

# Lets terraform destroy delete the bucket, and with it every run's reports
# under reports/. Download any worth keeping first, or set this to false.
bucket_force_destroy = true

sandboxed_machine_type = "c2-standard-16"

# For the microvm sandbox class, the workers can run on a GCE bare metal host
# instead of a VM with nested virtualization - /dev/kvm is native there. Set
# sandboxed_bare_metal = true and a "-metal" sandboxed_machine_type (e.g.
# c3-standard-192-metal). Bare metal stocks out per zone, so expect to retry or
# pick another zone/type.
# sandboxed_bare_metal   = true
# sandboxed_machine_type = "c3-standard-192-metal"
