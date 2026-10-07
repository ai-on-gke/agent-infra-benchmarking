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

set -o errexit -o nounset -o pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

# The manifests below are ours, but the tooling they need is not: kubectl-ate
# and the ko wrapper are substrate commands, and the ko:// references name
# packages in substrate's module. Those steps therefore run inside the
# submodule rather than against this repo.
SUBSTRATE_DIR="${ROOT}/dependency-repos/substrate"
if [[ ! -f "${SUBSTRATE_DIR}/go.mod" ]]; then
  echo "Error: substrate submodule not initialized at ${SUBSTRATE_DIR}." >&2
  echo "Run: git submodule update --init dependency-repos/substrate" >&2
  exit 1
fi

# Source the environment variables if configured
if [[ -f .env.sh ]]; then
  source .env.sh
fi
source benchmarking/images/lib.sh

# Ensure BUCKET_NAME is set
if [[ -z "${BUCKET_NAME:-}" ]]; then
  echo "Error: BUCKET_NAME environment variable is not set." >&2
  exit 1
fi

MANIFEST_DIR="benchmarking/workloads/manifests"
# The benchmark ActorTemplates named by --workload-template: a
# <name>-template.yaml.tmpl each, created through the ate API in the
# benchmark-workloads atespace.
TEMPLATES=()

# Created by deploy_worker_pool.sh; it takes the golden snapshots.
POOL_NAME=benchmark-ateom

SANDBOX_CLASS="gvisor"
# Actor memory limit (ActorTemplate resources.limits.memory). The default
# is the smallest size microvm admits (128Mi VMM reserve + 128Mi guest floor),
# so benchmark actors do not inherit the 2 GiB kata default and drag its page
# cache into every memory snapshot. Raise it for RAM-consuming suites.
ACTOR_MEMORY="256Mi"
# The address to which an instrumented actor container sends its telemetry.
# --otlp-endpoint sets it. Without the flag, resolve_otlp_endpoint reads the
# address that the control plane uses.
OTLP_ENDPOINT=""
# The timeout, in whole seconds, for waiting for mock-llm and each golden
# snapshot.
WAIT_TIMEOUT_SECS=300
# How often the golden-snapshot wait reports that it is still waiting. Only
# affects output, not the 5s poll interval.
PROGRESS_EVERY_SECS=30

usage() {
  echo "Usage: $0 [options]"
  echo ""
  echo "Deploys mock-llm and the benchmark ActorTemplates onto the WorkerPool that"
  echo "deploy_worker_pool.sh created, and waits for their golden snapshots."
  echo ""
  echo "Options:"
  echo "  --deploy                    Substitute env vars and deploy workloads to the cluster"
  echo "  --delete                    Delete workloads from the cluster"
  echo "  --workload-template NAME    ActorTemplate to deploy, from manifests/NAME-template.yaml.tmpl."
  echo "                              Required with --deploy, and may be given more than once. With"
  echo "                              --delete it narrows the delete; without it every template goes."
  echo "  --sandbox-class CLASS       Sandbox runtime for the ActorTemplates: gvisor | microvm (default: gvisor)."
  echo "                              Has to match the WorkerPool's."
  echo "  --actor-memory SIZE        Memory limit for the benchmark ActorTemplates (default: 256Mi,"
  echo "                              the smallest size microvm admits)"
  echo "  --otlp-endpoint URL         The address to which an instrumented actor container"
  echo "                              sends telemetry (default: the endpoint in the"
  echo "                              ate-otel-config ConfigMap)"
  echo "  --wait-timeout SECONDS      The timeout in seconds for waiting for mock-llm and each golden snapshot (default: 300)"
  echo "  -h, --help                  Show this help message"
}

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

