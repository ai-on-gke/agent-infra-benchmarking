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

// Package runworkload drives a workload actor through substrate's actor
// lifecycle: each VU creates one actor and then cycles it resume/suspend for
// the rest of the run, removing it at teardown.
//
// Every control-plane call reports its own row and span through tracedCall.
// On top of them each cycle reports TTFI: the wait from having a suspended
// actor to that actor serving an instruction. See measureTTFI for what the
// window includes. It also reports CycleTime, the whole cycle from the resume
// to the suspend returning; see cycle.
//
// The workload the actor runs is a parameter: --workload-name selects an
// implementation of the workload interface (see workload.go) and
// --workload-template overrides the ActorTemplate it names. Both flags are
// declared by benchmarking/locust/go/cmd/boomer-worker and arrive through SetOptions.
package runworkload

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"os"
	"sync"
	"sync/atomic"
	"time"

	"github.com/agent-substrate/substrate/internal/ateinterceptors"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/boomerutil"
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

// The atespace scenario_runner/src/scenario_runner/workloads/ creates its
// ActorTemplates in. Mirrored from it rather than imported.
const templateAtespace = "benchmark-workloads"

const (
	// Locust class name from master-image/tests/runworkload.py; must match boomer.Task.Name.
	runWorkloadUserClass = "RunWorkloadUser"

	// DefaultWorkload is the workload a RunWorkloadUser drives unless Options
	// names another. Exported because the worker's --workload-name usage
	// string states it.
	DefaultWorkload = gluttonWorkloadName

	// DefaultTimeout is the per-call deadline when Options sets none. Exported
	// for the worker's --workload-timeout usage string.
	DefaultTimeout = 30 * time.Second

	ttfiMetric          = "TTFI"
	cycleTimeMetric     = "CycleTime"
	resumeMetric        = "Resume"
	suspendMetric       = "Suspend"
	executionTimeMetric = "ExecutionTime"

	// Transport label for TTFI and CycleTime. Their windows span gRPC calls and
	// an HTTP instruction, so neither transport label describes them.
	runWorkloadMethod = "runworkload"

	// Caps the teardown's concurrent control-plane calls. Every VU's actor is
	// removed at once, so an unbounded run would open one suspend and one
	// delete per actor against an ateapi still draining the run's own traffic.
	teardownConcurrency = 16

	// drainTimeout bounds the wait for in-flight iterations at the start of
	// the teardown. Well under the budget the caller allows for the whole
	// teardown, so that the actors already created are still removed when an
	// iteration is wedged rather than merely slow.
	drainTimeout = 35 * time.Second

	// How long a VU refused by the teardown waits before boomer calls it again.
	// Boomer keeps the VUs looping until the teardown returns, and without a
	// wait they spin against the teardown's own calls.
	refusedWait = 100 * time.Millisecond
)

// Two refusals clear on their own once another operation on the same actor
// lands, which can take seconds when that operation takes a snapshot: a
// control-plane call refused with Aborted because the actor is busy, and an
// instruction answered 421 because the router sent it to a worker the actor
// was not active on yet. Both are retried, waiting retryInitialDelay and
// doubling up to retryMaxDelay, until retryBudget is spent; upstream's router
// retries Aborted the same way. Variables so that tests can shorten them.
var (
	retryInitialDelay = 100 * time.Millisecond
	retryMaxDelay     = time.Second
	retryBudget       = 5 * time.Second
)

// retry calls do until it returns an error retryable does not accept, nil,
// or retryBudget runs out, and returns how many calls it made and the last
// error. A cancelled ctx ends the wait between calls.
func retry(ctx context.Context, retryable func(error) bool, do func() error) (int, error) {
	start := time.Now()
	delay := retryInitialDelay
	for attempt := 1; ; attempt++ {
		err := do()
		if err == nil || !retryable(err) || time.Since(start)+delay > retryBudget {
			return attempt, err
		}
		select {
		case <-ctx.Done():
			return attempt, err
		case <-time.After(delay):
		}
		delay = min(2*delay, retryMaxDelay)
	}
}

