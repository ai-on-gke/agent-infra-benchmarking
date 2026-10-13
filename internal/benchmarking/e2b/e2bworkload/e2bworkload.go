// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Package e2bworkload drives an E2B sandbox through its lifecycle under load,
// as the boomer/Locust worker's task. It is the E2B counterpart of substrate's
// runworkload: each VU goroutine creates one sandbox and then cycles it
// resume/exec/pause for the rest of the run, killing it at teardown. Every
// lifecycle operation reports its own Locust/Prometheus row through the same
// bmetrics helpers substrate uses, so an E2B run and a substrate run produce
// comparably-shaped rows — the whole point of the integration.
//
// Operation mapping to substrate's runworkload (latency parity):
//
//	CreateActor  -> CreateSandbox   SuspendActor -> PauseSandbox
//	ResumeActor  -> ResumeSandbox   DeleteActor  -> KillSandbox
//	instruction  -> Exec (envd Process.Start)
//
// Two windows carry the same names as substrate because they measure the same
// thing: TTFI (resume wait) and CycleTime (resume+exec+pause).
//
// E2B differs from substrate in one way the runtime has to absorb: create boots
// a *running* microVM, whereas CreateActor leaves the actor suspended. To make
// every cycle start from the same paused baseline substrate's does, startUser
// pauses once after create; that setup pause is unmetered, so the CreateSandbox
// row stays a clean create-latency figure comparable to CreateActor.
package e2bworkload

import (
	"context"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/boomerutil"
	bmetrics "github.com/agent-substrate/substrate/internal/benchmarking/boomer/metrics"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/agent-substrate/substrate/agent-infra-benchmarking/internal/benchmarking/e2b/control"
	"github.com/agent-substrate/substrate/agent-infra-benchmarking/internal/benchmarking/e2b/envd"
)

// E2BUserClass is the Locust user class name this task registers as; the
// worker's boomer.Task.Name must equal it, and the Locust master must define a
// user class of the same name. It mirrors runworkload's RunWorkloadUser.
const E2BUserClass = "E2BLifecycleUser"

// Row names. The lifecycle ops use E2B-native names (not substrate's) so a
// chart never silently overlays operations with different semantics; the
// cross-mapping to substrate's names is a reporting-layer concern. The two
// window names are shared with substrate on purpose — they measure the same
// thing on both sides.
const (
	createMetric = "CreateSandbox"
	resumeMetric = "ResumeSandbox"
	pauseMetric  = "PauseSandbox"
	killMetric   = "KillSandbox"
	execMetric   = "Exec"

	ttfiMetric      = "TTFI"
	cycleTimeMetric = "CycleTime"
)

// bmetrics "method" (transport) labels. REST for the control plane, envd for
// the in-sandbox Process.Start, and e2b for the composite windows whose span
// crosses both.
const (
	methodREST = "rest"
	methodEnvd = "envd"
	methodE2B  = "e2b"
)

const (
	// teardownConcurrency caps the parallel kills at teardown, matching
	// runworkload: every VU's sandbox is removed at once and an unbounded burst
	// would hammer the E2B API while it is still draining the run's traffic.
	teardownConcurrency = 16

	// drainTimeout bounds the wait for in-flight iterations before teardown
	// walks the sandbox set. It must clear the longest a single iteration
	// legitimately runs (a resume+exec+pause), while staying well under the
	// worker's overall teardown budget.
	drainTimeout = 90 * time.Second
)

// controlClient is the slice of the E2B control-plane client the runtime uses.
// Declared as an interface so the cycle logic can be driven by a fake in tests;
// *control.Client satisfies it.
type controlClient interface {
	Create(ctx context.Context, req *control.CreateRequest) (*control.Sandbox, error)
	Pause(ctx context.Context, sandboxID string) error
	Resume(ctx context.Context, sandboxID string, timeout int) (*control.Sandbox, error)
	Kill(ctx context.Context, sandboxID string) error
}

// execClient runs one instruction inside a sandbox. *envd.Client satisfies it.
type execClient interface {
	Exec(ctx context.Context, command string) (*envd.ExecResult, error)
}

// execFactory builds the execClient for a sandbox's current connection details.
// It exists so resume can rebuild the client from a refreshed token/domain, and
// so tests can substitute a fake without a live sandbox.
type execFactory func(sandboxID, domain, accessToken string) execClient