substitute() {
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
#
# Readiness is the published golden tag, which is what upstream's own wait
# tests (internal/e2e/template.go). The controller warms a temporary golden
# actor, copies its snapshot into a tag owned by the template, and only then
# records the reference here — so a tag is the point at which an actor of this
# template can be restored from the golden, while the snapshot existing is not.
wait_actortemplate_ready() {
  local atespace="$1"
  local template="$2"
  local timeout_secs="${3:-300}"
  local started=${SECONDS}
  local deadline=$((SECONDS + timeout_secs))
  local json golden_tag error_message elapsed
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
      # The tag's name is its required field, so its presence is what marks
      # the status populated; the atespace is reserved (ate-golden) and is
      # printed only to locate the tag.
      golden_tag=$(jq -r '
        .status.goldenSnapshotStatus.goldenTag
        | select(.name != null and .name != "")
        | ((.atespace // "") + "/" + .name) // empty
      ' <<<"${json}")
      if [[ -n "${golden_tag}" ]]; then
        rm -f "${err_file}"
        echo "  ${template}: golden snapshot ready after $((SECONDS - started))s"
        echo "  ${template}: tag ${golden_tag}"
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
  echo "  the golden snapshot is warmed on a temporary actor and published as a tag in the" >&2
  echo "  reserved ate-golden atespace, under gs://${BUCKET_NAME}/ — check that the bucket" >&2
  echo "  exists and the ateom workers can write to it, then check the workers:" >&2
  echo "    kubectl get pods -n ${atespace}" >&2
  echo "    kubectl logs -n ${atespace} deployment/benchmark-ateom --tail=50" >&2
  return 1
}

# describe_template_state turns whatever the template reports into a short
# suffix for the progress line, so a pending snapshot still says something
# about why. Prints nothing when the status carries no recognizable field.
#
# takeGoldenSnapshotAt is the only progress the status carries before the tag
# appears: it is when the golden actor's warmup ends and the snapshot may be
# taken, so a time already past means the wait is on the snapshot itself
# rather than on the warmup, and an absent one means the golden actor has not
# started warming.
describe_template_state() {
  local json="$1"
  [[ -n "${json}" ]] || return 0
  local take_at
  take_at=$(jq -r '.status.goldenSnapshotStatus.takeGoldenSnapshotAt // empty' \
    <<<"${json}" 2>/dev/null) || return 0
  [[ -n "${take_at}" ]] && printf ' (snapshot due at %s)' "${take_at}"
  return 0
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

# The golden snapshots are taken on the pool's workers, so it has to be up and
# of the templates' sandbox class.
check_worker_pool() {
  local pool_class
  if ! pool_class="$(kubectl get workerpool "${POOL_NAME}" --namespace=benchmark-workloads \
      -o jsonpath='{.spec.sandboxClass}' 2>/dev/null)"; then
    echo "Error: no ${POOL_NAME} WorkerPool in benchmark-workloads." >&2
    echo "Run: benchmarking/workloads/deploy_worker_pool.sh --deploy --sandbox-class ${SANDBOX_CLASS}" >&2
    exit 1
  fi
  if [[ "${pool_class}" != "${SANDBOX_CLASS}" ]]; then
    echo "Error: the ${POOL_NAME} WorkerPool is ${pool_class}, but --sandbox-class is ${SANDBOX_CLASS}." >&2
    exit 1
  fi
  kubectl rollout status "deployment/${POOL_NAME}" \
    --namespace=benchmark-workloads --timeout="${WAIT_TIMEOUT_SECS}s"
}

deploy() {
  check_worker_pool
  resolve_otlp_endpoint
  # Echoed up front because every later failure is explained by one of these:
  # the bucket receives the golden snapshots and the templates are what the
  # wait below blocks on.
  echo "Deploying workloads:"
  echo "  templates:      ${TEMPLATES[*]}"
  echo "  sandbox_class:  ${SANDBOX_CLASS}"
  echo "  actor_memory:   ${ACTOR_MEMORY}"
  echo "  otlp_endpoint:  ${OTLP_ENDPOINT}"
  echo "  bucket:         gs://${BUCKET_NAME}/benchmark-workloads/"
  echo "  substrate:      ${SUBSTRATE_DIR} ($(cd "${SUBSTRATE_DIR}" && git rev-parse --short HEAD 2>/dev/null || echo 'unknown rev'))"
  echo "Deploying mock-llm service..."
  local mock_llm_image
  mock_llm_image="$(pin_image "${MOCK_LLM_IMAGE:-}")"
  sed "s|\${IMAGE_WITH_DIGEST}|${mock_llm_image}|g" "${MANIFEST_DIR}/mock-llm.yaml.tmpl" | kubectl apply -f -
  echo "Waiting for mock-llm to be ready (timeout: ${WAIT_TIMEOUT_SECS}s)..."
  kubectl rollout status deployment/mock-llm --namespace=benchmark-workloads --timeout="${WAIT_TIMEOUT_SECS}s"

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
      image_with_digest="$(pin_image "${OPENCLAW_IMAGE:-}")"
    fi
    # ko resolve builds the ko:// image references and replaces them with
    # pushed digests before the manifest reaches kubectl-ate.
    substitute "${MANIFEST_DIR}/${template}-template.yaml.tmpl" "${image_with_digest}" \
      | run_ko resolve -f - \
      | run_kubectl_ate create actor-template -f -
  done

  wait_templates_ready
}

delete() {
  echo "Deleting workloads..."
  local template
  for template in "${TEMPLATES[@]}"; do
    run_kubectl_ate delete actor-template "${template}" -a benchmark-workloads \
      >/dev/null 2>&1 || true
  done
  run_kubectl_ate delete atespace benchmark-workloads >/dev/null 2>&1 \
    || echo "atespace benchmark-workloads not deleted (may not exist or is not empty)"
  kubectl delete -f "${MANIFEST_DIR}/mock-llm.yaml.tmpl" --ignore-not-found 2>/dev/null || true
}

if [[ "$#" -eq 0 ]]; then
  usage
  exit 1
fi

action=""
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --deploy)
      action="deploy"
      ;;
    --delete)
      action="delete"
      ;;
    --workload-template)
      shift
      TEMPLATES+=("$1")
      ;;
    --workload-template=*)
      TEMPLATES+=("${1#*=}")
      ;;
    --sandbox-class)
      shift
      SANDBOX_CLASS="$1"
      ;;
    --sandbox-class=*)
      SANDBOX_CLASS="${1#*=}"
      ;;
    --otlp-endpoint)
      shift
      OTLP_ENDPOINT="$1"
      ;;
    --otlp-endpoint=*)
      OTLP_ENDPOINT="${1#*=}"
      ;;
    --actor-memory)
      shift
      ACTOR_MEMORY="$1"
      ;;
    --actor-memory=*)
      ACTOR_MEMORY="${1#*=}"
      ;;
    --wait-timeout)
      shift
      WAIT_TIMEOUT_SECS="$1"
      ;;
    --wait-timeout=*)
      WAIT_TIMEOUT_SECS="${1#*=}"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Error: Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

