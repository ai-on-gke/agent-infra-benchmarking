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

// Package runworkload drives a sandbox through one of two provisioning
// backends and measures the wall clock a client waits before it accepts
// work. --backend picks which:
//
//   - substrate (default, for backward compatibility): the original
//     behavior — an actor cycling through substrate's ResumeActor/
//     SuspendActor control-plane RPCs, addressed through the atenet router.
//     FirstResume is the actor's first activation; WarmStart resumes that
//     same actor's own prior live-memory snapshot (substrate's ~100ms
//     activation target).
//   - agent-sandbox: sigs.k8s.io/agent-sandbox instead. ColdCreate creates a
//     Sandbox directly from a SandboxTemplate's blueprint, bypassing any
//     warm pool — a true cold boot. PoolClaim creates a SandboxClaim against
//     a SandboxWarmPool, adopting an already-running pod — cheaper
//     provisioning, but NOT a live-memory restore, so it is not the same
//     latency class as substrate's WarmStart. See the README for the full
//     substrate/agent-sandbox mapping and what does not carry over.
//
// Both backends drive the same window/retry/span machinery below
// (runWorkloadUser.measure, firstInstruction); only how a window is
// provisioned (backend.first/warm/teardown) differs. --workload-name selects
// an application to run inside the sandbox (workload.go); each workload
// targets exactly one backend (glutton/openclaw: substrate, ping:
// agent-sandbox), so switching --backend also constrains which
// --workload-name values are valid.
package runworkload

