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

// Package ttfi measures time to first instruction — the wall clock a client
// waits before a sandbox accepts work.
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
//   - TTFIFirstResume: the actor has just been created and has no snapshot of
//     its own, so substrate activates it from whatever source its template
//     resolves to — the template's golden snapshot when it has one, the OCI
//     image when it does not. Point ttfi_template at a template without
//     snapshotsConfig to measure a true cold boot.
//   - TTFIWarmStart: the actor exists and was suspended to object storage.
//     This is resuming an existing session, and is the number substrate's
//     100ms activation target is about.
//
// The timing comes off the span tree rather than off arithmetic on the stats
// rows: the TTFI row's latency is its parent span's duration, and each phase
// is a child span, so a trace shows where the time went. ttfi.* attributes on
// the parent carry the split (control-plane handler time vs. the instruction
// round trip) so one sampled trace explains its own total.
//
// What is under measurement is substrate's activation path, not any
// application, so the workload the actor runs is a parameter rather than a
// fixture: ttfi_workload selects an implementation of the workload interface
// (see workload.go) and ttfi_template overrides the ActorTemplate it names.
// glutton is the implementation shipped today, because its /ping echo is a
// cheap proof that the sandbox ran an instruction rather than merely accepting
// a connection.
package ttfi

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
)

// The atespace workloads/deploy.sh creates its ActorTemplates in. Mirrored
// from that script rather than imported, and shared by every workload, so it
// stays here rather than in any one of them.
const templateAtespace = "benchmark-workloads"

const (
	// Locust class name from tests/ttfi.py; must match boomer.Task.Name.
	ttfiUserClass = "TTFIUser"

	// The workload a TTFIUser drives unless ttfi_workload names another. The
	// default template follows from the workload rather than being fixed
	// here, so the two keys cannot drift into naming a template the workload
	// has no way to address.
	defaultTTFIWorkload = gluttonWorkloadName

	// Bound on the instruction retry loop when ttfi_timeout is unset. An
	// actor that has not taken an instruction by then is recorded as a TTFI
	// failure instead of being waited on for the rest of the run.
	defaultTTFITimeout = 60 * time.Second

	// Delay between instruction attempts, short relative to a resume so the
	// retry granularity does not show up in the measurement.
	ttfiRetryDelay = 25 * time.Millisecond

	ttfiFirstResumeMetric = "TTFIFirstResume"
	ttfiWarmMetric        = "TTFIWarmStart"
	ttfiResumeMetric      = "TTFIResume"
	ttfiInstructionMetric = "TTFIInstruction"
	ttfiSuspendMetric     = "TTFISuspend"

	// Transport label for the composite rows. A TTFI window spans a gRPC
	// resume and an HTTP instruction, so neither transport label the other
	// rows use describes it.
	ttfiMethod = "ttfi"
)

func init() {
	userclass.Add(userclass.Entry{
		Name:       "ttfi",
		LocustFile: "ttfi.py",
		UserClass:  ttfiUserClass,
		Init:       initTTFI,
	})
}

// initTTFI creates a runtime tied to cfg and returns a boomer-compatible task
// function plus a shutdown hook the caller should run before exit.
func initTTFI(cfg *userclass.Config) (taskFn func(), shutdown func(context.Context)) {
	if cfg.Tracer == nil {
		// Scope names the benchmark, not the workload: the same spans are
		// emitted whichever workload ttfi_workload selects.
		cfg.Tracer = otel.Tracer("substrate-boomer/ttfi")
	}
	tcfg, err := loadTTFIConfig()
	if err != nil {
		slog.Error("fatal: invalid TTFI keys in --config-json",
			slog.String("err", err.Error()))
		os.Exit(1)
	}
	// Resolved once at startup rather than per iteration: an unselectable
	// workload is a configuration mistake, and a run that reports it after
	// producing rows has already wasted the run.
	wl, err := resolveWorkload(tcfg.Workload)
	if err != nil {
		slog.Error("fatal: cannot select the TTFI workload",
			slog.String("err", err.Error()))
		os.Exit(1)
	}
	rt := &ttfiRuntime{cfg: cfg, tcfg: tcfg, workload: wl}
	return rt.iterate, rt.shutdown
}

type ttfiRuntime struct {
	cfg      *userclass.Config
	tcfg     ttfiConfig
	workload workload
	users    sync.Map // goroutineID -> *ttfiUser
}

func (r *ttfiRuntime) dynamicWait() time.Duration {
	cfg := r.cfg.Dyn.Load()
	if cfg.MaxWait <= cfg.MinWait {
		return cfg.MinWait
	}
	jitter := cfg.MaxWait - cfg.MinWait
	return cfg.MinWait + time.Duration(rand.Float64()*float64(jitter))
}