// Config holds the dependencies the runtime needs, built once at startup.
type Config struct {
	// Control is the shared E2B control-plane client (one per worker).
	Control controlClient
	// HTTPClient is the shared HTTP client envd ConnectRPC calls ride on. It
	// backs the default exec factory; unused when newExec is overridden.
	HTTPClient *http.Client
	// Domain is the base domain used to reach a sandbox when the create/resume
	// response does not carry its own.
	Domain string
	// Template is the E2B template every sandbox is created from.
	Template string
	// TTL is the sandbox time-to-live in seconds, applied on create and resume.
	TTL int
	// Tracer anchors sampled spans; the otel global is used when nil.
	Tracer trace.Tracer

	// newExec builds the envd client for a sandbox. Defaulted by New to the
	// real envd client over HTTPClient; set by tests to inject a fake.
	newExec execFactory
}

// Options are the per-run knobs the worker parses from flags.
type Options struct {
	// MinWait and MaxWait bound the think time between iterations; a MaxWait at
	// or below MinWait waits MinWait exactly.
	MinWait time.Duration
	MaxWait time.Duration
	// OpTimeout caps any single control-plane or envd call.
	OpTimeout time.Duration
	// Command is the instruction run inside the sandbox each cycle, via
	// /bin/sh -c; it is the measured Exec round-trip.
	Command string
}

// Runtime is the worker's shared state: one per process, its Iterate method run
// concurrently on every VU goroutine.
type Runtime struct {
	cfg  Config
	opts Options

	users    sync.Map // goroutineID -> *user
	inflight inflight
}

// New builds a Runtime. A nil Config.Tracer falls back to the otel global, and
// a nil exec factory to the real envd client over Config.HTTPClient.
func New(cfg Config, opts Options) *Runtime {
	if cfg.Tracer == nil {
		cfg.Tracer = otel.Tracer("e2b-boomer/e2bworkload")
	}
	if cfg.newExec == nil {
		hc := cfg.HTTPClient
		cfg.newExec = func(sandboxID, domain, accessToken string) execClient {
			return envd.NewClient(sandboxID, domain, accessToken, hc)
		}
	}
	return &Runtime{cfg: cfg, opts: opts}
}

func (r *Runtime) dynamicWait() time.Duration {
	if r.opts.MaxWait <= r.opts.MinWait {
		return r.opts.MinWait
	}
	jitter := r.opts.MaxWait - r.opts.MinWait
	return r.opts.MinWait + time.Duration(rand.Float64()*float64(jitter))
}

// Iterate is the task boomer calls in a loop on each VU goroutine. The first
// call on a goroutine creates (and pauses) the sandbox; every later one runs
// one resume/exec/pause cycle. The inflight gate is held for the whole
// iteration so teardown can tell a half-created sandbox from a finished one; a
// refused entry means the run is tearing down and the only correct move is to
// return, since a sandbox created past that point would miss the teardown walk.
func (r *Runtime) Iterate() {
	if !r.inflight.enter() {
		return
	}
	defer r.inflight.leave()

	ctx := context.Background()
	gid := boomerutil.GoroutineID()

	val, loaded := r.users.Load(gid)
	if !loaded {
		u, err := r.startUser(ctx)
		if err != nil {
			slog.Warn("e2b start failed; goroutine will retry next iteration",
				slog.String("err", err.Error()))
			time.Sleep(r.dynamicWait())
			return
		}
		if _, loaded := r.users.LoadOrStore(gid, u); loaded {
			// Another user already holds this goroutine id, so this sandbox
			// would never be reached again. Kill it now rather than leak a
			// billable sandbox for the rest of the run.
			_ = u.kill(ctx)
			bmetrics.UpdateUsers(E2BUserClass, -1)
		}
		time.Sleep(r.dynamicWait())
		return
	}
	u := val.(*user)

	if call, err := u.cycle(ctx); err != nil {
		r.discard(ctx, gid, u, call, err)
		time.Sleep(r.dynamicWait())
		return
	}
	time.Sleep(r.dynamicWait())
}

// startUser creates a sandbox and pauses it to the baseline every cycle starts
// from. A failure after create kills the sandbox so a half-set-up VU does not
// leak one.
func (r *Runtime) startUser(ctx context.Context) (*user, error) {
	// domain is seeded from the base config; applyConn overrides it only when a
	// create/resume response carries its own.
	u := &user{cfg: r.cfg, opts: r.opts, domain: r.cfg.Domain}
	bmetrics.UpdateUsers(E2BUserClass, 1)

	if err := u.create(ctx); err != nil {
		bmetrics.UpdateUsers(E2BUserClass, -1)
		return nil, err
	}
	if err := u.pauseSetup(ctx); err != nil {
		_ = u.kill(ctx)
		bmetrics.UpdateUsers(E2BUserClass, -1)
		return nil, err
	}
	return u, nil
}

