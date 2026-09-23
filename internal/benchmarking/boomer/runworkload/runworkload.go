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

// Package runworkload drives a workload actor through substrate's activation
// path and measures the wall clock a client waits before the sandbox accepts
// work.
//
// It is deliberately not the sum of the other benchmarks' rows: ResumeActor
// returning is not the same as the actor being reachable, and under implicit
// resume no ResumeActor RPC is issued at all. So the measurement brackets
// everything between "client wants the sandbox" and "an instruction came back
// correct", including the routing hop and any retry a client would really
// have made.
//
// Two windows are measured, because they answer different questions:
//
//   - FirstResume: the actor has just been created and has no snapshot of
//     its own, so substrate activates it from whatever source its template
//     resolves to — the template's golden snapshot when it has one, the OCI
//     image when it does not. Point --workload-template at a template
//     without snapshotsConfig to measure a true cold boot.
//   - TTFI: the actor exists and was suspended to object storage. This is
//     resuming an existing session, and is the number substrate's 100ms
//     activation target is about. Both windows measure time to first
//     instruction; this one carries the name because it is the number the
//     benchmark reports.
//
// The timing comes off the span tree rather than off arithmetic on the stats
// rows: a window's row latency is its parent span's duration, and each phase
// is a child span, so a trace shows where the time went. The attributes on
// the parent carry the split (control-plane handler time vs. the instruction
// round trip) so one sampled trace explains its own total.
//
// What is under measurement is substrate's activation path, not any
// application, so the workload the actor runs is a parameter rather than a
// fixture: --workload-name selects an implementation of the workload
// interface (see workload.go) and --workload-template overrides the
// ActorTemplate it names. The flags themselves are declared by
// cmd/benchmarking/boomer-worker and arrive here through SetOptions, so the
// binary's --help lists every argument it takes in one place.
package runworkload

import (
	"context"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/agent-substrate/substrate/internal/ateinterceptors"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/boomerutil"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/dynconfig"
	bmetrics "github.com/agent-substrate/substrate/internal/benchmarking/boomer/metrics"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/userclass"
	"github.com/agent-substrate/substrate/pkg/proto/ateapipb"
	"github.com/google/uuid"
	"github.com/prometheus/client_golang/prometheus"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// The atespace workloads/deploy.sh creates its ActorTemplates in. Mirrored
// from that script rather than imported, and shared by every workload, so it
// stays here rather than in any one of them.
const templateAtespace = "benchmark-workloads"

const (
	// Locust class name from tests/runworkload.py; must match boomer.Task.Name.
	runWorkloadUserClass = "RunWorkloadUser"

	// DefaultWorkload is the workload a RunWorkloadUser drives unless Options
	// names another. The default template follows from the workload rather than
	// being fixed here, so the two settings cannot drift into naming a template
	// the workload has no way to address. Exported because the worker's
	// --workload-name usage string states it.
	DefaultWorkload = gluttonWorkloadName

	// DefaultTimeout bounds the instruction retry loop when Options leaves it
	// zero. An actor that has not taken an instruction by then is recorded as
	// a failure instead of being waited on for the rest of the run.
	DefaultTimeout = 60 * time.Second

	// Delay between instruction attempts, short relative to a resume so the
	// retry granularity does not show up in the measurement.
	retryDelay = 25 * time.Millisecond

	// drainTimeout bounds the wait for in-flight iterations at the start of
	// the teardown. Well under the budget the caller allows for the whole
	// teardown, so that the actors already created are still removed when an
	// iteration is wedged rather than merely slow.
	drainTimeout = 15 * time.Second

	// Caps the teardown's concurrent control-plane calls. Every VU's actor is
	// removed at once, so an unbounded run would open one suspend and one
	// delete per actor against an ateapi still draining the run's own traffic.
	teardownConcurrency = 16

	firstResumeMetric = "FirstResume"
	ttfiMetric        = "TTFI"
	resumeMetric      = "Resume"
	instructionMetric = "Instruction"
	suspendMetric     = "Suspend"
	crashCountMetric  = "CrashCount"

	// ateapi returns Aborted both for a crashed actor and for the transient
	// concurrent-update conflict, so the message is what tells them apart.
	crashedMsg = "crashed"

	// Transport label for the composite rows. A measurement window spans a gRPC
	// resume and an HTTP instruction, so neither transport label the other
	// rows use describes it.
	runWorkloadMethod = "runworkload"
)

// Options are the runworkload-only settings of a run. They are handed to
// this package rather than parsed by it: every flag the worker accepts is
// declared in cmd/benchmarking/boomer-worker, so one --help lists them all and
// a user class has no command-line surface of its own to keep in sync.
//
// The zero value asks for the defaults throughout.
type Options struct {
	// Workload names a registered workload (see workload.go). Empty selects
	// DefaultWorkload.
	Workload string

	// Template overrides the ActorTemplate the workload would measure. Empty
	// leaves that choice to the workload, which is the only place that knows
	// what it can address.
	Template string

	// Timeout is the ceiling on the instruction retry loop. Zero selects
	// DefaultTimeout. Negative is a caller error, rejected where the flag is
	// declared rather than silently treated as unset.
	Timeout time.Duration
}

// configured is what SetOptions recorded. It is read exactly once, by
// initRunWorkload, and passed by value from there on, so nothing on the
// measurement path reads mutable package state.
var configured Options

var (
	activeActors = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "boomer_active_actors",
			Help: "Number of currently active actors.",
		},
		[]string{"workload"},
	)
	suspendedActors = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "boomer_suspended_actors",
			Help: "Number of currently suspended actors.",
		},
		[]string{"workload"},
	)
)