// Resume modes. Explicit issues a ResumeActor before the instruction;
// implicit leaves the wake to the router.
const (
	ResumeModeExplicit = "explicit"
	ResumeModeImplicit = "implicit"
)

// Options are the runworkload-only settings of a run, handed to this package
// rather than parsed by it: every flag the worker accepts is declared in
// benchmarking/locust/go/cmd/boomer-worker. The zero value asks for the defaults.
type Options struct {
	// Workload names a registered workload (see workload.go). Empty selects
	// DefaultWorkload.
	Workload string

	// Template overrides the ActorTemplate the workload would run on. Empty
	// leaves that choice to the workload.
	Template string

	// Timeout is the deadline on each control-plane call and each instruction;
	// one that runs past it fails. Zero selects DefaultTimeout.
	Timeout time.Duration

	// MinWait and MaxWait bound the think time between iterations. An upper
	// bound at or below the lower one waits MinWait exactly.
	MinWait time.Duration
	MaxWait time.Duration

	// ResumeMode is ResumeModeExplicit or ResumeModeImplicit; empty selects
	// explicit.
	ResumeMode string

	// EgressAllow names the hosts every actor may connect to. Empty gives the
	// actors no egress policy, which substrate's egress gateway treats as
	// deny-all.
	EgressAllow []string

	// SuspendDisabled controls whether the suspend phase is skipped during the benchmark.
	SuspendDisabled bool
}

// configured is what SetOptions recorded, read once by initRunWorkload and
// passed by value from there on.
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
// after flag.Parse and before the userclass lookup.
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
// boomer-compatible task function plus a shutdown hook to run before exit.
func initRunWorkload(cfg *userclass.Config) (taskFn func(), shutdown func(context.Context)) {
	if cfg.Tracer == nil {
		cfg.Tracer = otel.Tracer("substrate-boomer/runworkload")
	}
	// Resolved once at startup: an unselectable workload is a configuration
	// mistake, and reporting it after producing rows has already wasted the
	// run.
	wl, err := resolveWorkload(configured.Workload)
	if err != nil {
		slog.Error("fatal: cannot select the workload", slog.String("err", err.Error()))
		os.Exit(1)
	}
	hosts, err := egressHosts(configured.EgressAllow)
	if err != nil {
		slog.Error("fatal: invalid --actor-egress-allow", slog.String("err", err.Error()))
		os.Exit(1)
	}
	if len(hosts) > 0 {
		slog.Info("actors may connect to", slog.Any("hosts", hosts))
	}
	opts := configured
	if opts.Timeout == 0 {
		opts.Timeout = DefaultTimeout
	}
	rt := &runWorkloadRuntime{cfg: cfg, opts: opts, workload: wl, egressHosts: hosts}
	rt.run, rt.endRun = context.WithCancelCause(context.Background())
	return rt.iterate, rt.shutdown
}

type runWorkloadRuntime struct {
	cfg      *userclass.Config
	opts     Options
	workload workload
	users    sync.Map // goroutineID -> *runWorkloadUser

	// Options.EgressAllow, checked; empty creates no egress policy.
	egressHosts []string

	// Gates the task function so the teardown can wait for the iterations
	// boomer leaves running when it returns. See inflight.go.
	inflight inflight

	// The run's atespace is created by the first user to start, and by any
	// later one only while that has not yet worked.
	atespaceMu    sync.Mutex
	atespaceReady bool

	// The context every iteration's calls run under, cancelled with
	// errRunEnded when the teardown starts. Nil (as in tests that build the
	// runtime by hand) runs them under context.Background().
	run    context.Context
	endRun context.CancelCauseFunc
}

// errRunEnded is the cause the run's context is cancelled with when the
// teardown starts.
var errRunEnded = errors.New("the run ended")

