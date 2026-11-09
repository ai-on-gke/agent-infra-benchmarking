# Workloads

A workload is the application inside each actor that the boomer worker sends
instructions to. Today there are two: `glutton` and `openclaw`. This
directory holds their images (`images/`, `images.json`) and their
ActorTemplates (`manifests/`).

## Adding a workload

Below, `myapp` is the new workload's name. It has to be the same in the Go
workload's `Name()`, the template's file name (`myapp-template.yaml.tmpl`),
the template's `metadata.name`, the key in `TEMPLATE_IMAGES` (step 4) and the
case's `workload-name` (step 7). The image's key in `images.json` can be
different. Paths are relative to this directory; commands run from the repo
root.

Steps 1–7 add the workload on substrate. Step 8 checks it works on a small
cluster. Agent-sandbox needs one more step, after that.

### 1. Write the actor image

Create `images/myapp/` with a `Dockerfile` and anything it copies. Pin its
base image by digest, such as `python:3.11.16-slim@sha256:<digest>`, so every
build runs the same. The application in it has to:

- serve HTTP on port 80, the only port forwarded into the sandbox;
- answer a readiness route, such as `GET /readyz`;
- send back something that proves it ran the instruction, best an echo of a
  nonce from the request.

A cluster takes long to find a broken image, so check it locally first:

```bash
docker build -t myapp-local benchmarking/workloads/images/myapp
docker run --rm -d -p 18080:80 --name myapp-local myapp-local
curl localhost:18080/readyz
curl -X POST --data hello localhost:18080/run
docker rm -f myapp-local
```

### 2. Register the image

Add an entry to [`images.json`](images.json):

```json
"myapp-workload": {
  "description": "The actor the myapp workload drives.",
  "dockerfile": "benchmarking/workloads/images/myapp/Dockerfile",
  "context": "benchmarking/workloads/images/myapp"
}
```

The key, here `myapp-workload`, is what steps 4 and 9 refer to.

### 3. Write the ActorTemplate

Create `manifests/myapp-template.yaml.tmpl`, with your container's `name` and
`command` and your readiness route in `wakeupProbe`:

```yaml
metadata:
  atespace: benchmark-workloads
  name: myapp
workerSelector:
  matchLabels:
    workload: benchmark-ateom
containers:
- name: myapp
  image: ${IMAGE_WITH_DIGEST}
  command: ["python3", "-u", "/app/myapp.py"]
  wakeupProbe:
    httpGet:
      path: /readyz
      port: 80
resources:
  limits:
    cpuMillicores: "${ACTOR_CPU_MILLICORES}"
    memoryBytes: "${ACTOR_MEMORY_BYTES}"
snapshotConfig:
  preferredFidelity: SNAPSHOT_FIDELITY_MEMORY
```

The container's `env` is optional. The worker lets the actors reach the hosts
of the URLs in it; any other host needs `actor-egress-allow` in step 7. Use
only these placeholders; any other `${...}` fails the deploy:

| Placeholder | Filled with |
|---|---|
| `${IMAGE_WITH_DIGEST}` | the image from step 2, pinned |
| `${ACTOR_MEMORY_BYTES}`, `${ACTOR_CPU_MILLICORES}` | the case's `actor-memory`, and 1 CPU |
| `${OTLP_ENDPOINT}` | the control plane's OTLP collector |
| `${PROJECT_ID}` | the cluster's project |

An actor that sends OpenTelemetry has to be told the collector, as substrate
gives it none. Add to the container:

```yaml
  env:
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: "${OTLP_ENDPOINT}"
  - name: OTEL_RESOURCE_ATTRIBUTES
    value: "service.name=myapp-actor"
```

### 4. Register the template with the scenario runner

In [`scenario_runner/src/scenario_runner/workloads/__init__.py`](../scenario_runner/src/scenario_runner/workloads/__init__.py),
add the template and its image key to `TEMPLATE_IMAGES`:

```python
TEMPLATE_IMAGES = {"openclaw": "openclaw-workload", "myapp": "myapp-workload"}
```

If the actors need more than 256Mi, also add `myapp` to
`DEFAULT_ACTOR_MEMORY_BY_WORKLOAD` and `MIN_ACTOR_MEMORY_MIB` in the same file.

### 5. Add the Go workload

Create `myapp.go` in [`locust/go/internal/workload/`](../locust/go/internal/workload/)
with the code below, then change two things in it: `Path`, to your
application's route, and `Verify`, to check your application's reply:

```go
package workload

import (
	"context"
	"fmt"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
)

const (
	myappWorkloadName = "myapp"
	myappTemplate     = "myapp"
)

func init() {
	register(myappWorkload{})
}

type myappWorkload struct{}

func (myappWorkload) Name() string            { return myappWorkloadName }
func (myappWorkload) DefaultTemplate() string { return myappTemplate }

func (myappWorkload) SendInstruction(ctx context.Context, actor sandbox.Actor) (bool, error) {
	nonce := uuid.NewString()
	return sendHTTPInstruction(ctx, actor, httpInstruction{
		Method:      http.MethodPost,
		Path:        "/run",
		ContentType: textContentType,
		Body:        []byte(nonce),
		Verify: func(body []byte) error {
			if !strings.Contains(string(body), nonce) {
				return fmt.Errorf("reply %q does not carry nonce %q", body, nonce)
			}
			return nil
		},
	})
}
```