// SetOptions records the settings initRunWorkload will use. It must run
// before the registered entry's Init, which main satisfies by calling it
// after flag.Parse and before the userclass lookup. A setter rather than an
// Init parameter because the registry hands every user class the same
// userclass.Config, which is upstream's type and carries no runworkload
// fields.
func SetOptions(o Options) { configured = o }

func init() {
	prometheus.MustRegister(activeActors, suspendedActors)
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
		// Scope names the benchmark, not the workload: the same spans are
		// emitted whichever workload --workload-name selects.
		cfg.Tracer = otel.Tracer("substrate-boomer/runworkload")
	}
	// Resolved once at startup rather than per iteration: an unselectable
	// workload is a configuration mistake, and a run that reports it after
	// producing rows has already wasted the run.
	wl, err := resolveWorkload(configured.Workload)
	if err != nil {
		slog.Error("fatal: cannot select the workload",
			slog.String("err", err.Error()))
		os.Exit(1)
	}
	rt := &runWorkloadRuntime{cfg: cfg, opts: configured, workload: wl}
	return rt.iterate, rt.shutdown
}

type runWorkloadRuntime struct {
	cfg      *userclass.Config
	opts     Options
	workload workload
	users    sync.Map // goroutineID -> *runWorkloadUser

	// Gates the task function so the teardown can wait for the iterations
	// boomer leaves running when it returns. See inflight.go.
	inflight inflight
}

func (r *runWorkloadRuntime) dynamicWait() time.Duration {
	cfg := r.cfg.Dyn.Load()
	if cfg.MaxWait <= cfg.MinWait {
		return cfg.MinWait
	}
	jitter := cfg.MaxWait - cfg.MinWait
	return cfg.MinWait + time.Duration(rand.Float64()*float64(jitter))
}

// iterate is the task function boomer calls in a loop on each VU goroutine.
// The first call on a goroutine creates the actor and measures its first
// resume; every later call suspends it and measures TTFI.
//
// The gate is taken for the whole iteration, so the teardown can tell an
// iteration that is still creating an actor from one that has finished. A
// refused entry means the run is being torn down, and the only correct thing
// left to do is return: an actor created past this point would not be in the
// map the teardown has already walked.
func (r *runWorkloadRuntime) iterate() {
	if !r.inflight.enter() {
		return
	}
	defer r.inflight.leave()

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
			// actor just created would never be reached again — and never
			// torn down. Drop it now instead of leaking it for the rest of
			// the run.
			u.suspendAndDelete(context.Background())
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
	if user.crashed {
		time.Sleep(r.dynamicWait())
		return
	}

	dynCfg := r.cfg.Dyn.Load()
	ctx := context.Background()
	// Suspend is the precondition for a warm measurement, not part of it:
	// it runs outside the measured span and reports its own row.
	user.suspend(ctx)
	user.measure(ctx, ttfiMetric, dynCfg, r.opts, false)

	time.Sleep(r.dynamicWait())
}