// cutOff reports whether err is a call the end of the run cut off rather than
// a result. locust records nothing for a user it stops mid-request either; an
// instruction the run's last seconds interrupt says nothing about the system.
func cutOff(ctx context.Context, err error) bool {
	return err != nil && errors.Is(context.Cause(ctx), errRunEnded)
}

func (r *runWorkloadRuntime) dynamicWait() time.Duration {
	if r.opts.MaxWait <= r.opts.MinWait {
		return r.opts.MinWait
	}
	jitter := r.opts.MaxWait - r.opts.MinWait
	return r.opts.MinWait + time.Duration(rand.Float64()*float64(jitter))
}

// iterate is the task function boomer calls in a loop on each VU goroutine.
// The first call on a goroutine creates the actor, which CreateActor leaves
// SUSPENDED; every later one resumes it, sends it one instruction and suspends
// it again. The actor therefore sits suspended through the think time between
// iterations, and each iteration finds it that way.
//
// The gate is taken for the whole iteration, so the teardown can tell an
// iteration that is still creating an actor from one that has finished. A
// refused entry means the run is being torn down, and the only correct thing
// left to do is return: an actor created past this point would not be in the
// map the teardown has already walked.
func (r *runWorkloadRuntime) iterate() {
	if !r.inflight.enter() {
		time.Sleep(refusedWait)
		return
	}
	defer r.inflight.leave()

	ctx := r.run
	if ctx == nil {
		ctx = context.Background()
	}

	gid := boomerutil.GoroutineID()
	val, loaded := r.users.Load(gid)
	if !loaded {
		u, err := r.startUser(ctx, r.opts)
		if err != nil {
			slog.Warn("runworkload on_start failed; goroutine will retry next iter",
				slog.String("err", err.Error()))
			time.Sleep(r.dynamicWait())
			return
		}
		if _, loaded := r.users.LoadOrStore(gid, u); loaded {
			// Another user already holds this goroutine id, so the actor just
			// created would never be reached again. Drop it now rather than
			// leak it for the rest of the run.
			_ = u.suspendAndDelete(ctx)
			bmetrics.UpdateUsers(runWorkloadUserClass, -1)
		}
		time.Sleep(r.dynamicWait())
		return
	}
	user := val.(*runWorkloadUser)

	if call, err := user.cycle(ctx, r.opts.ResumeMode); err != nil {
		// The teardown, which cut it off, removes the actor itself.
		if cutOff(ctx, err) {
			return
		}
		r.discard(ctx, gid, user, call, err)
		time.Sleep(r.dynamicWait())
		return
	}

	time.Sleep(r.dynamicWait())
}

// discard throws away an actor whose state is no longer known. Any failed
// call can leave it mid transition and nothing reconciles it, so the actor is
// deleted and the goroutine's slot freed; the next iteration starts a fresh
// one rather than working against a broken actor for the rest of the run.
//
// The slot is freed whether or not the delete worked, because a VU that
// cannot get rid of its actor still has to stop using it. A delete that
// failed is named, since that actor holds a worker until someone removes it.
func (r *runWorkloadRuntime) discard(ctx context.Context, gid int64, u *runWorkloadUser, call string, cause error) {
	slog.Warn("discarding the actor after a failed call",
		slog.String("actor", u.actorName),
		slog.String("call", call),
		slog.String("err", cause.Error()))

	if err := u.suspendAndDelete(ctx); err != nil {
		slog.Warn("discarded actor was left behind",
			slog.String("actor", u.actorName),
			slog.String("template", u.templateName),
			slog.String("err", err.Error()))
	}
	r.users.Delete(gid)
	bmetrics.UpdateUsers(runWorkloadUserClass, -1)
}