import (
	"context"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"os"
	"sync"
	"time"

	"github.com/agent-substrate/substrate/internal/ateinterceptors"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/boomerutil"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/dynconfig"
	bmetrics "github.com/agent-substrate/substrate/internal/benchmarking/boomer/metrics"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/userclass"
	"github.com/agent-substrate/substrate/pkg/proto/ateapipb"
	"github.com/google/uuid"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	sandboxv1beta1 "sigs.k8s.io/agent-sandbox/api/v1beta1"
	sandboxclientset "sigs.k8s.io/agent-sandbox/clients/k8s/clientset/versioned"
	extensionsclientset "sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned"
	extensionsv1beta1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

// Backend selects which provisioning control plane runworkload drives.
type Backend string

const (
	BackendSubstrate    Backend = "substrate"
	BackendAgentSandbox Backend = "agent-sandbox"

	// DefaultBackend preserves this benchmark's original behavior for a
	// caller that never sets --backend.
	DefaultBackend = BackendSubstrate
)

// The atespace workloads/deploy.sh creates its substrate ActorTemplates in.
// Substrate-only; agent-sandbox's SandboxTemplate lives in AgentSandboxDeps.Namespace instead.
const templateAtespace = "benchmark-workloads"

const (
	// Locust class name from tests/runworkload.py; must match boomer.Task.Name.
	runWorkloadUserClass = "RunWorkloadUser"

	// DefaultWorkload is substrate's default, kept for backward
	// compatibility. Actual resolution goes through defaultWorkloadFor,
	// which also covers agent-sandbox (ping).
	DefaultWorkload = gluttonWorkloadName

	// DefaultTimeout bounds the instruction retry loop when Options leaves it
	// zero. A sandbox that has not taken an instruction by then is recorded
	// as a failure instead of being waited on for the rest of the run.
	DefaultTimeout = 60 * time.Second

	// DefaultProvisionTimeout bounds the agent-sandbox backend's wait for a
	// Sandbox/SandboxClaim's Ready condition when Options leaves it zero.
	// Substrate has no equivalent: ResumeActor is a single RPC, not a poll
	// loop. Generous relative to DefaultTimeout because a cold create can
	// include a real image pull.
	DefaultProvisionTimeout = 5 * time.Minute

	// Delay between instruction attempts, short relative to a resume/claim
	// so the retry granularity does not show up in the measurement.
	retryDelay = 25 * time.Millisecond

	// Delay between agent-sandbox Ready-condition polls. Provisioning takes
	// seconds, not milliseconds, so this can be coarser than retryDelay.
	provisionPollInterval = 500 * time.Millisecond

	// probePort is the port the agent-sandbox ping workload's container
	// listens on. Fixed rather than configurable: it is part of the
	// contract between that workload and the SandboxTemplate it names.
	probePort int32 = 8888

	firstResumeMetric = "FirstResume"
	warmStartMetric   = "WarmStart"
	resumeMetric      = "Resume"
	instructionMetric = "Instruction"
	suspendMetric     = "Suspend"

	coldCreateMetric = "ColdCreate"
	poolClaimMetric  = "PoolClaim"
	provisionMetric  = "Provision"

	// Transport/method label for the composite rows. A measurement window
	// spans a provisioning step (a substrate gRPC resume or an agent-sandbox
	// k8s create+wait) and an HTTP instruction, so neither transport label
	// the sub-spans use describes it.
	runWorkloadMethod = "runworkload"
)

// Options are the runworkload-only settings of a run. They are handed to
// this package rather than parsed by it: every flag the worker accepts is
// declared in cmd/benchmarking/boomer-worker, so one --help lists them all and
// a user class has no command-line surface of its own to keep in sync.
//
// The zero value asks for the defaults throughout (substrate backend,
// glutton workload).
type Options struct {
	// Backend selects the provisioning control plane. Empty selects
	// DefaultBackend (substrate).
	Backend Backend

	// Workload names a registered workload (see workload.go). Empty selects
	// the chosen backend's own default.
	Workload string

	// Template overrides the ActorTemplate (substrate) or SandboxTemplate
	// (agent-sandbox) the workload would measure. Empty leaves that choice
	// to the workload, which is the only place that knows what it can
	// address.
	Template string

	// WarmPool overrides the SandboxWarmPool the agent-sandbox backend's
	// pool-claim window adopts from. Empty selects "<template>-warmpool".
	// Ignored by the substrate backend.
	WarmPool string

	// Timeout is the ceiling on the instruction retry loop. Zero selects
	// DefaultTimeout. Negative is a caller error, rejected where the flag is
	// declared rather than silently treated as unset.
	Timeout time.Duration

	// ProvisionTimeout is the ceiling on the agent-sandbox backend's wait
	// for a Ready condition. Zero selects DefaultProvisionTimeout. Ignored
	// by the substrate backend.
	ProvisionTimeout time.Duration
}

// configured is what SetOptions recorded. It is read exactly once, by
// initRunWorkload, and passed by value from there on, so nothing on the
// measurement path reads mutable package state.
var configured Options

// SetOptions records the settings initRunWorkload will use. It must run
// before the registered entry's Init, which main satisfies by calling it
// after flag.Parse and before the userclass lookup. A setter rather than an
// Init parameter because the registry hands every user class the same
// userclass.Config, which is substrate's own type and carries no runworkload
// or agent-sandbox fields.
func SetOptions(o Options) { configured = o }

// AgentSandboxDeps carries the agent-sandbox backend's dependencies.
// substrate's userclass.Config (an external type this package does not own)
// has no fields for them, so — like Options — they arrive via a setter
// rather than through the registry's Init parameter. Only read when the
// resolved backend is BackendAgentSandbox.
type AgentSandboxDeps struct {
	Sandboxes  sandboxclientset.Interface
	Extensions extensionsclientset.Interface
	Namespace  string
}

var configuredASDeps AgentSandboxDeps

// SetAgentSandboxDeps records the agent-sandbox clients initRunWorkload will
// use. Must run before boomer.Run, same as SetOptions. A no-op call (zero
// value) is fine when --backend substrate is selected; the agent-sandbox
// backend is never constructed in that case.
func SetAgentSandboxDeps(d AgentSandboxDeps) { configuredASDeps = d }

func init() {
	userclass.Add(userclass.Entry{
		Name:       "runworkload",
		LocustFile: "runworkload.py",
		UserClass:  runWorkloadUserClass,
		Init:       initRunWorkload,
	})
}

// initRunWorkload creates a runtime tied to cfg and returns a
// boomer-compatible task function plus a shutdown hook the caller should run
// before exit.
func initRunWorkload(cfg *userclass.Config) (taskFn func(), shutdown func(context.Context)) {
	if cfg.Tracer == nil {
		// Scope names the benchmark, not the backend or workload: the same
		// spans are emitted whichever combination is measured.
		cfg.Tracer = otel.Tracer("substrate-boomer/runworkload")
	}
	backend := configured.Backend
	if backend == "" {
		backend = DefaultBackend
	}
	// Resolved once at startup rather than per iteration: an unselectable
	// workload is a configuration mistake, and a run that reports it after
	// producing rows has already wasted the run.
	wl, err := resolveWorkload(configured.Workload, backend)
	if err != nil {
		slog.Error("fatal: cannot select the workload",
			slog.String("err", err.Error()))
		os.Exit(1)
	}
	rt := &runWorkloadRuntime{cfg: cfg, opts: configured, backend: backend, workload: wl}
	return rt.iterate, rt.shutdown
}

// resolveTimeout returns configured if positive, else def.
func resolveTimeout(configured, def time.Duration) time.Duration {
	if configured > 0 {
		return configured
	}
	return def
}

type runWorkloadRuntime struct {
	cfg      *userclass.Config
	opts     Options
	backend  Backend
	workload workload
	users    sync.Map // goroutineID -> *runWorkloadUser
}

func (r *runWorkloadRuntime) dynamicWait() time.Duration {
	cfg := r.cfg.Dyn.Load()
	if cfg.MaxWait <= cfg.MinWait {
		return cfg.MinWait
	}
	jitter := cfg.MaxWait - cfg.MinWait
	return cfg.MinWait + time.Duration(rand.Float64()*float64(jitter))
}

// newBackend builds the sandboxBackend for one VU. Each VU gets its own
// instance because per-actor state (substrate's actor name) or per-claim
// state (agent-sandbox's current Sandbox/SandboxClaim name) lives on it.
func (r *runWorkloadRuntime) newBackend(templateName string) sandboxBackend {
	switch r.backend {
	case BackendAgentSandbox:
		pool := r.opts.WarmPool
		if pool == "" {
			pool = templateName + "-warmpool"
		}
		return &agentSandboxBackend{
			cfg:              r.cfg,
			asDeps:           configuredASDeps,
			templateName:     templateName,
			warmPoolName:     pool,
			provisionTimeout: resolveTimeout(r.opts.ProvisionTimeout, DefaultProvisionTimeout),
		}
	case BackendSubstrate:
		return &substrateBackend{
			cfg:          r.cfg,
			actorName:    "sb-" + uuid.NewString(),
			templateName: templateName,
		}
	default:
		// Unreachable: main rejects any --backend outside the known set at
		// startup, and initRunWorkload defaults the empty value to substrate, so
		// r.backend is always one of the cases above. Panic on the invariant
		// rather than silently building a substrateBackend for an unknown value,
		// which would drive the wrong control plane.
		panic(fmt.Sprintf("runworkload: unknown backend %q", r.backend))
	}
}

// iterate is the task function boomer calls in a loop on each VU goroutine.
// The first call on a goroutine creates the sandbox and measures its first
// window; every later call prepares for (substrate: suspends; agent-sandbox:
// releases) and measures the warm window.
func (r *runWorkloadRuntime) iterate() {
	gid := boomerutil.GoroutineID()
	val, loaded := r.users.Load(gid)
	if !loaded {
		u, err := r.startUser(context.Background(), r.cfg.Dyn.Load(), r.opts)
		if err != nil {
			slog.Warn("runworkload on_start failed; goroutine will retry next iter",
				slog.String("err", err.Error()))
			time.Sleep(r.dynamicWait())
			return
		}
		if _, loaded := r.users.LoadOrStore(gid, u); loaded {
			// A user is already registered under this goroutine id, so the
			// sandbox just created would never be reached again — and never
			// torn down. Drop it now instead of leaking it for the rest of
			// the run.
			u.backend.teardown(context.Background())
			bmetrics.UpdateUsers(runWorkloadUserClass, -1)
		}
		// The cold window was measured inside startUser. Returning here
		// keeps one iteration to one measurement, so the two rows stay
		// independent samples rather than a cold and a warm number
		// averaged into one iteration's think time.
		time.Sleep(r.dynamicWait())
		return
	}
	user := val.(*runWorkloadUser)

	dynCfg := r.cfg.Dyn.Load()
	ctx := context.Background()
	// prepareWarm is the precondition for a warm measurement, not part of
	// it: it runs outside the measured span and reports its own row.
	user.backend.prepareWarm(ctx)
	user.measure(ctx, user.backend.warmMetricName(), r.opts, false, func(ctx context.Context) (actorRef, time.Duration, error) {
		return user.backend.warm(ctx, dynCfg)
	})

	time.Sleep(r.dynamicWait())
}

func (r *runWorkloadRuntime) startUser(ctx context.Context, dynCfg dynconfig.Config, opts Options) (*runWorkloadUser, error) {
	tmpl := opts.Template
	if tmpl == "" {
		tmpl = r.workload.DefaultTemplate()
	}

	b := r.newBackend(tmpl)
	u := &runWorkloadUser{
		cfg:      r.cfg,
		workload: r.workload,
		backend:  b,
	}
	bmetrics.UpdateUsers(runWorkloadUserClass, 1)
	if err := b.prepare(ctx); err != nil {
		bmetrics.UpdateUsers(runWorkloadUserClass, -1)
		return nil, err
	}

	// A failed first window is a result, not a reason to drop the sandbox:
	// it is already recorded as a first-window failure, and the next
	// iteration's prepareWarm normalizes whatever state was left behind.
	u.measure(ctx, b.firstMetricName(), opts, true, func(ctx context.Context) (actorRef, time.Duration, error) {
		return b.first(ctx, dynCfg)
	})
	return u, nil
}

func (r *runWorkloadRuntime) shutdown(ctx context.Context) {
	r.users.Range(func(_, val any) bool {
		u := val.(*runWorkloadUser)
		u.backend.teardown(ctx)
		bmetrics.UpdateUsers(runWorkloadUserClass, -1)
		return true
	})
}

// sandboxBackend abstracts the provisioning control plane a runWorkloadUser
// drives. Exactly two implementations exist: substrateBackend and
// agentSandboxBackend. Each owns whatever per-VU state its backend needs
// (substrate: one actor's name, resumed and suspended in place across
// iterations; agent-sandbox: the currently-live Sandbox or SandboxClaim
// name, deleted and recreated each iteration).
type sandboxBackend interface {
	// name identifies the backend for span/log attributes.
	name() string
	// template is the ActorTemplate/SandboxTemplate name this backend measures.
	template() string
	// firstMetricName/warmMetricName name the two measurement windows.
	firstMetricName() string
	warmMetricName() string

	// prepare runs once, before the first window, and is not itself
	// measured: substrate must register the actor before it can be resumed;
	// agent-sandbox needs nothing (the Sandbox is created inside first()
	// itself, as part of what's measured). A non-nil error here is a setup
	// failure that prevents the VU from starting at all, not a first-window
	// failure.
	prepare(ctx context.Context) error
	// first provisions and returns the target for the VU's first window.
	first(ctx context.Context, dynCfg dynconfig.Config) (actorRef, time.Duration, error)
	// prepareWarm runs before every later window, unmeasured but
	// independently traced (substrate: suspend the actor; agent-sandbox:
	// delete the previous claim).
	prepareWarm(ctx context.Context)
	// warm provisions and returns the target for a later window.
	warm(ctx context.Context, dynCfg dynconfig.Config) (actorRef, time.Duration, error)
	// teardown releases everything at VU shutdown.
	teardown(ctx context.Context)
}

type runWorkloadUser struct {
	cfg      *userclass.Config
	workload workload
	backend  sandboxBackend
}

// budget is the ceiling on the instruction retry loop.
func (u *runWorkloadUser) budget(opts Options) time.Duration {
	if opts.Timeout > 0 {
		return opts.Timeout
	}
	return DefaultTimeout
}

// measure times one window and reports it as metricName. The parent
// span's duration is the measurement, so every phase that a client would
// wait through — provisioning and the instruction — runs inside it and
// nothing else does.
func (u *runWorkloadUser) measure(ctx context.Context, metricName string, opts Options, firstWindow bool, provision func(context.Context) (actorRef, time.Duration, error)) {
	ctx, span := u.cfg.Tracer.Start(ctx, metricName)
	defer span.End()
	span.SetAttributes(
		attribute.String("workload", u.workload.Name()),
		attribute.String("template", u.backend.template()),
		attribute.String("backend", u.backend.name()),
		attribute.Bool("first_resume", firstWindow),
	)

	start := time.Now()

	actor, controlPlane, err := provision(ctx)
	if err != nil {
		u.recordWindow(span, metricName, time.Since(start), 0, controlPlane, 0, err)
		return
	}

	attempts, instruction, err := u.firstInstruction(ctx, actor, u.budget(opts))
	u.recordWindow(span, metricName, time.Since(start), attempts, controlPlane, instruction, err)
}

// recordWindow closes out a measurement window: the attributes make one
// sampled trace self-explaining, and the row carries client wall clock. That
// is a deliberate departure from the other benchmarks, which prefer the
// server's elapsed trailer — the gaps between the phases are exactly what the
// benchmark is measuring, and a server-side number cannot see them.
func (u *runWorkloadUser) recordWindow(span trace.Span, metricName string, total time.Duration, attempts int, controlPlane, instruction time.Duration, err error) {
	span.SetAttributes(
		attribute.Float64("total_ms", boomerutil.MsFloat(total)),
		attribute.Int("attempts", attempts),
	)
	if controlPlane > 0 {
		span.SetAttributes(attribute.Float64("control_plane_ms", boomerutil.MsFloat(controlPlane)))
	}
	if instruction > 0 {
		span.SetAttributes(attribute.Float64("instruction_ms", boomerutil.MsFloat(instruction)))
	}
	boomerutil.LogSampledTrace(span, metricName, total, boomerutil.SourceClient, err)
	if err != nil {
		bmetrics.RecordFailure(runWorkloadMethod, metricName, runWorkloadUserClass, total, err.Error())
		return
	}
	bmetrics.RecordSuccess(runWorkloadMethod, metricName, runWorkloadUserClass, total, 0)
}

// firstInstruction drives the workload until one instruction comes back
// correct or the budget elapses, and reports the whole loop as one row. The
// retries are inside the measurement window on purpose: until an instruction
// lands, the client cannot use the sandbox, so a request rejected while the
// sandbox was still activating is part of the wait. Whether a given failure
// is worth retrying is the workload's call, since only it knows what its own
// answers mean.
func (u *runWorkloadUser) firstInstruction(ctx context.Context, actor actorRef, budget time.Duration) (int, time.Duration, error) {
	ctx, span := u.cfg.Tracer.Start(ctx, instructionMetric)
	defer span.End()

	start := time.Now()
	deadline := start.Add(budget)

	var attempts int
	var lastErr error
	for {
		attempts++
		retryable, err := u.workload.SendInstruction(ctx, actor)
		if err == nil {
			latency := time.Since(start)
			span.SetAttributes(attribute.Int("attempts", attempts))
			boomerutil.LogSampledTrace(span, instructionMetric, latency, boomerutil.SourceClient, nil)
			bmetrics.RecordSuccess("http", instructionMetric, runWorkloadUserClass, latency, 0)
			return attempts, latency, nil
		}
		lastErr = err
		if !retryable {
			break
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			lastErr = fmt.Errorf("no instruction accepted within %s (%d attempts), last error: %w", budget, attempts, err)
			break
		}
		time.Sleep(min(retryDelay, remaining))
	}

	latency := time.Since(start)
	span.SetAttributes(attribute.Int("attempts", attempts))
	boomerutil.LogSampledTrace(span, instructionMetric, latency, boomerutil.SourceClient, lastErr)
	bmetrics.RecordFailure("http", instructionMetric, runWorkloadUserClass, latency, lastErr.Error())
	return attempts, latency, lastErr
}

// substrateBackend is the original substrate control plane: one actor,
// created once, then repeatedly suspended and resumed in place.
type substrateBackend struct {
	cfg          *userclass.Config
	actorName    string
	templateName string
}

func (b *substrateBackend) name() string            { return string(BackendSubstrate) }
func (b *substrateBackend) template() string        { return b.templateName }
func (b *substrateBackend) firstMetricName() string { return firstResumeMetric }
func (b *substrateBackend) warmMetricName() string  { return warmStartMetric }

func (b *substrateBackend) ref() *ateapipb.ObjectRef {
	return &ateapipb.ObjectRef{Atespace: b.cfg.Atespace, Name: b.actorName}
}

// actor is the handle the workload addresses. Built per call rather than
// stored so the workload can hold no benchmark state of its own.
func (b *substrateBackend) actor() actorRef {
	return actorRef{
		HTTPClient: b.cfg.HTTPClient,
		RouterURL:  b.cfg.RouterURL,
		Atespace:   b.cfg.Atespace,
		Name:       b.actorName,
	}
}

func (b *substrateBackend) prepare(ctx context.Context) error {
	if err := b.ensureAtespace(ctx); err != nil {
		return err
	}
	return b.create(ctx)
}

func (b *substrateBackend) first(ctx context.Context, dynCfg dynconfig.Config) (actorRef, time.Duration, error) {
	return b.resumeWindow(ctx, dynCfg)
}

func (b *substrateBackend) prepareWarm(ctx context.Context) {
	b.suspend(ctx)
}

func (b *substrateBackend) warm(ctx context.Context, dynCfg dynconfig.Config) (actorRef, time.Duration, error) {
	return b.resumeWindow(ctx, dynCfg)
}

// resumeWindow issues the explicit ResumeActor that opens an explicit-mode
// window (implicit mode issues no wake request at all: the actor stays
// suspended until a request reaches the atenet router, which wakes it while
// the request is parked), and returns the server-side handler duration so
// the caller can attribute how much of the window ateapi owns.
func (b *substrateBackend) resumeWindow(ctx context.Context, dynCfg dynconfig.Config) (actorRef, time.Duration, error) {
	mode := dynCfg.ResumeMode
	if mode == "" {
		// Matches common/resume_mode.py's default, so the mode is the same
		// whether the value arrives from locust or from nothing at all.
		mode = dynconfig.ResumeModeExplicit
	}
	trace.SpanFromContext(ctx).SetAttributes(attribute.String("resume_mode", mode))

	var controlPlane time.Duration
	if mode == dynconfig.ResumeModeExplicit {
		elapsed, err := b.resume(ctx)
		controlPlane = elapsed
		if err != nil {
			return actorRef{}, controlPlane, err
		}
	}
	return b.actor(), controlPlane, nil
}

func (b *substrateBackend) resume(ctx context.Context) (time.Duration, error) {
	var serverElapsed time.Duration
	err := b.tracedCall(ctx, resumeMetric, &serverElapsed, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := b.cfg.APIStub.ResumeActor(callCtx, &ateapipb.ResumeActorRequest{
			Actor: b.ref(),
		}, grpc.Trailer(tr))
		return err
	})
	return serverElapsed, err
}

