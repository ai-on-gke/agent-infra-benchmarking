#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Deploys (or deletes) the sandbox backend's workloads. --backend picks
# which of two unrelated flows runs:
#
#   substrate (default, for backward compatibility): ActorTemplates and a
#   WorkerPool created through kubectl-ate/ko, waiting for golden snapshots.
#   Needs the dependency-repos/substrate submodule and BUCKET_NAME.
#
#   agent-sandbox: a Namespace, a ping SandboxTemplate, and a
#   SandboxWarmPool created through plain kubectl apply — no separate control
#   plane, no golden-snapshot wait. Needs PROJECT_ID (to derive the probe
#   image path) and the agent-sandbox controller already installed.

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

# Source the environment variables if configured
if [[ -f .env.sh ]]; then
  source .env.sh
fi

MANIFEST_DIR="benchmarking/workloads/manifests"
BACKEND="substrate"

usage() {
  echo "Usage: $0 [options]"
  echo ""
  echo "Options:"
  echo "  --deploy                    Deploy the selected --backend's workloads"
  echo "  --delete                    Delete them"
  echo "  --backend NAME              substrate (default) | agent-sandbox"
  echo "  --worker-count N            Pool size: WorkerPool replicas (substrate) or"
  echo "                              SandboxWarmPool replicas (agent-sandbox). Default: 1"
  echo "  --wait-timeout SECONDS      Timeout waiting for the pool to be ready (default: 300)"
  echo ""
  echo "substrate-only options:"
  echo "  --sandbox-class CLASS       Sandbox runtime for the WorkerPool: gvisor | microvm (default: gvisor)."
  echo "                              microvm requires dependency-repos/substrate/hack/install-microvm-deps.sh --install to have run."
  echo "  --actor-memory SIZE         Memory limit for the benchmark ActorTemplates (default: 256Mi,"
  echo "                              the smallest size microvm admits)"
  echo "  --otlp-endpoint URL         The address to which an instrumented actor container"
  echo "                              sends telemetry (default: the endpoint in the"
  echo "                              ate-otel-config ConfigMap)"
  echo ""
  echo "agent-sandbox-only options:"
  echo "  --runtime-class NAME        RuntimeClass for the probe SandboxTemplate (default: unset,"
  echo "                              i.e. runc). Must already be registered in the cluster."
  echo "  --probe-memory SIZE         Memory limit for the probe container (default: 64Mi)"
  echo ""
  echo "  -h, --help                  Show this help message"
}

action=""
WORKER_COUNT=1
WAIT_TIMEOUT_SECS=300

# substrate-only settings.
SANDBOX_CLASS="gvisor"
# Actor memory limit (ActorTemplate resources.limits.memory). The default
# is the smallest size microvm admits (128Mi VMM reserve + 128Mi guest floor),
# so benchmark actors do not inherit the 2 GiB kata default and drag its page
# cache into every memory snapshot. Raise it for RAM-consuming suites.
ACTOR_MEMORY="256Mi"
OTLP_ENDPOINT=""
# How often the golden-snapshot wait reports that it is still waiting. Only
# affects output, not the 5s poll interval.
PROGRESS_EVERY_SECS=30

# agent-sandbox-only settings.
SANDBOX_RUNTIME_CLASS=""
PING_MEMORY="64Mi"
BENCHMARK_NAMESPACE="benchmarking"

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --deploy) action="deploy" ;;
    --delete) action="delete" ;;
    --backend) shift; BACKEND="$1" ;;
    --backend=*) BACKEND="${1#*=}" ;;
    --worker-count) shift; WORKER_COUNT="$1" ;;
    --worker-count=*) WORKER_COUNT="${1#*=}" ;;
    --wait-timeout) shift; WAIT_TIMEOUT_SECS="$1" ;;
    --wait-timeout=*) WAIT_TIMEOUT_SECS="${1#*=}" ;;
    --sandbox-class) shift; SANDBOX_CLASS="$1" ;;
    --sandbox-class=*) SANDBOX_CLASS="${1#*=}" ;;
    --actor-memory) shift; ACTOR_MEMORY="$1" ;;
    --actor-memory=*) ACTOR_MEMORY="${1#*=}" ;;
    --otlp-endpoint) shift; OTLP_ENDPOINT="$1" ;;
    --otlp-endpoint=*) OTLP_ENDPOINT="${1#*=}" ;;
    --runtime-class) shift; SANDBOX_RUNTIME_CLASS="$1" ;;
    --runtime-class=*) SANDBOX_RUNTIME_CLASS="${1#*=}" ;;
    --probe-memory) shift; PING_MEMORY="$1" ;;
    --probe-memory=*) PING_MEMORY="${1#*=}" ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Error: Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