func (r *runWorkloadRuntime) startUser(ctx context.Context, opts Options) (*runWorkloadUser, error) {
	tmpl := opts.Template
	if tmpl == "" {
		tmpl = r.workload.DefaultTemplate()
	}

	u := &runWorkloadUser{
		cfg:             r.cfg,
		workload:        r.workload,
		actorName:       "sb-" + uuid.NewString(),
		templateName:    tmpl,
		userClass:       runWorkloadUserClass,
		timeout:         opts.Timeout,
		suspendDisabled: opts.SuspendDisabled,
	}
	bmetrics.UpdateUsers(runWorkloadUserClass, 1)
	if err := r.ensureAtespace(ctx, u); err != nil {
		bmetrics.UpdateUsers(runWorkloadUserClass, -1)
		return nil, err
	}
	if err := u.create(ctx); err != nil {
		bmetrics.UpdateUsers(runWorkloadUserClass, -1)
		return nil, err
	}
	if len(r.egressHosts) > 0 {
		if err := u.allowEgress(ctx, r.egressHosts); err != nil {
			// Not yet resumed, so still SUSPENDED and deletable as it is.
			if delErr := u.delete(ctx, false); delErr != nil {
				slog.Warn("actor without an egress policy was left behind",
					slog.String("actor", u.actorName),
					slog.String("err", delErr.Error()))
			}
			bmetrics.UpdateUsers(runWorkloadUserClass, -1)
			return nil, err
		}
	}
	return u, nil
}

// shutdown removes every actor the run created, and says per actor whether it
// went. An actor that outlives the run holds a worker until someone deletes
// it by hand, so the one place that knows it was left behind names it.
//
// The actors go in parallel, capped at teardownConcurrency: a run's worth of
// them removed one at a time would outlast the budget the caller allows.
//
// It begins by draining the task function, because boomer.Run returns while
// iterations are still in flight (see inflight.go) and the walk below has to
// happen after the last actor has been recorded, not during it.
func (r *runWorkloadRuntime) shutdown(ctx context.Context) {
	// In-flight calls are cut off first, so the drain waits seconds rather than
	// the rest of an instruction: an openclaw turn takes far longer than the
	// teardown has. What they were doing is not recorded; see cutOff.
	if r.endRun != nil {
		r.endRun(errRunEnded)
	}

	// The drain takes a slice of the caller's budget rather than all of it, so
	// that one wedged iteration cannot spend every second the suspend and
	// delete calls need — turning a drain meant to prevent leaked actors into
	// the cause of them.
	drainCtx, cancel := context.WithTimeout(ctx, drainTimeout)
	defer cancel()
	if !r.inflight.drain(drainCtx) {
		// Not fatal, and deliberately still followed by the walk: the actors
		// already recorded are worth removing even though the map may still
		// grow behind us. What it costs is the guarantee, so name it.
		slog.Warn("teardown drain timed out; tearing down with iterations still in flight",
			slog.Duration("drain_timeout", drainTimeout))
	}

	// Snapshotted before the teardown so nothing is walked while goroutines are
	// being launched against it.
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
			slog.Info("teardown removed an actor", slog.String("actor", u.actorName))
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

	// timeout bounds each call; zero leaves it unbounded.
	timeout time.Duration

	// suspendDisabled controls whether the suspend phase is skipped during the benchmark.
	suspendDisabled bool

	// Track the state of the actor so we don't double-count metrics if
	// the benchmark calls Resume/Suspend idempotently or repeatedly.
	isActive  bool
	isCreated bool
}

func (u *runWorkloadUser) ref() *ateapipb.ObjectRef {
	return &ateapipb.ObjectRef{Atespace: u.cfg.Atespace, Name: u.actorName}
}

// actor is the handle a workload addresses. Built per call rather than stored
// so the workload holds no benchmark state of its own.
func (u *runWorkloadUser) actor() actorRef {
	return actorRef{
		HTTPClient: u.cfg.HTTPClient,
		RouterURL:  u.cfg.RouterURL,
		Atespace:   u.cfg.Atespace,
		Name:       u.actorName,
	}
}