func (b *substrateBackend) ensureAtespace(ctx context.Context) error {
	return b.tracedCall(ctx, "CreateAtespace", nil, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := b.cfg.APIStub.CreateAtespace(callCtx, &ateapipb.CreateAtespaceRequest{
			Atespace: &ateapipb.Atespace{
				Metadata: &ateapipb.ResourceMetadata{
					Name: b.cfg.Atespace,
				},
			},
		}, grpc.Trailer(tr))
		if err == nil {
			return nil
		}
		if s, ok := status.FromError(err); ok && s.Code() == codes.AlreadyExists {
			return nil
		}
		return err
	})
}

// create registers the actor. It runs before the cold window opens, so its
// latency is reported but not counted in the window: a harness creates the
// record once and may do it long before the first instruction.
func (b *substrateBackend) create(ctx context.Context) error {
	return b.tracedCall(ctx, "CreateActor", nil, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := b.cfg.APIStub.CreateActor(callCtx, &ateapipb.CreateActorRequest{
			Actor: &ateapipb.Actor{
				Metadata:      &ateapipb.ResourceMetadata{Atespace: b.cfg.Atespace, Name: b.actorName},
				ActorTemplate: &ateapipb.ObjectRef{Atespace: templateAtespace, Name: b.templateName},
			},
		}, grpc.Trailer(tr))
		return err
	})
}