case "${SANDBOX_CLASS}" in
  gvisor|microvm) ;;
  *)
    echo "Error: --sandbox-class must be gvisor or microvm, got '${SANDBOX_CLASS}'" >&2
    exit 1
    ;;
esac

if ! [[ "${WAIT_TIMEOUT_SECS}" =~ ^[0-9]+$ ]]; then
  echo "Error: --wait-timeout must be a whole number of seconds like 300, got '${WAIT_TIMEOUT_SECS}'" >&2
  exit 1
fi

if [[ "${#TEMPLATES[@]}" -eq 0 ]]; then
  if [[ "${action}" == "deploy" ]]; then
    echo "Error: --deploy needs at least one --workload-template." >&2
    exit 1
  fi
  for manifest in "${MANIFEST_DIR}"/*-template.yaml.tmpl; do
    manifest="${manifest##*/}"
    TEMPLATES+=("${manifest%-template.yaml.tmpl}")
  done
fi
for template in "${TEMPLATES[@]}"; do
  if [[ ! -f "${MANIFEST_DIR}/${template}-template.yaml.tmpl" ]]; then
    echo "Error: no ${MANIFEST_DIR}/${template}-template.yaml.tmpl for --workload-template ${template}." >&2
    exit 1
  fi
done

if [[ "${action}" == "deploy" ]]; then
  build_kubectl_ate
  deploy
elif [[ "${action}" == "delete" ]]; then
  build_kubectl_ate
  delete
fi