// ensureAtespace creates the run's atespace once, through the first user to
// get here. Every user used to do it, which put a CreateAtespace row and call
// in the run for each actor it created.
func (r *runWorkloadRuntime) ensureAtespace(ctx context.Context, u *runWorkloadUser) error {
	r.atespaceMu.Lock()
	defer r.atespaceMu.Unlock()
	if r.atespaceReady {
		return nil
	}
	if err := u.ensureAtespace(ctx); err != nil {
		return err
	}
	r.atespaceReady = true
	return nil
}

func (u *runWorkloadUser) ensureAtespace(ctx context.Context) error {
	return u.tracedCall(ctx, "CreateAtespace", func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.CreateAtespace(callCtx, &ateapipb.CreateAtespaceRequest{
			Atespace: &ateapipb.Atespace{
				Metadata: &ateapipb.ResourceMetadata{Name: u.cfg.Atespace},
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

func (u *runWorkloadUser) create(ctx context.Context) error {
	err := u.tracedCall(ctx, "CreateActor", func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.CreateActor(callCtx, &ateapipb.CreateActorRequest{
			Actor: &ateapipb.Actor{
				Metadata:      &ateapipb.ResourceMetadata{Atespace: u.cfg.Atespace, Name: u.actorName},
				ActorTemplate: &ateapipb.ObjectRef{Atespace: templateAtespace, Name: u.templateName},
			},
		}, grpc.Trailer(tr))
		return err
	})
	// CreateActor leaves the actor SUSPENDED; the first resume moves it over.
	if err == nil && !u.isCreated {
		suspendedActors.WithLabelValues(u.workload.Name()).Inc()
		u.isCreated = true
	}
	return err
}

// cycle runs one resume/instruction/suspend cycle and reports its whole
// duration as CycleTime: the window opens where measureTTFI's does and closes
// when the suspend returns, so it holds the resume, the instruction's service
// time and the suspend together. Under implicit resume there is no ResumeActor,
// so it opens on the instruction that has the router wake the actor.
//
// Its span parents the TTFI and Suspend spans, so a trace shows the cycle as
// one unit. A failed call reports the window as a failure over the time spent
// and names that call, for the actor to be discarded.
func (u *runWorkloadUser) cycle(ctx context.Context, mode string) (call string, err error) {
	ctx, span := u.cfg.Tracer.Start(ctx, cycleTimeMetric)
	defer span.End()

	start := time.Now()
	defer func() { u.recordWindow(ctx, span, cycleTimeMetric, time.Since(start), err) }()

	if err := u.measureTTFI(ctx, mode); err != nil {
		return ttfiMetric, err
	}
	if err := u.suspend(ctx); err != nil {
		return suspendMetric, err
	}
	return "", nil
}

// measureTTFI brings the actor back up, sends it one instruction, and reports
// the wait until that instruction is served as TTFI. It must be called with the
// actor suspended, which is how iterate leaves it between iterations.
//
// Only explicit mode issues ResumeActor; implicit leaves the wake to the
// router. Either way the window closes on the served instruction, so it
// includes the instruction's service time, which ExecutionTime reports alone.
//
// A failed call reports the window as a failure over the wait it spent and
// hands the error back for the actor to be discarded.
func (u *runWorkloadUser) measureTTFI(ctx context.Context, mode string) error {
	if mode == "" {
		mode = ResumeModeExplicit
	}

	ctx, span := u.cfg.Tracer.Start(ctx, ttfiMetric)
	defer span.End()
	span.SetAttributes(
		attribute.String("workload", u.workload.Name()),
		attribute.String("template", u.templateName),
		attribute.String("resume_mode", mode),
	)

	start := time.Now()
	if mode == ResumeModeExplicit {
		if err := u.resume(ctx); err != nil {
			u.recordWindow(ctx, span, ttfiMetric, time.Since(start), err)
			return err
		}
	}

	err := u.executeInstruction(ctx)
	u.recordWindow(ctx, span, ttfiMetric, time.Since(start), err)
	return err
}

// recordWindow closes out the window reported as name, as a success or as a
// failure over the wait it spent.
func (u *runWorkloadUser) recordWindow(ctx context.Context, span trace.Span, name string, total time.Duration, err error) {
	if cutOff(ctx, err) {
		return
	}
	span.SetAttributes(attribute.Float64("total_ms", boomerutil.MsFloat(total)))
	boomerutil.LogSampledTrace(span, name, total, boomerutil.SourceClient, err)
	if err != nil {
		bmetrics.RecordFailure(runWorkloadMethod, name, u.userClass, total, err.Error())
		return
	}
	bmetrics.RecordSuccess(runWorkloadMethod, name, u.userClass, total, 0)
}

// executeInstruction sends one instruction and reports it as the ExecutionTime
// row. There are no retries: the actor is resumed before this runs, so a
// command it will not serve is a fault to surface rather than a state to wait
// out. The workload's retryable verdict is therefore ignored, with one
// exception: a 421 says the router used a worker assignment that was stale
// or not live yet, and is retried as retry describes.
//
// A non-nil error leaves the actor in a state the run cannot reason about: it
// is the signal to discard the actor, not merely a failed sample.
func (u *runWorkloadUser) executeInstruction(ctx context.Context) error {
	ctx, span := u.cfg.Tracer.Start(ctx, executionTimeMetric)
	defer span.End()
	ctx, cancel := u.callContext(ctx)
	defer cancel()

	start := time.Now()
	_, err := retry(ctx, func(err error) bool { return errors.Is(err, errStaleAssignment) }, func() error {
		_, err := u.workload.SendInstruction(ctx, u.actor())
		return err
	})
	service := time.Since(start)

	if cutOff(ctx, err) {
		return err
	}
	boomerutil.LogSampledTrace(span, executionTimeMetric, service, boomerutil.SourceClient, err)
	if err != nil {
		bmetrics.RecordFailure("http", executionTimeMetric, u.userClass, service, err.Error())
		return err
	}
	bmetrics.RecordSuccess("http", executionTimeMetric, u.userClass, service, 0)
	return nil
}

func (u *runWorkloadUser) resume(ctx context.Context) error {
	if u.suspendDisabled && u.isActive {
		return nil
	}

	err := u.tracedCall(ctx, resumeMetric, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.ResumeActor(callCtx, &ateapipb.ResumeActorRequest{
			Actor: u.ref(),
		}, grpc.Trailer(tr))
		return err
	})
	if err != nil {
		return err
	}
	if !u.isActive {
		suspendedActors.WithLabelValues(u.workload.Name()).Dec()
		activeActors.WithLabelValues(u.workload.Name()).Inc()
		u.isActive = true
	}
	return nil
}

// suspend takes the actor down and reports whether it went. A failed suspend
// can leave the actor mid transition, and nothing reconciles it, so the
// caller decides what the run does next rather than assuming it is suspended.
func (u *runWorkloadUser) suspend(ctx context.Context) error {
	if u.suspendDisabled {
		return nil
	}

	err := u.tracedCall(ctx, suspendMetric, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.SuspendActor(callCtx, &ateapipb.SuspendActorRequest{
			Actor: u.ref(),
		}, grpc.Trailer(tr))
		return err
	})
	if err != nil {
		// tracedCall records the row but logs only a sampled trace, so this
		// is the one place the reason is always said.
		slog.Warn("suspend failed; the actor may be left mid transition",
			slog.String("actor", u.actorName),
			slog.String("err", err.Error()))
		return err
	}
	if u.isActive {
		activeActors.WithLabelValues(u.workload.Name()).Dec()
		suspendedActors.WithLabelValues(u.workload.Name()).Inc()
		u.isActive = false
	}
	return nil
}

// suspendAndDelete suspends the actor before deleting it. DeleteActor
// requires SUSPENDED or CRASHED; deleting a running actor leaks it. The
// suspend is unmetered (teardown precondition, not benchmark latency), while
// the delete is metered so true leaks still surface.
//
// A nil return means the actor is gone, so the delete decides it and a failed
// suspend rides along as context: the two fail together when the actor is
// still in a transitional state, and the pair is what names that cause.
func (u *runWorkloadUser) suspendAndDelete(ctx context.Context) error {
	suspendCtx, cancel := u.callContext(ctx)
	_, suspendErr := u.cfg.APIStub.SuspendActor(suspendCtx, &ateapipb.SuspendActorRequest{
		Actor: u.ref(),
	})
	cancel()
	if suspendErr == nil && u.isActive {
		activeActors.WithLabelValues(u.workload.Name()).Dec()
		suspendedActors.WithLabelValues(u.workload.Name()).Inc()
		u.isActive = false
	}

	// A failed suspend leaves the actor in a state the delete precondition
	// refuses, and it is the case where leaving it behind costs a worker for
	// the rest of the run, so that delete is the one allowed from any state.
	deleteErr := u.delete(ctx, suspendErr != nil)
	if deleteErr == nil {
		return nil
	}
	if suspendErr != nil {
		return fmt.Errorf("delete: %w (after suspend: %v)", deleteErr, suspendErr)
	}
	return fmt.Errorf("delete: %w", deleteErr)
}

// delete removes the actor. anyState relaxes the SUSPENDED-or-CRASHED
// precondition the control plane otherwise enforces; the rest of the teardown
// (atelet termination, volume detach, worker release) runs either way, so it
// frees the worker rather than only dropping the record.
func (u *runWorkloadUser) delete(ctx context.Context, anyState bool) error {
	err := u.tracedCall(ctx, "DeleteActor", func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.DeleteActor(callCtx, &ateapipb.DeleteActorRequest{
			Actor:    u.ref(),
			AnyState: anyState,
		}, grpc.Trailer(tr))
		// Already gone, which is what the call is for: the teardown and a VU
		// discarding the same actor can both get to it.
		if status.Code(err) == codes.NotFound {
			return nil
		}
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
// reporting, as the other user classes do. The latency reported is the
// server's own elapsed trailer when it sent one, and client wall clock
// otherwise.
func (u *runWorkloadUser) tracedCall(ctx context.Context, name string, do func(context.Context, *metadata.MD) error) error {
	ctx, span := u.cfg.Tracer.Start(ctx, name)
	defer span.End()
	ctx, cancel := u.callContext(ctx)
	defer cancel()

	start := time.Now()
	var tr metadata.MD
	attempts, err := retry(ctx, func(err error) bool { return status.Code(err) == codes.Aborted }, func() error {
		tr = nil
		return do(ctx, &tr)
	})
	clientLatency := time.Since(start)

	latency, source := boomerutil.ElapsedFromMD(tr, ateinterceptors.ServerElapsedTrailer, clientLatency)
	// The server's elapsed trailer is the last attempt's alone, and would hide
	// the conflicts the client waited through before it.
	if attempts > 1 {
		latency, source = clientLatency, boomerutil.SourceClient
	}
	if source == boomerutil.SourceServer {
		span.SetAttributes(attribute.Float64("server.elapsed_ms", boomerutil.MsFloat(latency)))
	}
	if cutOff(ctx, err) {
		return err
	}
	boomerutil.LogSampledTrace(span, name, latency, source, err)
	if err != nil {
		bmetrics.RecordFailure("grpc", name, u.userClass, latency, err.Error())
		return err
	}
	bmetrics.RecordSuccess("grpc", name, u.userClass, latency, 0)
	return nil
}

// callContext bounds one call, retries included, by the user's timeout.
func (u *runWorkloadUser) callContext(ctx context.Context) (context.Context, context.CancelFunc) {
	if u.timeout <= 0 {
		return ctx, func() {}
	}
	return context.WithTimeout(ctx, u.timeout)
}