func (b *substrateBackend) suspend(ctx context.Context) {
	_ = b.tracedCall(ctx, suspendMetric, nil, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := b.cfg.APIStub.SuspendActor(callCtx, &ateapipb.SuspendActorRequest{
			Actor: b.ref(),
		}, grpc.Trailer(tr))
		return err
	})
}

// teardown suspends the actor before deleting it. DeleteActor requires
// SUSPENDED or CRASHED; deleting a running actor leaks it. The suspend is
// unmetered (teardown precondition, not benchmark latency), while the delete
// is metered so true leaks still surface in failures.csv.
func (b *substrateBackend) teardown(ctx context.Context) {
	_, _ = b.cfg.APIStub.SuspendActor(ctx, &ateapipb.SuspendActorRequest{
		Actor: b.ref(),
	})
	b.delete(ctx)
}

func (b *substrateBackend) delete(ctx context.Context) {
	_ = b.tracedCall(ctx, "DeleteActor", nil, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := b.cfg.APIStub.DeleteActor(callCtx, &ateapipb.DeleteActorRequest{
			Actor: b.ref(),
		}, grpc.Trailer(tr))
		return err
	})
}

// tracedCall wraps a unary gRPC call with a span and Prometheus/locust
// reporting, as the other user classes do. It additionally hands the
// server-side elapsed time back through serverElapsed (when non-nil) so a
// caller inside a measurement window can report what share of the window
// ateapi's handler accounted for.
func (b *substrateBackend) tracedCall(ctx context.Context, name string, serverElapsed *time.Duration, do func(context.Context, *metadata.MD) error) error {
	ctx, span := b.cfg.Tracer.Start(ctx, name)
	defer span.End()

	start := time.Now()
	var tr metadata.MD
	err := do(ctx, &tr)
	clientLatency := time.Since(start)

	latency, source := boomerutil.ElapsedFromMD(tr, ateinterceptors.ServerElapsedTrailer, clientLatency)
	if source == boomerutil.SourceServer {
		span.SetAttributes(attribute.Float64("server.elapsed_ms", boomerutil.MsFloat(latency)))
	}
	if serverElapsed != nil {
		*serverElapsed = latency
	}
	boomerutil.LogSampledTrace(span, name, latency, source, err)
	if err != nil {
		bmetrics.RecordFailure("grpc", name, runWorkloadUserClass, latency, err.Error())
		return err
	}
	bmetrics.RecordSuccess("grpc", name, runWorkloadUserClass, latency, 0)
	return nil
}