// discard throws away a sandbox whose state the run can no longer reason about:
// a failed call can leave it mid-transition and nothing reconciles it. The slot
// is freed whether or not the kill worked, because a VU that cannot kill its
// sandbox still has to stop using it; a kill that failed is named, since that
// sandbox bills until someone removes it.
func (r *Runtime) discard(ctx context.Context, gid int64, u *user, call string, cause error) {
	slog.Warn("discarding sandbox after a failed call",
		slog.String("sandbox", u.id),
		slog.String("call", call),
		slog.String("err", cause.Error()))

	if err := u.kill(ctx); err != nil {
		slog.Warn("discarded sandbox was left behind",
			slog.String("sandbox", u.id),
			slog.String("err", err.Error()))
	}
	r.users.Delete(gid)
	bmetrics.UpdateUsers(E2BUserClass, -1)
}

// Shutdown kills every sandbox the run created and reports per sandbox whether
// it went, so a leak always surfaces. It first drains the task function, since
// boomer.Run returns while iterations are still in flight and the walk must run
// after the last sandbox is recorded. Kills go in parallel, capped at
// teardownConcurrency.
func (r *Runtime) Shutdown(ctx context.Context) {
	drainCtx, cancel := context.WithTimeout(ctx, drainTimeout)
	defer cancel()
	if !r.inflight.drain(drainCtx) {
		slog.Warn("teardown drain timed out; tearing down with iterations still in flight",
			slog.Duration("drain_timeout", drainTimeout))
	}

	var users []*user
	r.users.Range(func(_, val any) bool {
		users = append(users, val.(*user))
		return true
	})

	var removed, left atomic.Int64
	var wg sync.WaitGroup
	sem := make(chan struct{}, teardownConcurrency)
	for _, u := range users {
		sem <- struct{}{}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-sem }()

			err := u.kill(ctx)
			bmetrics.UpdateUsers(E2BUserClass, -1)
			if err != nil {
				left.Add(1)
				slog.Warn("teardown left a sandbox behind",
					slog.String("sandbox", u.id),
					slog.String("err", err.Error()))
				return
			}
			removed.Add(1)
		}()
	}
	wg.Wait()

	slog.Info("teardown finished",
		slog.Int64("removed", removed.Load()),
		slog.Int64("left", left.Load()))
}

// user is one VU's sandbox and the connection details to reach its envd. The
// details are refreshed on every resume, since E2B may hand back a fresh envd
// access token and serving domain when it wakes a sandbox.
type user struct {
	cfg  Config
	opts Options

	// id is the sandbox identifier, set once in create and never rewritten.
	// Teardown's kill runs on a different goroutine than the VU's cycle, so it
	// reads only this write-once field — never the connection details below,
	// which a concurrent resume may be mutating when the drain times out.
	id string

	// Connection details, refreshed on each resume and touched only by the
	// VU's own goroutine (create, resume, exec).
	domain      string
	accessToken string
	envd        execClient
}

// applyConn refreshes the connection details from a create/resume response and
// rebuilds the envd client. It overwrites the domain and token only when the
// response actually carries them, so a partial resume body cannot clobber a
// still-valid token or domain with an empty one. The sandbox id is not touched
// here — create sets it once.
func (u *user) applyConn(sb *control.Sandbox) {
	if sb.Domain != nil && *sb.Domain != "" {
		u.domain = *sb.Domain
	}
	if sb.EnvdAccessToken != "" {
		u.accessToken = sb.EnvdAccessToken
	}
	u.envd = u.cfg.newExec(u.id, u.domain, u.accessToken)
}

// create boots the sandbox and records it as the CreateSandbox row.
func (u *user) create(ctx context.Context) error {
	return u.metered(ctx, methodREST, createMetric, func(callCtx context.Context) error {
		sb, err := u.cfg.Control.Create(callCtx, &control.CreateRequest{
			TemplateID: u.cfg.Template,
			Timeout:    u.cfg.TTL,
		})
		if err != nil {
			return err
		}
		u.id = sb.SandboxID
		u.applyConn(sb)
		return nil
	})
}

// pauseSetup pauses a freshly-created sandbox to reach the baseline every cycle
// starts from. Unmetered: it is setup to reach steady state, not a benchmarked
// op — the metered pauses are the ones inside the cycle.
func (u *user) pauseSetup(ctx context.Context) error {
	callCtx, cancel := context.WithTimeout(ctx, u.opts.OpTimeout)
	defer cancel()
	return u.cfg.Control.Pause(callCtx, u.id)
}

// cycle runs one resume/exec/pause cycle and reports its whole duration as
// CycleTime. Its span parents the TTFI span so a trace shows the cycle as one
// unit. A failed call reports the window as a failure and names that call, for
// the sandbox to be discarded.
func (u *user) cycle(ctx context.Context) (call string, err error) {
	ctx, span := u.cfg.Tracer.Start(ctx, cycleTimeMetric)
	defer span.End()

	start := time.Now()
	defer func() { u.recordWindow(span, cycleTimeMetric, time.Since(start), err) }()

	if err := u.measureTTFI(ctx); err != nil {
		return ttfiMetric, err
	}
	if err := u.pause(ctx); err != nil {
		return pauseMetric, err
	}
	return "", nil
}