// iterate is the task function boomer calls in a loop on each VU goroutine.
// The first call on a goroutine creates the actor and measures its first
// resume; every later call suspends it and measures the warm one.
func (r *ttfiRuntime) iterate() {
	gid := boomerutil.GoroutineID()
	val, loaded := r.users.Load(gid)
	if !loaded {
		u, err := r.startUser(context.Background(), r.cfg.Dyn.Load(), r.tcfg)
		if err != nil {
			slog.Warn("ttfi on_start failed; goroutine will retry next iter",
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
			bmetrics.UpdateUsers(ttfiUserClass, -1)
		}
		// The cold window was measured inside startUser. Returning here
		// keeps one iteration to one measurement, so the two rows stay
		// independent samples rather than a cold and a warm number
		// averaged into one iteration's think time.
		time.Sleep(r.dynamicWait())
		return
	}
	user := val.(*ttfiUser)

	dynCfg := r.cfg.Dyn.Load()
	ctx := context.Background()
	// Suspend is the precondition for a warm measurement, not part of it:
	// it runs outside the TTFI span and reports its own row.
	user.suspend(ctx)
	user.measure(ctx, ttfiWarmMetric, dynCfg, r.tcfg, false)

	time.Sleep(r.dynamicWait())
}

func (r *ttfiRuntime) startUser(ctx context.Context, dynCfg dynconfig.Config, tcfg ttfiConfig) (*ttfiUser, error) {
	tmpl := tcfg.Template
	if tmpl == "" {
		tmpl = r.workload.DefaultTemplate()
	}

	u := &ttfiUser{
		cfg:          r.cfg,
		workload:     r.workload,
		actorName:    "sb-" + uuid.NewString(),
		templateName: tmpl,
		userClass:    ttfiUserClass,
	}
	bmetrics.UpdateUsers(ttfiUserClass, 1)
	if err := u.ensureAtespace(ctx); err != nil {
		bmetrics.UpdateUsers(ttfiUserClass, -1)
		return nil, err
	}
	if err := u.create(ctx); err != nil {
		bmetrics.UpdateUsers(ttfiUserClass, -1)
		return nil, err
	}

	// A failed first-resume window is a result, not a reason to drop the
	// actor: it is already recorded as a TTFIFirstResume failure, and the next
	// iteration's suspend normalizes whatever state the actor was left in.
	u.measure(ctx, ttfiFirstResumeMetric, dynCfg, tcfg, true)
	return u, nil
}

func (r *ttfiRuntime) shutdown(ctx context.Context) {
	r.users.Range(func(_, val any) bool {
		u := val.(*ttfiUser)
		u.suspendAndDelete(ctx)
		bmetrics.UpdateUsers(ttfiUserClass, -1)
		return true
	})
}

type ttfiUser struct {
	cfg          *userclass.Config
	workload     workload
	actorName    string
	templateName string
	userClass    string
}

func (u *ttfiUser) ref() *ateapipb.ObjectRef {
	return &ateapipb.ObjectRef{Atespace: u.cfg.Atespace, Name: u.actorName}
}

// actor is the handle the workload addresses. Built per call rather than
// stored so the workload can hold no benchmark state of its own.
func (u *ttfiUser) actor() actorRef {
	return actorRef{
		HTTPClient: u.cfg.HTTPClient,
		RouterURL:  u.cfg.RouterURL,
		Atespace:   u.cfg.Atespace,
		Name:       u.actorName,
	}
}

// budget is the ceiling on the instruction retry loop.
func (u *ttfiUser) budget(tcfg ttfiConfig) time.Duration {
	if tcfg.Timeout > 0 {
		return tcfg.Timeout
	}
	return defaultTTFITimeout
}

// measure times one TTFI window and reports it as metricName. The parent
// span's duration is the measurement, so every phase that a client would
// wait through runs inside it and nothing else does.
func (u *ttfiUser) measure(ctx context.Context, metricName string, dynCfg dynconfig.Config, tcfg ttfiConfig, firstResume bool) {
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
		attribute.String("ttfi.workload", u.workload.Name()),
		attribute.String("ttfi.template", u.templateName),
		attribute.String("ttfi.resume_mode", mode),
		attribute.Bool("ttfi.first_resume", firstResume),
	)

	start := time.Now()

	var controlPlane time.Duration
	if mode == dynconfig.ResumeModeExplicit {
		elapsed, err := u.resume(ctx)
		controlPlane = elapsed
		if err != nil {
			u.recordTTFI(span, metricName, time.Since(start), 0, controlPlane, 0, err)
			return
		}
	}

	attempts, instruction, err := u.firstInstruction(ctx, u.budget(tcfg))
	u.recordTTFI(span, metricName, time.Since(start), attempts, controlPlane, instruction, err)
}