func (r *runWorkloadRuntime) startUser(ctx context.Context, dynCfg dynconfig.Config, opts Options) (*runWorkloadUser, error) {
	tmpl := opts.Template
	if tmpl == "" {
		tmpl = r.workload.DefaultTemplate()
	}

	u := &runWorkloadUser{
		cfg:          r.cfg,
		workload:     r.workload,
		actorName:    "sb-" + uuid.NewString(),
		templateName: tmpl,
		userClass:    runWorkloadUserClass,
	}
	bmetrics.UpdateUsers(runWorkloadUserClass, 1)
	if err := u.ensureAtespace(ctx); err != nil {
		bmetrics.UpdateUsers(runWorkloadUserClass, -1)
		return nil, err
	}
	if err := u.create(ctx); err != nil {
		bmetrics.UpdateUsers(runWorkloadUserClass, -1)
		return nil, err
	}

	// A failed first-resume window is a result, not a reason to drop the
	// actor: it is already recorded as a FirstResume failure, and the next
	// iteration's suspend normalizes whatever state the actor was left in.
	u.measure(ctx, firstResumeMetric, dynCfg, opts, true)
	return u, nil
}

// shutdown removes every actor the run created, and says per actor whether it
// went. An actor that outlives the run holds a worker until someone deletes it
// by hand, so the one place that knows it was left behind reports it by name
// rather than leaving the next run to find it.
//
// It begins by draining the task function, because boomer.Run returns while
// iterations are still in flight (see inflight.go) and the walk below has to
// happen after the last actor has been recorded, not during it.
func (r *runWorkloadRuntime) shutdown(ctx context.Context) {
	// The drain takes a slice of the caller's budget rather than all of it.
	// An iteration can sit in the instruction retry loop for opts.Timeout,
	// which is the same order as the whole teardown budget, so waiting on
	// ctx alone would let one stuck iteration spend every second that the
	// suspend and delete calls need — turning a drain that was meant to
	// prevent leaked actors into the cause of them.
	drainCtx, cancel := context.WithTimeout(ctx, drainTimeout)
	defer cancel()
	if !r.inflight.drain(drainCtx) {
		// Not fatal, and deliberately still followed by the walk: the actors
		// already recorded are worth removing even though the map may still
		// grow behind us. What it costs is the guarantee, so name it.
		slog.Warn("teardown drain timed out; tearing down with iterations still in flight",
			slog.Duration("drain_timeout", drainTimeout))
	}

	// Snapshotted before the teardown so nothing is walked while goroutines
	// are being launched against it.
	var users []*runWorkloadUser
	r.users.Range(func(_, val any) bool {
		users = append(users, val.(*runWorkloadUser))
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

			err := u.suspendAndDelete(ctx)
			bmetrics.UpdateUsers(runWorkloadUserClass, -1)
			if err != nil {
				left.Add(1)
				slog.Warn("teardown left an actor behind",
					slog.String("actor", u.actorName),
					slog.String("template", u.templateName),
					slog.String("err", err.Error()))
				return
			}
			removed.Add(1)
			slog.Info("teardown removed an actor",
				slog.String("actor", u.actorName))
		}()
	}
	wg.Wait()

	slog.Info("teardown finished",
		slog.Int64("removed", removed.Load()),
		slog.Int64("left", left.Load()))
}

type runWorkloadUser struct {
	cfg          *userclass.Config
	workload     workload
	actorName    string
	templateName string
	userClass    string

	// Track the state of the actor so we don't double-count metrics if
	// the benchmark calls Resume/Suspend idempotently or repeatedly.
	isActive  bool
	isCreated bool

	// Set once ResumeActor reports ACTOR_STATE_CRASHED. ateapi never
	// rehabilitates a crashed actor, so iterate stops measuring this VU.
	crashed bool
}

func (u *runWorkloadUser) ref() *ateapipb.ObjectRef {
	return &ateapipb.ObjectRef{Atespace: u.cfg.Atespace, Name: u.actorName}
}