if [[ "$#" -eq 0 && -z "${action}" ]]; then
  usage
  exit 1
fi

if ! [[ "${WAIT_TIMEOUT_SECS}" =~ ^[0-9]+$ ]]; then
  echo "Error: --wait-timeout must be a whole number of seconds like 300, got '${WAIT_TIMEOUT_SECS}'" >&2
  exit 1
fi

case "${BACKEND}" in
  substrate|agent-sandbox) ;;
  *)
    echo "Error: --backend must be substrate or agent-sandbox, got '${BACKEND}'" >&2
    exit 1
    ;;
esac

###############################################################################
# substrate backend
###############################################################################

SUBSTRATE_DIR="${ROOT}/dependency-repos/substrate"
POOL_MANIFEST="${MANIFEST_DIR}/workloads.yaml.tmpl"
# The benchmark ActorTemplates: <name>-template.yaml.tmpl each, created
# through the ate API in the benchmark-workloads atespace. WORKLOAD_TEMPLATES
# overrides the default set — the usermem and kernelmem templates (for the
# matching locust tests) are not deployed by default.
read -r -a TEMPLATES <<<"${WORKLOAD_TEMPLATES:-sleep glutton glutton-durdir-data glutton-durdir-full openclaw}"

# Read the endpoint from the ate-otel-config ConfigMap, which every control
# plane component reads through envFrom. The value is correct for the cluster
# in use: the GKE collector, the kind collector in otel-system, or the
# telemetry meter while a measurement runs. Thus the actors follow the control
# plane, and this script needs no test for the type of cluster.
#
# ate-system must exist, because the workloads below need its CRDs. Thus an
# absent ConfigMap is an error and not a condition to work around: a default
# here would send the actor telemetry to the wrong collector with no message.
resolve_otlp_endpoint() {
  if [[ -n "${OTLP_ENDPOINT}" ]]; then
    return 0
  fi
  OTLP_ENDPOINT="$(kubectl get configmap ate-otel-config --namespace=ate-system \
    -o jsonpath='{.data.OTEL_EXPORTER_OTLP_ENDPOINT}' 2>/dev/null || true)"
  if [[ -z "${OTLP_ENDPOINT}" ]]; then
    echo "Error: cannot read OTEL_EXPORTER_OTLP_ENDPOINT from the ate-otel-config" >&2
    echo "ConfigMap in ate-system. Deploy ate-system first, or give --otlp-endpoint." >&2
    exit 1
  fi
}

# kubectl-ate runs once per poll while waiting for the golden snapshots;
# build_kubectl_ate builds it once up front instead of paying the `go run`
# toolchain startup on every call.
KUBECTL_ATE_BIN=""

build_kubectl_ate() {
  local dir
  dir="$(mktemp -d)"
  trap 'rm -rf '"${dir}" EXIT
  KUBECTL_ATE_BIN="${dir}/kubectl-ate"
  # kubectl-ate belongs to substrate, so it builds in the submodule against
  # that module's own go.mod and vendor tree. mktemp gives an absolute path,
  # so the output still lands outside the submodule.
  (cd "${SUBSTRATE_DIR}" && go build -o "${KUBECTL_ATE_BIN}" ./cmd/kubectl-ate)
}

run_kubectl_ate() {
  "${KUBECTL_ATE_BIN}" "$@"
}

# ko resolves the ko:// references in the manifests, which name substrate
# commands (cmd/ateom-*, cmd/benchmarking/glutton). Both the tool wrapper
# under hack/tools and those import paths resolve only inside the submodule,
# so ko runs with that as its working directory. Piped stdin flows through the
# subshell unchanged.
run_ko() {
  (cd "${SUBSTRATE_DIR}" && hack/run-tool.sh ko "$@")
}