// agentSandboxBackend drives sigs.k8s.io/agent-sandbox: no actor persists
// across iterations the way substrate's does — each warm window deletes
// whatever this VU currently holds and claims a fresh one from the pool.
type agentSandboxBackend struct {
	cfg              *userclass.Config
	asDeps           AgentSandboxDeps
	templateName     string
	warmPoolName     string
	provisionTimeout time.Duration

	// name and usingPool describe whatever object is currently live for this
	// backend: a bare Sandbox (usingPool false) or a SandboxClaim (usingPool
	// true). Empty name means nothing is live.
	liveName  string
	usingPool bool
}

func (b *agentSandboxBackend) name() string            { return string(BackendAgentSandbox) }
func (b *agentSandboxBackend) template() string        { return b.templateName }
func (b *agentSandboxBackend) firstMetricName() string { return coldCreateMetric }
func (b *agentSandboxBackend) warmMetricName() string  { return poolClaimMetric }

// prepare is a no-op: unlike substrate's actor registration, there is
// nothing to set up before the first window — creating the Sandbox is part
// of what ColdCreate measures.
func (b *agentSandboxBackend) prepare(ctx context.Context) error { return nil }

func (b *agentSandboxBackend) first(ctx context.Context, _ dynconfig.Config) (actorRef, time.Duration, error) {
	return b.timedProvision(ctx, b.doCreateCold)
}