// measureTTFI resumes the sandbox, sends it one instruction, and reports the
// wait as TTFI. The window closes the instant the resume returns — serving the
// instruction is the application's cost, not E2B's — so TTFI is the resume the
// client waited through and Exec is its own separate row. Either call failing
// reports the window as a failure over the wait it spent.
func (u *user) measureTTFI(ctx context.Context) error {
	ctx, span := u.cfg.Tracer.Start(ctx, ttfiMetric)
	defer span.End()
	span.SetAttributes(attribute.String("template", u.cfg.Template))

	start := time.Now()
	if err := u.resume(ctx); err != nil {
		u.recordWindow(span, ttfiMetric, time.Since(start), err)
		return err
	}

	total := time.Since(start)
	if err := u.exec(ctx); err != nil {
		u.recordWindow(span, ttfiMetric, time.Since(start), err)
		return err
	}

	u.recordWindow(span, ttfiMetric, total, nil)
	return nil
}

// resume wakes the sandbox and refreshes its connection details, since a resume
// may rotate the envd access token or move the sandbox to a new serving domain.
func (u *user) resume(ctx context.Context) error {
	return u.metered(ctx, methodREST, resumeMetric, func(callCtx context.Context) error {
		sb, err := u.cfg.Control.Resume(callCtx, u.id, u.cfg.TTL)
		if err != nil {
			return err
		}
		// A resume that answers with no body leaves the pre-pause details in
		// place; one that returns a sandbox may carry a fresh token/domain.
		if sb != nil {
			u.applyConn(sb)
		}
		return nil
	})
}

// exec sends the configured instruction through envd and records it as the Exec
// row. A non-zero exit is a failure: the sandbox answered but did not run the
// instruction cleanly, which is a fault to surface, not a sample to keep.
func (u *user) exec(ctx context.Context) error {
	return u.metered(ctx, methodEnvd, execMetric, func(callCtx context.Context) error {
		res, err := u.envd.Exec(callCtx, u.opts.Command)
		if err != nil {
			return err
		}
		if res.ExitCode != 0 {
			return fmt.Errorf("instruction exited %d: %s", res.ExitCode, res.Stderr)
		}
		return nil
	})
}

// pause suspends the sandbox and records it as the PauseSandbox row.
func (u *user) pause(ctx context.Context) error {
	return u.metered(ctx, methodREST, pauseMetric, func(callCtx context.Context) error {
		return u.cfg.Control.Pause(callCtx, u.id)
	})
}

// kill deletes the sandbox and records it as the KillSandbox row. E2B kill
// works from any state, so unlike substrate's delete it needs no suspend
// precondition.
func (u *user) kill(ctx context.Context) error {
	return u.metered(ctx, methodREST, killMetric, func(callCtx context.Context) error {
		return u.cfg.Control.Kill(callCtx, u.id)
	})
}

// metered runs one lifecycle call under a per-op timeout, times it, and reports
// the row and span. E2B calls carry no server-side elapsed trailer, so the
// latency reported is client wall clock — the round-trip a real caller sees.
func (u *user) metered(ctx context.Context, method, name string, do func(context.Context) error) error {
	ctx, span := u.cfg.Tracer.Start(ctx, name)
	defer span.End()

	callCtx, cancel := context.WithTimeout(ctx, u.opts.OpTimeout)
	defer cancel()

	start := time.Now()
	err := do(callCtx)
	report(span, method, name, time.Since(start), err)
	return err
}

// recordWindow closes out a composite window (TTFI, CycleTime) reported under
// the e2b method label, as a success or as a failure over the wait it spent.
func (u *user) recordWindow(span trace.Span, name string, total time.Duration, err error) {
	span.SetAttributes(attribute.Float64("total_ms", boomerutil.MsFloat(total)))
	report(span, methodE2B, name, total, err)
}

// report emits one completed operation's sampled trace and its Locust/Prometheus
// row, as a success or a failure over latency. Shared by metered (per-call) and
// recordWindow (composite windows) so the row/trace shape stays identical.
func report(span trace.Span, method, name string, latency time.Duration, err error) {
	boomerutil.LogSampledTrace(span, name, latency, boomerutil.SourceClient, err)
	if err != nil {
		bmetrics.RecordFailure(method, name, E2BUserClass, latency, err.Error())
		return
	}
	bmetrics.RecordSuccess(method, name, E2BUserClass, latency, 0)
}
