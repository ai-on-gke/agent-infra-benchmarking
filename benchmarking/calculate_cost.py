import re
import csv
import subprocess
import sys
import urllib.request
import argparse

def fetch_pricing_yml():
    """Fetches the latest open-source GCP pricing database via HTTP."""
    url = "https://raw.githubusercontent.com/Cyclenerd/google-cloud-pricing-cost-calculator/master/pricing.yml"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as response:
        output = response.read().decode('utf-8')
        return output

def get_machine_cost(pricing_yml, machine_type, region):
    """Parses the YAML string to find the monthly cost for a given machine type and region."""
    # Find the machine_type block
    mt_block = re.search(r'^    ' + re.escape(machine_type) + r':(.*?)(?=\n    \S|\Z)', pricing_yml, re.MULTILINE | re.DOTALL)
    if mt_block:
        # Find the region block inside the machine type
        region_block = re.search(r'^        ' + re.escape(region) + r':(.*?)(?=\n        \S|\Z)', mt_block.group(1), re.MULTILINE | re.DOTALL)
        if region_block:
            # Extract the monthly cost
            month_match = re.search(r'^\s*month:\s*([\d\.]+)', region_block.group(1), re.MULTILINE)
            if month_match:
                return float(month_match.group(1))

    print(f"Warning: Could not find pricing for {machine_type} in {region}")
    return 0.0

def get_tfvars_cost(tfvars_path):
    with open(tfvars_path, 'r') as f:
        content = f.read()

    # Parse region
    region_match = re.search(r'region\s*=\s*"([^"]+)"', content)
    region = region_match.group(1) if region_match else "us-central1"

    pools = []

    # Extract the node_pools block manually
    node_pools_match = re.search(r'node_pools\s*=\s*\{([\s\S]*?)\n\s*\}\n\}', content)
    if node_pools_match:
        pools_content = node_pools_match.group(1)
        # Find all pool blocks (e.g., default = { ... })
        pool_matches = re.finditer(r'(\w+)\s*=\s*\{([^}]*)\}', pools_content)
        for pm in pool_matches:
            pool_body = pm.group(2)

            # extract machine type
            mt_match = re.search(r'machine_type\s*=\s*"([^"]+)"', pool_body)
            if not mt_match: continue
            machine_type = mt_match.group(1)

            # extract node_count
            nc_match = re.search(r'^[ \t]*node_count\s*=\s*(\d+)', pool_body, re.MULTILINE)
            if nc_match:
                node_count = int(nc_match.group(1))
            else:
                # try min_node_count if node_count is commented or missing
                min_nc_match = re.search(r'^[ \t]*min_node_count\s*=\s*(\d+)', pool_body, re.MULTILINE)
                node_count = int(min_nc_match.group(1)) if min_nc_match else 1

            # extract node_locations
            nl_match = re.search(r'^[ \t]*node_locations\s*=\s*\[(.*?)\]', pool_body, re.MULTILINE)
            if nl_match:
                locations_str = nl_match.group(1)
                zones = [z.strip(' "') for z in locations_str.split(',') if z.strip(' "')]
                multiplier = len(zones) if zones else 3
            else:
                multiplier = 3

            total_nodes = node_count * multiplier
            pools.append({
                "pool_name": pm.group(1),
                "machine_type": machine_type,
                "total_nodes": total_nodes
            })

    print(f"Parsed cluster configuration from {tfvars_path}:")
    print(f"  Region: {region}")
    for p in pools:
        print(f"  Pool '{p['pool_name']}' with {p['total_nodes']} x {p['machine_type']}")

    print("Fetching dynamic GCP pricing data via HTTP...")
    pricing_yml = fetch_pricing_yml()

    # GKE standard cluster management fee
    gke_base_monthly = 73.00
    total_cost = gke_base_monthly

    for pool in pools:
        cost_per_node = get_machine_cost(pricing_yml, pool['machine_type'], region)
        pool['cost_per_node'] = cost_per_node
        pool['pool_total_cost'] = cost_per_node * pool['total_nodes']
        total_cost += pool['pool_total_cost']

    return total_cost, pools