func (b *agentSandboxBackend) prepareWarm(ctx context.Context) {
	b.teardown(ctx)
}

func (b *agentSandboxBackend) warm(ctx context.Context, _ dynconfig.Config) (actorRef, time.Duration, error) {
	return b.timedProvision(ctx, b.doClaimFromPool)
}

func (b *agentSandboxBackend) timedProvision(ctx context.Context, do func(context.Context) (actorRef, error)) (actorRef, time.Duration, error) {
	ctx, span := b.cfg.Tracer.Start(ctx, provisionMetric)
	defer span.End()
	start := time.Now()

	actor, err := do(ctx)
	elapsed := time.Since(start)
	boomerutil.LogSampledTrace(span, provisionMetric, elapsed, boomerutil.SourceClient, err)
	if err != nil {
		bmetrics.RecordFailure("k8s", provisionMetric, runWorkloadUserClass, elapsed, err.Error())
		return actorRef{}, elapsed, err
	}
	bmetrics.RecordSuccess("k8s", provisionMetric, runWorkloadUserClass, elapsed, 0)
	return actor, elapsed, nil
}

// doCreateCold creates a Sandbox directly from the template's blueprint,
// bypassing any warm pool, through to Ready. This is agent-sandbox's true
// cold-boot path: image pull, scheduling, and application start all happen
// inside the window.
func (b *agentSandboxBackend) doCreateCold(ctx context.Context) (actorRef, error) {
	tmpl, err := b.asDeps.Extensions.ExtensionsV1beta1().SandboxTemplates(b.asDeps.Namespace).Get(ctx, b.templateName, metav1.GetOptions{})
	if err != nil {
		return actorRef{}, fmt.Errorf("get SandboxTemplate %s: %w", b.templateName, err)
	}

	name := "sb-cold-" + uuid.NewString()
	sb := &sandboxv1beta1.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: b.asDeps.Namespace},
		Spec:       sandboxv1beta1.SandboxSpec{SandboxBlueprint: tmpl.Spec.SandboxBlueprint},
	}
	if _, err := b.asDeps.Sandboxes.AgentsV1beta1().Sandboxes(b.asDeps.Namespace).Create(ctx, sb, metav1.CreateOptions{}); err != nil {
		return actorRef{}, fmt.Errorf("create Sandbox %s: %w", name, err)
	}
	// Recorded before the wait so a failed or interrupted wait still leaves
	// teardown something to clean up.
	b.liveName, b.usingPool = name, false

	fqdn, err := b.waitReady(ctx, "serviceFQDN", func(ctx context.Context) (string, bool, error) {
		got, err := b.asDeps.Sandboxes.AgentsV1beta1().Sandboxes(b.asDeps.Namespace).Get(ctx, name, metav1.GetOptions{})
		if err != nil {
			return "", false, err
		}
		if apimeta.IsStatusConditionTrue(got.Status.Conditions, string(sandboxv1beta1.SandboxConditionReady)) {
			return got.Status.ServiceFQDN, true, nil
		}
		return "", false, nil
	})
	if err != nil {
		return actorRef{}, err
	}
	return actorRef{HTTPClient: b.cfg.HTTPClient, ServiceFQDN: fqdn, Port: probePort}, nil
}