# resolve_image_digest prints the digest reference for the image tag it is
# given. An ActorTemplate's image field takes a digest and rejects a tag, so
# the registry is asked what the tag points at right now and the template names
# whatever was last pushed. Called from the one loop iteration whose template
# needs it, so a run without that template never reaches the registry. Progress
# and errors go to stderr because the caller captures stdout.
resolve_image_digest() {
  local image="$1"
  if [[ -z "${image}" ]]; then
    echo "Error: no image reference to resolve; set the one this template needs" >&2
    echo "in .env.sh (see env.sh.example)." >&2
    return 1
  fi
  if ! command -v gcloud &>/dev/null; then
    echo "Error: gcloud is required to resolve ${image} to a digest." >&2
    return 1
  fi

  local digest
  digest="$(gcloud artifacts docker images describe "${image}" \
    --format="value(image_summary.digest)" 2>/dev/null || true)"
  # An unpushed tag and a registry that refused the request look the same from
  # here, and both leave this empty. Either way the template cannot be created,
  # so name the image that could not be resolved rather than substituting an
  # empty image field.
  if [[ "${digest}" != sha256:* ]]; then
    echo "Error: cannot resolve ${image} to a digest." >&2
    echo "Push it first with benchmarking/locust/build_and_push.sh, and check that" >&2
    echo "the active gcloud account can read that Artifact Registry repository." >&2
    return 1
  fi

  # The template takes the configured image path with the digest in place of
  # the tag, not the digest on its own. The path comes from the argument rather
  # than from the registry's own spelling of it, so the deployed reference is
  # the one .env.sh configured even where a repository answers to more than one
  # name. Only the last path segment can carry a tag, which leaves a registry
  # host with a port alone; ${image%@*} handles an argument that already names
  # a digest.
  local repo="${image%@*}"
  if [[ "${repo##*/}" == *:* ]]; then
    repo="${repo%:*}"
  fi
  echo "  image: ${repo}@${digest}" >&2
  printf '%s@%s\n' "${repo}" "${digest}"
}

substitute_substrate() {
  # SandboxConfig names are pinned per class in the ActorTemplates (rather
  # than defaulted) so a stale config from a dirty teardown fails loudly
  # instead of silently binding these workloads. gvisor-default is applied by
  # substrate's hack/install-ate.sh; microvm by its hack/install-microvm-deps.sh.
  # The protojson templates take the sandbox class as its proto enum spelling.
  local manifest="$1"
  # Only a template whose image is neither a ko:// reference nor a literal
  # carries a digest, and only its caller has one to give. Every other manifest
  # passes nothing and the expression below finds no placeholder to replace.
  local image_with_digest="${2:-}"
  local sandbox_config_name sandbox_class_enum
  case "${SANDBOX_CLASS}" in
    gvisor)  sandbox_config_name="gvisor-default" sandbox_class_enum="SANDBOX_CLASS_GVISOR" ;;
    microvm) sandbox_config_name="microvm"        sandbox_class_enum="SANDBOX_CLASS_MICROVM" ;;
  esac
  sed -e "s|\${BUCKET_NAME}|${BUCKET_NAME}|g" \
      -e "s|\${WORKER_COUNT}|${WORKER_COUNT}|g" \
      -e "s|\${SANDBOX_CLASS}|${SANDBOX_CLASS}|g" \
      -e "s|\${SANDBOX_CLASS_ENUM}|${sandbox_class_enum}|g" \
      -e "s|\${SANDBOX_CONFIG_NAME}|${sandbox_config_name}|g" \
      -e "s|\${OTLP_ENDPOINT}|${OTLP_ENDPOINT}|g" \
      -e "s|\${ACTOR_MEMORY}|${ACTOR_MEMORY}|g" \
      -e "s|\${PROJECT_ID}|${PROJECT_ID}|g" \
      -e "s|\${IMAGE_WITH_DIGEST}|${image_with_digest}|g" \
      "${manifest}"
}