def get_benchmark_metrics(gcs_url):
    stats_cmd = f"gsutil cat {gcs_url}/stats.csv"
    history_cmd = f"gsutil cat {gcs_url}/locust_stats_history.csv"

    stats_out = subprocess.check_output(stats_cmd, shell=True, text=True)
    history_out = subprocess.check_output(history_cmd, shell=True, text=True)

    # Parse max users
    max_users = 0
    reader = csv.DictReader(history_out.strip().splitlines())
    for row in reader:
        try:
            users = int(row.get('User Count', 0))
            if users > max_users:
                max_users = users
        except ValueError:
            pass

    # Parse TTFI percentiles
    ttfi_percentiles = {}
    reader = csv.DictReader(stats_out.strip().splitlines())
    for row in reader:
        if row.get('Type') == 'runworkload' and row.get('Name') == 'TTFI':
            for pct in ['50%', '66%', '75%', '80%', '90%', '95%', '98%', '99%', '99.9%', '99.99%', '100%']:
                val = row.get(pct)
                ttfi_percentiles[pct] = float(val) if val else 0.0
            break

    return max_users, ttfi_percentiles

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Calculate cost per agent month for Agent Substrate.")
    parser.add_argument(
        "--gcs_url",
        type=str,
        default="gs://dima-ns/reports/20260928t124446z",
        help="GCS URL of the benchmark reports (default: gs://dima-ns/reports/20260928t124446z)"
    )
    parser.add_argument(
        "--terraform_vars_path",
        type=str,
        default="infra/terraform-gke/terraform.tfvars",
        help="Path to the terraform.tfvars file (default: infra/terraform-gke/terraform.tfvars)"
    )
    parser.add_argument(
        "--output_csv",
        type=str,
        default="cost_analysis.csv",
        help="Path to output the generated CSV report (default: cost_analysis.csv)"
    )

    args = parser.parse_args()

    monthly_cost, pools_info = get_tfvars_cost(args.terraform_vars_path)
    max_users, ttfi_percentiles = get_benchmark_metrics(args.gcs_url)

    # Print to console
    print(f"\n--- Cost Analysis ---")
    print(f"Total Monthly Infra Cost: ${monthly_cost:.2f}")
    print(f"Max Concurrent Users (Agents): {max_users}")
    if max_users > 0:
        print(f"Cost per Agent Month: ${monthly_cost / max_users:.2f}")

    print("\n--- Latency (TTFI) ---")
    for pct, val in ttfi_percentiles.items():
        print(f"  {pct}: {val} ms")

    # Write to CSV
    with open(args.output_csv, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['Category', 'Metric', 'Value'])
        
        # Resources
        for p in pools_info:
            writer.writerow(['Resource', f"Pool: {p['pool_name']} ({p['machine_type']})", f"{p['total_nodes']} nodes"])
            writer.writerow(['Cost', f"{p['machine_type']} cost/node", f"${p['cost_per_node']:.2f} / month"])
            writer.writerow(['Cost', f"Pool '{p['pool_name']}' total", f"${p['pool_total_cost']:.2f} / month"])
        
        writer.writerow(['Cost', 'GKE Management Fee', '$73.00 / month'])
        writer.writerow(['Cost', 'Total Monthly Infra Cost', f"${monthly_cost:.2f} / month"])
        
        # Benchmark & SLA
        writer.writerow(['Benchmark', 'Max Concurrent Users', max_users])
        if max_users > 0:
            writer.writerow(['Benchmark', 'Cost per Agent Month', f"${monthly_cost/max_users:.2f}"])
        writer.writerow(['Benchmark', 'SLA (TTFI 95%)', f"{ttfi_percentiles.get('95%', 0)} ms"])

    print(f"\nAnalysis successfully written to {args.output_csv}")