// actor is the handle the workload addresses. Built per call rather than
// stored so the workload can hold no benchmark state of its own.
func (u *runWorkloadUser) actor() actorRef {
	return actorRef{
		HTTPClient: u.cfg.HTTPClient,
		RouterURL:  u.cfg.RouterURL,
		Atespace:   u.cfg.Atespace,
		Name:       u.actorName,
	}
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
// wait through runs inside it and nothing else does.
func (u *runWorkloadUser) measure(ctx context.Context, metricName string, dynCfg dynconfig.Config, opts Options, firstResume bool) {
	mode := dynCfg.ResumeMode
	if mode == "" {
		// Matches common/resume_mode.py's default, so the mode is the same
		// whether the value arrives from locust or from nothing at all.
		// Implicit is the more realistic client path — a harness sends
		// traffic rather than calling ResumeActor — so the tests.yaml
		// matrix measures both rather than assuming one.
		mode = dynconfig.ResumeModeExplicit
	}
	ctx, span := u.cfg.Tracer.Start(ctx, metricName)
	defer span.End()
	span.SetAttributes(
		attribute.String("workload", u.workload.Name()),
		attribute.String("template", u.templateName),
		attribute.String("resume_mode", mode),
		attribute.Bool("first_resume", firstResume),
	)

	start := time.Now()

	var controlPlane time.Duration
	if mode == dynconfig.ResumeModeExplicit {
		elapsed, err := u.resume(ctx)
		controlPlane = elapsed
		if err != nil {
			u.recordWindow(span, metricName, time.Since(start), 0, controlPlane, 0, err)
			return
		}
	}

	attempts, instruction, err := u.firstInstruction(ctx, u.budget(opts))
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
		bmetrics.RecordFailure(runWorkloadMethod, metricName, u.userClass, total, err.Error())
		return
	}
	bmetrics.RecordSuccess(runWorkloadMethod, metricName, u.userClass, total, 0)
}