// doClaimFromPool claims a pre-warmed sandbox from the SandboxWarmPool
// through to Ready. Adoption is a label flip onto an already-running pod, so
// this is the "warm" number ColdCreate is compared against.
func (b *agentSandboxBackend) doClaimFromPool(ctx context.Context) (actorRef, error) {
	name := "sb-pool-" + uuid.NewString()
	claim := &extensionsv1beta1.SandboxClaim{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: b.asDeps.Namespace},
		Spec: extensionsv1beta1.SandboxClaimSpec{
			WarmPoolRef: extensionsv1beta1.SandboxWarmPoolRef{Name: b.warmPoolName},
		},
	}
	if _, err := b.asDeps.Extensions.ExtensionsV1beta1().SandboxClaims(b.asDeps.Namespace).Create(ctx, claim, metav1.CreateOptions{}); err != nil {
		return actorRef{}, fmt.Errorf("create SandboxClaim %s: %w", name, err)
	}
	b.liveName, b.usingPool = name, true

	// The claim mirrors the adopted Sandbox's name onto its own status as
	// soon as it's Ready, but not every controller version also mirrors
	// serviceFQDN there (that field is newer than status.sandbox.name/
	// podIPs, which have been there since the claim API's first release).
	// Resolving the FQDN from the Sandbox object itself, once we know its
	// name, works regardless of which the running controller mirrors.
	sandboxName, err := b.waitReady(ctx, "adopted sandbox name", func(ctx context.Context) (string, bool, error) {
		got, err := b.asDeps.Extensions.ExtensionsV1beta1().SandboxClaims(b.asDeps.Namespace).Get(ctx, name, metav1.GetOptions{})
		if err != nil {
			return "", false, err
		}
		if apimeta.IsStatusConditionTrue(got.Status.Conditions, string(sandboxv1beta1.SandboxConditionReady)) {
			return got.Status.SandboxStatus.Name, true, nil
		}
		return "", false, nil
	})
	if err != nil {
		return actorRef{}, err
	}
	if sandboxName == "" {
		return actorRef{}, fmt.Errorf("SandboxClaim %s reported Ready with no adopted sandbox name", name)
	}

	sb, err := b.asDeps.Sandboxes.AgentsV1beta1().Sandboxes(b.asDeps.Namespace).Get(ctx, sandboxName, metav1.GetOptions{})
	if err != nil {
		return actorRef{}, fmt.Errorf("get adopted Sandbox %s: %w", sandboxName, err)
	}
	if sb.Status.ServiceFQDN == "" {
		return actorRef{}, fmt.Errorf("adopted Sandbox %s has no serviceFQDN; its SandboxTemplate must set spec.service: true", sandboxName)
	}
	return actorRef{HTTPClient: b.cfg.HTTPClient, ServiceFQDN: sb.Status.ServiceFQDN, Port: probePort}, nil
}

