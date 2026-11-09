# Workloads

A workload is the application inside each actor that the boomer worker sends
instructions to. Today there are two: `glutton` and `openclaw`. This
directory holds their images (`images/`, `images.json`) and their
ActorTemplates (`manifests/`).

## Adding a workload

Below, `myapp` is the new workload's name. It has to be the same in three
places: the Go workload's `Name()`, the template's file name
(`myapp-template.yaml.tmpl`) and the template's `metadata.name`. The image's
key in `images.json` can be different. Paths are relative to this directory.

Steps 1–7 add the workload on substrate. Step 8 checks it works. Agent-sandbox
needs one more step, after that.

### 1. Write the actor image

Create `images/myapp/` with a `Dockerfile` and anything it copies. The
application in it has to:

- serve HTTP on port 80, the only port forwarded into the sandbox;
- answer a readiness route, such as `GET /readyz`;
- send back something that proves it ran the instruction, best an echo of a
  nonce from the request.

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

Copy [`manifests/openclaw-template.yaml.tmpl`](manifests/openclaw-template.yaml.tmpl)
to `manifests/myapp-template.yaml.tmpl`, then change:

- `metadata.name` to `myapp`;
- the container's `name`, `command` and `env` to your application's;
- `wakeupProbe.httpGet.path` to your readiness route.

Keep `metadata.atespace`, `workerSelector`, `resources` and
`snapshotConfig`. Use only these placeholders; any other `${...}` fails the
deploy:

| Placeholder | Filled with |
|---|---|
| `${IMAGE_WITH_DIGEST}` | the image from step 2, pinned |
| `${ACTOR_MEMORY_BYTES}`, `${ACTOR_CPU_MILLICORES}` | the case's `actor-memory`, and 1 CPU |
| `${OTLP_ENDPOINT}` | the control plane's OTLP collector |
| `${PROJECT_ID}` | the cluster's project |

### 4. Register the template with the scenario runner

In [`scenario_runner/src/scenario_runner/workloads/__init__.py`](../scenario_runner/src/scenario_runner/workloads/__init__.py),
add the template and its image key to `TEMPLATE_IMAGES`:

```python
TEMPLATE_IMAGES = {"openclaw": "openclaw-workload", "myapp": "myapp-workload"}
```

In the same file, only if it applies:

- the actors need more than 256Mi: add `myapp` to
  `DEFAULT_ACTOR_MEMORY_BY_WORKLOAD` and `MIN_ACTOR_MEMORY_MIB`;
- the actors call mock-llm: add `myapp` to `MOCK_LLM_TEMPLATES`, and give
  its cases `configuration: openclaw` in step 7.

### 5. Add the Go workload

Create [`locust/go/internal/workload/`](../locust/go/internal/workload/)`myapp.go`.
Replace `/run` and the check in `Verify` with your application's route and
reply:

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
		Method: http.MethodPost,
		Path:   "/run",
		Body:   []byte(nonce),
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

Create `locust/go/internal/workload/myapp_test.go`, following
[`openclaw_test.go`](../locust/go/internal/workload/openclaw_test.go): a fake
server for your routes, and tests that the right request is sent, a wrong
reply fails, a 503 is retried, and `Resolve("myapp")` returns your workload.

### 7. Add a case

In [`scenarios/scenarios.yaml`](../scenarios/scenarios.yaml), add an entry to
the `workload` parameters of the `"{workload}-{control-plane}-{sandbox-class}"`
case:

```yaml
      - workload: myapp
        # Only if the actors call another host; without it they can't reach it.
        actor-egress-allow: myhost.example.svc.cluster.local
```

This creates the cases `myapp-substrate-gvisor` and
`myapp-substrate-microvm`. To write a case of your own instead, set
`workload-name: myapp` in its `locust.worker`; see
[Adding a case](../README.md#adding-a-case).

### 8. Check it

From `benchmarking/locust/go`, run the Go tests:

```bash
go test ./...
```

Then, from the repo root, run the case on a dev infra. The run deploys the
template and fails if the actors don't answer:

```bash
./benchmarking/tools/run --dev-infra myapp-dev --scenario myapp-substrate-gvisor
```

### 9. Optional: agent-sandbox

Copy [`../agent-sandbox/manifests/glutton-template.yaml.tmpl`](../agent-sandbox/manifests/glutton-template.yaml.tmpl)
to `../agent-sandbox/manifests/myapp-template.yaml.tmpl` and change its
container as in step 3. It takes `${NAME}`, `${NAMESPACE}`, `${IMAGE}` and
`${RUNTIME_CLASS}`.

Add the image key to `WORKLOAD_IMAGES` in
[`agent_sandbox.py`](../scenario_runner/src/scenario_runner/control_planes/agent_sandbox.py):

```python
WORKLOAD_IMAGES = {"glutton": "glutton", "openclaw": "openclaw-workload", "myapp": "myapp-workload"}
```

Then add an entry to the parameters of the `"{workload}-agent-sandbox-gvisor"`
case, with `actor-egress-allow` as in step 7 if needed, and run
`myapp-agent-sandbox-gvisor` as in step 8:

```yaml
      - workload: myapp
        vus: 30
```