`SendInstruction` must not return a nil error before the actor has run the
instruction: a nil error ends the measurement. `sendHTTPInstruction`
already retries a 5xx, 429 or 421. To support `glutton-write-ram` and
`glutton-write-disk`, also implement `Loader` from `workload.go`.

### 6. Add a Go test

Create `locust/go/internal/workload/myapp_test.go`. A test server standing in
for the actor is enough:

```go
func newMyappActor(t *testing.T, handler http.HandlerFunc) sandbox.Actor {
	t.Helper()
	ts := httptest.NewServer(handler)
	t.Cleanup(ts.Close)
	return sandbox.Actor{HTTPClient: ts.Client(), RouterURL: ts.URL, Name: "myappactor"}
}
```

With it, test that:

- `SendInstruction` sends the right request, and succeeds with `retry`
  false on the right reply;
- a wrong reply fails it, with `retry` false;
- a 503 fails it with `retry` true;
- `Resolve("myapp")` returns your workload, with `DefaultTemplate()`
  `myapp`.

### 7. Add a case

First add a small case to check the workload on, in
[`scenarios/scenarios.yaml`](../scenarios/scenarios.yaml) under `cases:`: one
sandboxed node and 10 VUs (virtual users) for 5 minutes. `workload-name` also names the template to deploy:

```yaml
  myapp-single:
    repeats:
      warmups: 0
    locust:
      install:
        substrate:
          sandbox-class: gvisor
      infra:
        cluster:
          sandboxed-nodes: 1
      workers:
        count: 10
      master:
        run-time: 5m
        users: 10
        spawn-rate: 10
        warm-up: 10s
      worker:
        resume-mode: explicit
        workload-name: myapp
        # Only if the actors call a host that no URL in the template's env
        # names; without it they can't reach it.
        # actor-egress-allow: myhost.example.svc.cluster.local
```

To check how the case resolves, before anything is applied:

```bash
./benchmarking/tools/python -m scenario_runner.cli.cases myapp-single
```

Once step 8 passes, add the workload to the full-size cases too: an entry in
the `workload` parameters of the `"{workload}-{control-plane}-{sandbox-class}"`
case, which makes `myapp-substrate-gvisor` and `myapp-substrate-microvm`, each
300 VUs on 30 nodes:

```yaml
      - workload: myapp
        # As in myapp-single, only if needed.
        actor-egress-allow: myhost.example.svc.cluster.local
```

### 8. Check it

Run the Go tests, from `benchmarking/locust/go`:

```bash
go test ./...
```

Then the scenario-runner's, from `benchmarking/scenario_runner`, for the
change to `workloads/__init__.py`:

```bash
../tools/python -m unittest discover -s tests
```

Then run the small case on a dev infra. The run deploys the template and fails
if the actors don't answer:

```bash
./benchmarking/tools/run --dev-infra myapp-dev --scenario myapp-single
```

The reports land in `reports/<run-id>/myapp-single/`, and the run's
`README.md` there says whether it passed.

### 9. Optional: agent-sandbox

Create `../agent-sandbox/manifests/myapp-template.yaml.tmpl`, a
SandboxTemplate, with your container's `name`, `command` and readiness route.
It takes `${NAME}`, `${NAMESPACE}`, `${IMAGE}` and `${RUNTIME_CLASS}`, and
sets the memory limit itself:

```yaml
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxTemplate
metadata:
  name: ${NAME}
  namespace: ${NAMESPACE}
spec:
  networkPolicyManagement: Unmanaged
  podTemplate:
    spec:
      runtimeClassName: ${RUNTIME_CLASS}
      dnsPolicy: ClusterFirst
      nodeSelector:
        benchmarking-worker: "true"
      tolerations:
      - key: ate.dev/sandboxClass
        operator: Exists
        effect: NoSchedule
      containers:
      - name: myapp
        image: ${IMAGE}
        command: ["python3", "-u", "/app/myapp.py"]
        ports:
        - containerPort: 80
          name: http
        readinessProbe:
          httpGet:
            path: /readyz
            port: 80
          initialDelaySeconds: 1
          periodSeconds: 2
        resources:
          limits:
            memory: 256Mi
```

Add the image key to `WORKLOAD_IMAGES` in
[`agent_sandbox.py`](../scenario_runner/src/scenario_runner/control_planes/agent_sandbox.py):

```python
WORKLOAD_IMAGES = {"glutton": "glutton", "openclaw": "openclaw-workload", "myapp": "myapp-workload"}
```

Then add an entry to the parameters of the `"{workload}-agent-sandbox-gvisor"`
case, with `actor-egress-allow` as in step 7 if needed, and run
`myapp-agent-sandbox-gvisor` as in step 8, on a dev infra of its own: it needs
`gke-sandbox`, which `myapp-dev`'s cluster lacks.

```yaml
      - workload: myapp
        vus: 30
```