// waitReady polls poll until it reports ready, timeout elapses, or ctx ends.
// poll returns some non-empty result string once ready (a serviceFQDN or a
// sandbox name, depending on the caller); want names that result for the
// error message. A ready report with an empty result is a configuration
// problem (e.g. the SandboxTemplate not setting spec.service: true) rather
// than something worth retrying past.
func (b *agentSandboxBackend) waitReady(ctx context.Context, want string, poll func(context.Context) (result string, ready bool, err error)) (string, error) {
	deadline := time.Now().Add(b.provisionTimeout)
	for {
		result, ready, err := poll(ctx)
		if err != nil {
			return "", err
		}
		if ready {
			if result == "" {
				return "", fmt.Errorf("ready with no %s; the SandboxTemplate must set spec.service: true", want)
			}
			return result, nil
		}
		if time.Now().After(deadline) {
			return "", fmt.Errorf("not ready within %s", b.provisionTimeout)
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(provisionPollInterval):
		}
	}
}

// teardown deletes whatever object is currently live for this backend, if
// any. Best-effort: the delete is reported to metrics/tracing, but a failure
// here does not stop the run — the next doCreateCold/doClaimFromPool call
// starts a fresh object under a fresh name regardless.
func (b *agentSandboxBackend) teardown(ctx context.Context) {
	if b.liveName == "" {
		return
	}
	name, usingPool := b.liveName, b.usingPool
	b.liveName = ""

	spanName, deleteFn := "DeleteSandbox", func(ctx context.Context) error {
		return b.asDeps.Sandboxes.AgentsV1beta1().Sandboxes(b.asDeps.Namespace).Delete(ctx, name, metav1.DeleteOptions{})
	}
	if usingPool {
		spanName, deleteFn = "DeleteSandboxClaim", func(ctx context.Context) error {
			return b.asDeps.Extensions.ExtensionsV1beta1().SandboxClaims(b.asDeps.Namespace).Delete(ctx, name, metav1.DeleteOptions{})
		}
	}

	ctx, span := b.cfg.Tracer.Start(ctx, spanName)
	defer span.End()
	start := time.Now()
	err := deleteFn(ctx)
	latency := time.Since(start)
	if err != nil && !apierrors.IsNotFound(err) {
		boomerutil.LogSampledTrace(span, spanName, latency, boomerutil.SourceClient, err)
		bmetrics.RecordFailure("k8s", spanName, runWorkloadUserClass, latency, err.Error())
		return
	}
	boomerutil.LogSampledTrace(span, spanName, latency, boomerutil.SourceClient, nil)
	bmetrics.RecordSuccess("k8s", spanName, runWorkloadUserClass, latency, 0)
}
