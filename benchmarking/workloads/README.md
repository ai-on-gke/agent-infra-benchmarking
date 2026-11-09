# Workloads

A workload is the application inside each actor that the boomer worker sends
instructions to. Today there are two: `glutton` and `openclaw`. This
directory holds their images (`images/`, `images.json`) and their
ActorTemplates (`manifests/`).

## Adding a workload

You add a workload in four places: the actor image, its ActorTemplate, the
scenario runner's tables, and a Go type in the boomer worker. Below, `myapp`
stands for the new workload's name. Use the same name everywhere, because the
scenario runner finds each file by it. Paths are relative to this directory.

### Building the actor image

Put a `Dockerfile` and anything it copies in `images/myapp/`. Then add an
entry to [`images.json`](images.json); its key is the image name the later
steps refer to. The application has to:

- Serve on port 80. That is the only inbound port the worker forwards into
  the sandbox.
- Answer a readiness route, such as `/readyz`, which the template's
  `wakeupProbe` polls.
- Give an answer that proves it ran the instruction. Echoing a nonce sent in
  the request is the best proof: glutton's `/ping` does that, and openclaw's
  reply carries the nonce back.

### Writing the ActorTemplate

Add `manifests/myapp-template.yaml.tmpl`. Copy
[`openclaw-template.yaml.tmpl`](manifests/openclaw-template.yaml.tmpl) and
keep its `metadata.atespace`, its `workerSelector` and its `resources` block.
The scenario runner fills these placeholders:

| Placeholder | Value |
|---|---|
| `${IMAGE_WITH_DIGEST}` | the image built above, pinned (needs the `TEMPLATE_IMAGES` entry below) |
| `${ACTOR_MEMORY_BYTES}`, `${ACTOR_CPU_MILLICORES}` | the case's `actor-memory`, and 1 CPU |
| `${OTLP_ENDPOINT}` | the control plane's OTLP collector |
| `${PROJECT_ID}` | the cluster's project |

Any other `${...}` in the template makes the deploy fail, so a new
placeholder needs its own code in `render_template` in
[`workloads/__init__.py`](../scenario_runner/src/scenario_runner/workloads/__init__.py),
the way `OPENCLAW_TIMEOUT_SECS` has. Actors may call only the hosts named in
URLs in the container's `env`; the worker allows those hosts, and substrate
refuses connections to any other host.

### Registering it with the scenario runner

In [`workloads/__init__.py`](../scenario_runner/src/scenario_runner/workloads/__init__.py):

- Add `"myapp": "<images.json key>"` to `TEMPLATE_IMAGES`.
- If the actors need more than 256Mi, add the workload to
  `DEFAULT_ACTOR_MEMORY_BY_WORKLOAD` and `MIN_ACTOR_MEMORY_MIB`.
- If the actors call mock-llm, add the workload to `MOCK_LLM_TEMPLATES`. Its
  cases then need `configuration: openclaw`, the cluster with mock-llm's
  pool.

### Driving it from the boomer worker

Add `myapp.go` to [`locust/go/internal/workload/`](../locust/go/internal/workload/).
In it, write a type that implements `Workload` (in `workload.go`), and
register it in `init`:

```go
const (
	myappWorkloadName = "myapp"
	myappTemplate     = "myapp" // the name in myapp-template.yaml.tmpl
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

Send the request with `sendHTTPInstruction`. It addresses the actor through
the router, propagates the trace, and sorts failures into retryable (5xx,
429, 421) and fatal. `SendInstruction` must not return a nil error until the
actor has actually run the instruction, because a nil error ends the
measurement. To let cases use `glutton-write-ram` and `glutton-write-disk`,
also implement `Loader`; without it, the worker skips that step. The master
needs no change: it runs `runworkload` for every workload.

Add `myapp_test.go` next to the new file. Its fake server is built the way
`openclaw_test.go` builds one, and it checks the request, the verification,
a 503 being retried, and `Resolve("myapp")`. Then, in `benchmarking/locust/go`,
run:

```bash
go test ./...
```

### Running it on agent-sandbox too

For agent-sandbox cases, also add
`../agent-sandbox/manifests/myapp-template.yaml.tmpl`, a SandboxTemplate that
takes `${NAME}`, `${NAMESPACE}`, `${IMAGE}` and `${RUNTIME_CLASS}`.
[`glutton-template.yaml.tmpl`](../agent-sandbox/manifests/glutton-template.yaml.tmpl)
there is the one to copy. Then add `"myapp": "<images.json key>"` to
`WORKLOAD_IMAGES` in
[`agent_sandbox.py`](../scenario_runner/src/scenario_runner/control_planes/agent_sandbox.py).

### Adding a case that runs it

Add `- workload: myapp` to the `workload` parameters of the
`"{workload}-{control-plane}-{sandbox-class}"` case in
[`scenarios.yaml`](../scenarios/scenarios.yaml). That case sets
`actor-egress-allow` itself, so if your actors call a host, give the
parameter entry that host, as openclaw's entry does. For agent-sandbox, add
the workload to the `"{workload}-agent-sandbox-gvisor"` case as well. You can
also write a case of your own whose `locust.worker` sets
`workload-name: myapp`; [Adding a case](../README.md#adding-a-case) covers
the rest.

Then, from the repo root, run it on a dev infra:

```bash
./benchmarking/tools/run --dev-infra myapp-dev --scenario myapp-substrate-gvisor
```