// firstInstruction drives the workload until one instruction comes back
// correct or the budget elapses, and reports the whole loop as one row. The
// retries are inside the measurement window on purpose: until an instruction
// lands, the client cannot use the sandbox, so a request the router rejected
// while the actor was still activating is part of the wait. Whether a given
// failure is worth retrying is the workload's call, since only it knows what
// its own answers mean. Per-attempt visibility comes from the router's own
// server-side spans, which the injected trace context parents to this one.
func (u *runWorkloadUser) firstInstruction(ctx context.Context, budget time.Duration) (int, time.Duration, error) {
	ctx, span := u.cfg.Tracer.Start(ctx, instructionMetric)
	defer span.End()

	start := time.Now()
	deadline := start.Add(budget)

	var attempts int
	var lastErr error
	for {
		attempts++
		retryable, err := u.workload.SendInstruction(ctx, u.actor())
		if err == nil {
			latency := time.Since(start)
			span.SetAttributes(attribute.Int("attempts", attempts))
			boomerutil.LogSampledTrace(span, instructionMetric, latency, boomerutil.SourceClient, nil)
			bmetrics.RecordSuccess("http", instructionMetric, u.userClass, latency, 0)
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
	bmetrics.RecordFailure("http", instructionMetric, u.userClass, latency, lastErr.Error())
	return attempts, latency, lastErr
}

// resume issues the explicit ResumeActor that opens an explicit-mode
// window, and returns the server-side handler duration so the parent can
// attribute how much of the window ateapi owns.
func (u *runWorkloadUser) resume(ctx context.Context) (time.Duration, error) {
	var serverElapsed time.Duration
	err := u.tracedCall(ctx, resumeMetric, &serverElapsed, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.ResumeActor(callCtx, &ateapipb.ResumeActorRequest{
			Actor: u.ref(),
		}, grpc.Trailer(tr))
		return err
	})
	if err != nil {
		u.markIfCrashed(err)
		return serverElapsed, err
	}
	if !u.isActive {
		suspendedActors.WithLabelValues(u.workload.Name()).Dec()
		activeActors.WithLabelValues(u.workload.Name()).Inc()
		u.isActive = true
	}
	return serverElapsed, nil
}

// markIfCrashed records the actor as crashed when err is ateapi's report of
// one, and ignores every other error. Only the explicit path reaches it:
// under implicit resume the benchmark issues no ResumeActor.
func (u *runWorkloadUser) markIfCrashed(err error) {
	if u.crashed {
		return
	}
	s, ok := status.FromError(err)
	if !ok || s.Code() != codes.Aborted || !strings.Contains(s.Message(), crashedMsg) {
		return
	}
	u.crashed = true
	bmetrics.RecordFailure("actor", crashCountMetric, u.userClass, 0, "actor entered ACTOR_STATE_CRASHED")
	slog.Warn("runworkload actor crashed; will stop measuring it",
		slog.String("actor", u.actorName),
		slog.String("template", u.templateName),
		slog.String("err", err.Error()))
}

func (u *runWorkloadUser) ensureAtespace(ctx context.Context) error {
	return u.tracedCall(ctx, "CreateAtespace", nil, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.CreateAtespace(callCtx, &ateapipb.CreateAtespaceRequest{
			Atespace: &ateapipb.Atespace{
				Metadata: &ateapipb.ResourceMetadata{
					Name: u.cfg.Atespace,
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
func (u *runWorkloadUser) create(ctx context.Context) error {
	err := u.tracedCall(ctx, "CreateActor", nil, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.CreateActor(callCtx, &ateapipb.CreateActorRequest{
			Actor: &ateapipb.Actor{
				Metadata:      &ateapipb.ResourceMetadata{Atespace: u.cfg.Atespace, Name: u.actorName},
				ActorTemplate: &ateapipb.ObjectRef{Atespace: templateAtespace, Name: u.templateName},
			},
		}, grpc.Trailer(tr))
		return err
	})
	if err == nil && !u.isActive {
		activeActors.WithLabelValues(u.workload.Name()).Inc()
		u.isActive = true
		u.isCreated = true
	}
	return err
}

func (u *runWorkloadUser) suspend(ctx context.Context) {
	err := u.tracedCall(ctx, suspendMetric, nil, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.SuspendActor(callCtx, &ateapipb.SuspendActorRequest{
			Actor: u.ref(),
		}, grpc.Trailer(tr))
		return err
	})
	if err == nil && u.isActive {
		activeActors.WithLabelValues(u.workload.Name()).Dec()
		suspendedActors.WithLabelValues(u.workload.Name()).Inc()
		u.isActive = false
	}
}

// suspendAndDelete suspends the actor before deleting it. DeleteActor
// requires SUSPENDED or CRASHED; deleting a running actor leaks it. The
// suspend is unmetered (teardown precondition, not benchmark latency), while
// the delete is metered so true leaks still surface in failures.csv.
//
// A nil return means the actor is gone, so the delete decides it and a failed
// suspend rides along as context: the two fail together when the actor is
// still in a transitional state, and the pair is what names that cause.
func (u *runWorkloadUser) suspendAndDelete(ctx context.Context) error {
	_, suspendErr := u.cfg.APIStub.SuspendActor(ctx, &ateapipb.SuspendActorRequest{
		Actor: u.ref(),
	})
	if suspendErr == nil && u.isActive {
		activeActors.WithLabelValues(u.workload.Name()).Dec()
		suspendedActors.WithLabelValues(u.workload.Name()).Inc()
		u.isActive = false
	}

	deleteErr := u.delete(ctx)
	if deleteErr == nil {
		return nil
	}
	if suspendErr != nil {
		return fmt.Errorf("delete: %w (after suspend: %v)", deleteErr, suspendErr)
	}
	return fmt.Errorf("delete: %w", deleteErr)
}

func (u *runWorkloadUser) delete(ctx context.Context) error {
	err := u.tracedCall(ctx, "DeleteActor", nil, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.DeleteActor(callCtx, &ateapipb.DeleteActorRequest{
			Actor: u.ref(),
		}, grpc.Trailer(tr))
		return err
	})

	// Regardless of whether DeleteActor succeeds (it might fail if the actor is
	// stuck suspending, but we're throwing away the benchmark tracking struct anyway),
	// we must decrement our local gauge so it doesn't leak in Prometheus.
	if u.isCreated {
		if u.isActive {
			activeActors.WithLabelValues(u.workload.Name()).Dec()
		} else {
			suspendedActors.WithLabelValues(u.workload.Name()).Dec()
		}
		u.isCreated = false
		u.isActive = false
	}
	return err
}

// tracedCall wraps a unary gRPC call with a span and Prometheus/locust
// reporting, as the other user classes do. It additionally hands the
// server-side elapsed time back through serverElapsed (when non-nil) so a
// caller inside a measurement window can report what share of the window
// ateapi's handler accounted for.
func (u *runWorkloadUser) tracedCall(ctx context.Context, name string, serverElapsed *time.Duration, do func(context.Context, *metadata.MD) error) error {
	ctx, span := u.cfg.Tracer.Start(ctx, name)
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
		bmetrics.RecordFailure("grpc", name, u.userClass, latency, err.Error())
		return err
	}
	bmetrics.RecordSuccess("grpc", name, u.userClass, latency, 0)
	return nil
}