# wait_actortemplate_ready polls a substrate ActorTemplate resource until its
# golden snapshot exists (the substrate counterpart of `kubectl wait
# --for=condition=Ready actortemplate/...`). Fails fast when the template
# reconciler reports an error.
wait_actortemplate_ready() {
  local atespace="$1"
  local template="$2"
  local timeout_secs="${3:-300}"
  local started=${SECONDS}
  local deadline=$((SECONDS + timeout_secs))
  local json snapshot error_message elapsed
  local last_json="" last_get_err="" next_report=$((SECONDS + PROGRESS_EVERY_SECS))
  # The poll's stderr is kept rather than discarded: a get that fails on every
  # attempt otherwise looks exactly like a snapshot that is merely slow, and
  # both end in the same bare timeout.
  local err_file
  err_file="$(mktemp)"

  while ((SECONDS < deadline)); do
    if json=$(run_kubectl_ate get actor-template "${template}" -a "${atespace}" -o json 2>"${err_file}"); then
      last_get_err=""
      last_json="${json}"
      snapshot=$(jq -r '.status.goldenSnapshotStatus.goldenSnapshot.snapshotUri // empty' <<<"${json}")
      if [[ -n "${snapshot}" ]]; then
        rm -f "${err_file}"
        echo "  ${template}: golden snapshot ready after $((SECONDS - started))s"
        echo "  ${template}: ${snapshot}"
        return 0
      fi
      error_message=$(jq -r '.status.goldenSnapshotStatus.errorMessage // empty' <<<"${json}")
      if [[ -n "${error_message}" ]]; then
        rm -f "${err_file}"
        echo "actor template ${atespace}/${template} failed: ${error_message}" >&2
        return 1
      fi
    else
      last_get_err="$(<"${err_file}")"
    fi

    # Without this the script is silent for the whole timeout, so a stuck
    # reconciler and a slow one look the same while it is running.
    if ((SECONDS >= next_report)); then
      elapsed=$((SECONDS - started))
      if [[ -n "${last_get_err}" ]]; then
        echo "  ${template}: ${elapsed}s/${timeout_secs}s — get failing: ${last_get_err//$'\n'/ }"
      else
        echo "  ${template}: ${elapsed}s/${timeout_secs}s — no golden snapshot yet$(describe_template_state "${last_json}")"
      fi
      next_report=$((SECONDS + PROGRESS_EVERY_SECS))
    fi
    sleep 5
  done

  rm -f "${err_file}"
  echo "timed out after ${timeout_secs}s waiting for actor template ${atespace}/${template} golden snapshot" >&2
  if [[ -n "${last_get_err}" ]]; then
    echo "  every poll failed; last error from kubectl-ate get:" >&2
    echo "${last_get_err}" | sed 's/^/    /' >&2
  elif [[ -n "${last_json}" ]]; then
    echo "  the template exists but reported neither a snapshot nor an error." >&2
    echo "  last observed .status:" >&2
    jq '.status // {}' <<<"${last_json}" 2>/dev/null | sed 's/^/    /' >&2
  fi
  echo "  snapshots are written to gs://${BUCKET_NAME}/${atespace}/${template}/ — check that the" >&2
  echo "  bucket exists and the ateom workers can write to it, then check the workers:" >&2
  echo "    kubectl get pods -n ${atespace}" >&2
  echo "    kubectl logs -n ${atespace} deployment/benchmark-ateom --tail=50" >&2
  return 1
}