// recordTTFI closes out a TTFI window: the attributes make one sampled trace
// self-explaining, and the row carries client wall clock. That is a
// deliberate departure from the other benchmarks, which prefer the server's
// elapsed trailer — the gaps between the phases are exactly what TTFI is
// measuring, and a server-side number cannot see them.
func (u *ttfiUser) recordTTFI(span trace.Span, metricName string, total time.Duration, attempts int, controlPlane, instruction time.Duration, err error) {
	span.SetAttributes(
		attribute.Float64("ttfi.total_ms", boomerutil.MsFloat(total)),
		attribute.Int("ttfi.attempts", attempts),
	)
	if controlPlane > 0 {
		span.SetAttributes(attribute.Float64("ttfi.control_plane_ms", boomerutil.MsFloat(controlPlane)))
	}
	if instruction > 0 {
		span.SetAttributes(attribute.Float64("ttfi.instruction_ms", boomerutil.MsFloat(instruction)))
	}
	boomerutil.LogSampledTrace(span, metricName, total, boomerutil.SourceClient, err)
	if err != nil {
		bmetrics.RecordFailure(ttfiMethod, metricName, u.userClass, total, err.Error())
		return
	}
	bmetrics.RecordSuccess(ttfiMethod, metricName, u.userClass, total, 0)
}

// firstInstruction drives the workload until one instruction comes back
// correct or the budget elapses, and reports the whole loop as one row. The
// retries are inside the TTFI window on purpose: until an instruction lands,
// the client cannot use the sandbox, so a request the router rejected while
// the actor was still activating is part of the wait. Whether a given failure
// is worth retrying is the workload's call, since only it knows what its own
// answers mean. Per-attempt visibility comes from the router's own server-side
// spans, which the injected trace context parents to this one.
func (u *ttfiUser) firstInstruction(ctx context.Context, budget time.Duration) (int, time.Duration, error) {
	ctx, span := u.cfg.Tracer.Start(ctx, ttfiInstructionMetric)
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
			span.SetAttributes(attribute.Int("ttfi.attempts", attempts))
			boomerutil.LogSampledTrace(span, ttfiInstructionMetric, latency, boomerutil.SourceClient, nil)
			bmetrics.RecordSuccess("http", ttfiInstructionMetric, u.userClass, latency, 0)
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
		time.Sleep(min(ttfiRetryDelay, remaining))
	}

	latency := time.Since(start)
	span.SetAttributes(attribute.Int("ttfi.attempts", attempts))
	boomerutil.LogSampledTrace(span, ttfiInstructionMetric, latency, boomerutil.SourceClient, lastErr)
	bmetrics.RecordFailure("http", ttfiInstructionMetric, u.userClass, latency, lastErr.Error())
	return attempts, latency, lastErr
}

// resume issues the explicit ResumeActor that opens an explicit-mode TTFI
// window, and returns the server-side handler duration so the parent can
// attribute how much of TTFI ateapi owns.
func (u *ttfiUser) resume(ctx context.Context) (time.Duration, error) {
	var serverElapsed time.Duration
	err := u.tracedCall(ctx, ttfiResumeMetric, &serverElapsed, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.ResumeActor(callCtx, &ateapipb.ResumeActorRequest{
			Actor: u.ref(),
		}, grpc.Trailer(tr))
		return err
	})
	return serverElapsed, err
}

func (u *ttfiUser) ensureAtespace(ctx context.Context) error {
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
// latency is reported but not counted as TTFI: a harness creates the record
// once and may do it long before the first instruction.
func (u *ttfiUser) create(ctx context.Context) error {
	return u.tracedCall(ctx, "CreateActor", nil, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.CreateActor(callCtx, &ateapipb.CreateActorRequest{
			Actor: &ateapipb.Actor{
				Metadata:      &ateapipb.ResourceMetadata{Atespace: u.cfg.Atespace, Name: u.actorName},
				ActorTemplate: &ateapipb.ObjectRef{Atespace: templateAtespace, Name: u.templateName},
			},
		}, grpc.Trailer(tr))
		return err
	})
}

func (u *ttfiUser) suspend(ctx context.Context) {
	_ = u.tracedCall(ctx, ttfiSuspendMetric, nil, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.SuspendActor(callCtx, &ateapipb.SuspendActorRequest{
			Actor: u.ref(),
		}, grpc.Trailer(tr))
		return err
	})
}

// suspendAndDelete suspends the actor before deleting it. DeleteActor
// requires SUSPENDED or CRASHED; deleting a running actor leaks it. The
// suspend is unmetered (teardown precondition, not benchmark latency), while
// the delete is metered so true leaks still surface in failures.csv.
func (u *ttfiUser) suspendAndDelete(ctx context.Context) {
	_, _ = u.cfg.APIStub.SuspendActor(ctx, &ateapipb.SuspendActorRequest{
		Actor: u.ref(),
	})
	u.delete(ctx)
}

func (u *ttfiUser) delete(ctx context.Context) {
	_ = u.tracedCall(ctx, "DeleteActor", nil, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.DeleteActor(callCtx, &ateapipb.DeleteActorRequest{
			Actor: u.ref(),
		}, grpc.Trailer(tr))
		return err
	})
}

// tracedCall wraps a unary gRPC call with a span and Prometheus/locust
// reporting, as the other user classes do. It additionally hands the
// server-side elapsed time back through serverElapsed (when non-nil) so a
// caller inside a TTFI window can report what share of the window ateapi's
// handler accounted for.
func (u *ttfiUser) tracedCall(ctx context.Context, name string, serverElapsed *time.Duration, do func(context.Context, *metadata.MD) error) error {
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