# describe_template_state turns whatever the template reports into a short
# suffix for the progress line, so a pending snapshot still says something
# about why. Prints nothing when the status carries no recognizable field.
describe_template_state() {
  local json="$1"
  [[ -n "${json}" ]] || return 0
  local phase
  phase=$(jq -r '
    [ .status.goldenSnapshotStatus.phase?,
      .status.goldenSnapshotStatus.state?,
      .status.phase?,
      .status.state? ] | map(select(. != null and . != "")) | first // empty
  ' <<<"${json}" 2>/dev/null) || return 0
  [[ -n "${phase}" ]] && printf ' (%s)' "${phase}"
}

# wait_templates_ready blocks until every benchmark template's golden
# snapshot exists (there is no kubectl wait for substrate resources), failing
# fast when the template reconciler reports an error.
wait_templates_ready() {
  if ! command -v jq &>/dev/null; then
    echo "jq is required to wait for the benchmark actor templates" >&2
    return 1
  fi
  local template i=0
  local total=${#TEMPLATES[@]}
  for template in "${TEMPLATES[@]}"; do
    i=$((i + 1))
    echo "[${i}/${total}] Waiting for the benchmark-workloads/${template} golden snapshot (timeout ${WAIT_TIMEOUT_SECS}s)..."
    wait_actortemplate_ready benchmark-workloads "${template}" "${WAIT_TIMEOUT_SECS}"
  done
  echo "All ${total} golden snapshots ready."
}

deploy_substrate() {
  if [[ ! -f "${SUBSTRATE_DIR}/go.mod" ]]; then
    echo "Error: substrate submodule not initialized at ${SUBSTRATE_DIR}." >&2
    echo "Run: git submodule update --init dependency-repos/substrate" >&2
    exit 1
  fi
  if [[ -z "${BUCKET_NAME:-}" ]]; then
    echo "Error: BUCKET_NAME environment variable is not set." >&2
    exit 1
  fi
  if [[ ! -f "${POOL_MANIFEST}" ]]; then
    echo "Error: ${POOL_MANIFEST} not found in $(pwd)" >&2
    exit 1
  fi
  case "${SANDBOX_CLASS}" in
    gvisor|microvm) ;;
    *)
      echo "Error: --sandbox-class must be gvisor or microvm, got '${SANDBOX_CLASS}'" >&2
      exit 1
      ;;
  esac

  build_kubectl_ate
  resolve_otlp_endpoint
  # Echoed up front because every later failure is explained by one of these:
  # the bucket receives the golden snapshots, the templates are what the wait
  # below blocks on, and the sandbox class has to match the WorkerPool's.
  echo "Deploying workloads (backend=substrate):"
  echo "  templates:      ${TEMPLATES[*]}"
  echo "  worker_count:   ${WORKER_COUNT}"
  echo "  sandbox_class:  ${SANDBOX_CLASS}"
  echo "  actor_memory:   ${ACTOR_MEMORY}"
  echo "  otlp_endpoint:  ${OTLP_ENDPOINT}"
  echo "  bucket:         gs://${BUCKET_NAME}/benchmark-workloads/"
  echo "  substrate:      ${SUBSTRATE_DIR} ($(cd "${SUBSTRATE_DIR}" && git rev-parse --short HEAD 2>/dev/null || echo 'unknown rev'))"
  substitute_substrate "${POOL_MANIFEST}" | run_ko apply -f -
  echo "Waiting for worker pool to be ready (timeout: ${WAIT_TIMEOUT_SECS}s)..."
  kubectl wait --for=create deployment/benchmark-ateom \
    --namespace=benchmark-workloads --timeout="${WAIT_TIMEOUT_SECS}s"
  kubectl rollout status deployment/benchmark-ateom \
    --namespace=benchmark-workloads --timeout="${WAIT_TIMEOUT_SECS}s"

  # The store enforces that a template's atespace exists at create time.
  run_kubectl_ate create atespace benchmark-workloads >/dev/null 2>&1 \
    || run_kubectl_ate get atespace benchmark-workloads >/dev/null

  # Actor templates are immutable (no update RPC). A value that changes for
  # each run — the OTLP endpoint or the sandbox class — needs the removal of
  # the old template first. This removal is safe, because the benchmark
  # automation deletes the actors between the tests; it also removes the old
  # golden actor and snapshot server-side.
  local template image_with_digest
  for template in "${TEMPLATES[@]}"; do
    run_kubectl_ate delete actor-template "${template}" -a benchmark-workloads \
      >/dev/null 2>&1 || true
    # This one template's image is neither a ko:// reference nor a literal, so
    # its digest is looked up on its own iteration. Assigned on a line of its
    # own rather than on the declaration above: `local x="$(...)"` would return
    # local's status and hide a failed lookup from errexit.
    image_with_digest=""
    if [[ "${template}" == "openclaw" ]]; then
      image_with_digest="$(resolve_image_digest "${OPENCLAW_IMAGE:-}")"
    fi
    # ko resolve builds the ko:// image references and replaces them with
    # pushed digests before the manifest reaches kubectl-ate.
    substitute_substrate "${MANIFEST_DIR}/${template}-template.yaml.tmpl" "${image_with_digest}" \
      | run_ko resolve -f - \
      | run_kubectl_ate create actor-template -f -
  done

  wait_templates_ready
}

delete_substrate() {
  if [[ ! -f "${SUBSTRATE_DIR}/go.mod" ]]; then
    echo "Error: substrate submodule not initialized at ${SUBSTRATE_DIR}." >&2
    echo "Run: git submodule update --init dependency-repos/substrate" >&2
    exit 1
  fi
  build_kubectl_ate
  echo "Deleting workloads (backend=substrate)..."
  local template
  for template in "${TEMPLATES[@]}"; do
    run_kubectl_ate delete actor-template "${template}" -a benchmark-workloads \
      >/dev/null 2>&1 || true
  done
  run_kubectl_ate delete atespace benchmark-workloads >/dev/null 2>&1 \
    || echo "atespace benchmark-workloads not deleted (may not exist or is not empty)"
  # The pool manifest contains ko:// image references; route through
  # `ko delete` so they get resolved before kubectl sees them.
  substitute_substrate "${POOL_MANIFEST}" | run_ko delete --ignore-not-found -f -
}

###############################################################################
# agent-sandbox backend
###############################################################################
# Plain namespaced CRDs the agent-sandbox controller already watches once
# it's installed — no separate control plane, no kubectl-ate/ko, no
# golden-snapshot wait like substrate's flow above.

substitute_agent_sandbox() {
  local manifest="$1"
  local runtime_class_line=""
  if [[ -n "${SANDBOX_RUNTIME_CLASS}" ]]; then
    runtime_class_line="runtimeClassName: ${SANDBOX_RUNTIME_CLASS}"
  fi
  sed -e "s|\${BENCHMARK_NAMESPACE}|${BENCHMARK_NAMESPACE}|g" \
      -e "s|\${WORKER_COUNT}|${WORKER_COUNT}|g" \
      -e "s|\${RUNTIME_CLASS_LINE}|${runtime_class_line}|g" \
      -e "s|\${PING_MEMORY}|${PING_MEMORY}|g" \
      -e "s|\${PROBE_IMAGE}|${PROBE_IMAGE:-}|g" \
      "${manifest}"
}

deploy_agent_sandbox() {
  if [ -z "${PROJECT_ID:-}" ]; then
    echo "Error: PROJECT_ID environment variable must be set." >&2
    exit 1
  fi
  # Derived the same way locust/build_and_push.sh derives it, so the two
  # never need to agree on a value passed between them: given the same
  # PROJECT_ID, both compute the same string. Override by exporting
  # PROBE_IMAGE (e.g. in .env.sh) for a custom registry path.
  PROBE_IMAGE="${PROBE_IMAGE:-us-docker.pkg.dev/${PROJECT_ID}/agent-infra-benchmarking/ping-probe:latest}"

  echo "Deploying (backend=agent-sandbox):"
  echo "  namespace:      ${BENCHMARK_NAMESPACE}"
  echo "  worker_count:   ${WORKER_COUNT}"
  echo "  runtime_class:  ${SANDBOX_RUNTIME_CLASS:-<default>}"
  echo "  probe_memory:   ${PING_MEMORY}"
  echo "  probe_image:    ${PROBE_IMAGE}"

  substitute_agent_sandbox "${MANIFEST_DIR}/namespace.yaml.tmpl" | kubectl apply -f -
  substitute_agent_sandbox "${MANIFEST_DIR}/ping-template.yaml.tmpl" | kubectl apply -f -
  substitute_agent_sandbox "${MANIFEST_DIR}/ping-warmpool.yaml.tmpl" | kubectl apply -f -

  echo "Waiting for the warm pool to fill (timeout: ${WAIT_TIMEOUT_SECS}s)..."
  kubectl wait sandboxwarmpool/ping-warmpool --namespace "${BENCHMARK_NAMESPACE}" \
    --for=jsonpath='{.status.readyReplicas}'="${WORKER_COUNT}" --timeout="${WAIT_TIMEOUT_SECS}s"
}

delete_agent_sandbox() {
  echo "Deleting (backend=agent-sandbox)..."
  substitute_agent_sandbox "${MANIFEST_DIR}/ping-warmpool.yaml.tmpl" | kubectl delete --ignore-not-found -f -
  substitute_agent_sandbox "${MANIFEST_DIR}/ping-template.yaml.tmpl" | kubectl delete --ignore-not-found -f -
  # The namespace is left in place deliberately: deleting it would also
  # cascade-delete the locust Deployment/RBAC that
  # benchmarking/locust/deploy.sh puts in the same namespace, coupling two
  # otherwise-independent teardown steps. Delete it by hand if it should go
  # too: kubectl delete namespace "${BENCHMARK_NAMESPACE}"
}

###############################################################################

if [[ "${action}" == "deploy" ]]; then
  if [[ "${BACKEND}" == "agent-sandbox" ]]; then
    deploy_agent_sandbox
  else
    deploy_substrate
  fi
elif [[ "${action}" == "delete" ]]; then
  if [[ "${BACKEND}" == "agent-sandbox" ]]; then
    delete_agent_sandbox
  else
    delete_substrate
  fi
fi
