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

// Package runworkload drives a sandbox through agent-sandbox's provisioning
// paths and measures the wall clock a client waits before the sandbox
// accepts work.
//
// This is a from-scratch reimplementation of the RunWorkloadUser this
// benchmark shipped against substrate. substrate measured one actor cycling
// through its own ResumeActor/SuspendActor control-plane RPCs, timing a true
// live-memory snapshot restore (its "WarmStart", ~100ms target). agent-sandbox
// has no equivalent RPC or live-memory-snapshot warm pool on a portable
// (non-GKE) cluster, so this measures the two provisioning paths agent-sandbox
// actually offers instead:
//
//   - ColdCreate: create a Sandbox directly from a SandboxTemplate's
//     blueprint, bypassing any warm pool. Image pull, pod scheduling and
//     application start all happen inside the window — a true cold boot.
//   - PoolClaim: create a SandboxClaim against a SandboxWarmPool. Adoption is
//     a label flip onto an already-running pod, so this is the "warm" number
//     to compare ColdCreate against.
//
// Neither name should be read as substrate's FirstResume/WarmStart: there is
// no live-memory restore here, only cheaper-vs-more-expensive provisioning.
// A GKE Pod Snapshot golden-pool setup (see
// examples/podsnapshot-golden-warmpool in kubernetes-sigs/agent-sandbox) gets
// closer to substrate's golden-snapshot behavior, but that is GKE-only and
// out of scope for this package — see the repo README for the tracking note.
//
// Two spans bracket each window's provisioning and instruction phases
// (Provision, Instruction) so one sampled trace explains its own total,
// mirroring the substrate benchmark's Resume/Instruction split.
package runworkload

import (
	"context"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"os"
	"sync"
	"time"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	sandboxv1beta1 "sigs.k8s.io/agent-sandbox/api/v1beta1"
	extensionsv1beta1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"

	"github.com/ai-on-gke/agent-infra-benchmarking/internal/benchmarking/boomer/boomerutil"
	bmetrics "github.com/ai-on-gke/agent-infra-benchmarking/internal/benchmarking/boomer/metrics"
	"github.com/ai-on-gke/agent-infra-benchmarking/internal/benchmarking/boomer/userclass"
)

const (
	// Locust class name from tests/runworkload.py; must match boomer.Task.Name.
	runWorkloadUserClass = "RunWorkloadUser"

	// DefaultWorkload is the workload a RunWorkloadUser drives unless Options
	// names another. The default template follows from the workload rather
	// than being fixed here, so the two settings cannot drift into naming a
	// template the workload has no way to address. Exported because the
	// worker's --workload-name usage string states it.
	DefaultWorkload = pingWorkloadName

	// DefaultTimeout bounds the instruction retry loop when Options leaves it
	// zero. A sandbox that has not taken an instruction by then is recorded
	// as a failure instead of being waited on for the rest of the run.
	DefaultTimeout = 60 * time.Second

	// DefaultProvisionTimeout bounds the wait for a Sandbox/SandboxClaim's
	// Ready condition when Options leaves it zero. Generous relative to
	// DefaultTimeout because a cold create can include a real image pull,
	// which the instruction retry budget is not meant to absorb.
	DefaultProvisionTimeout = 5 * time.Minute

	// Delay between instruction attempts, short relative to provisioning so
	// the retry granularity does not show up in the measurement.
	retryDelay = 25 * time.Millisecond

	// Delay between Ready-condition polls. Sandbox/SandboxClaim provisioning
	// takes seconds, not milliseconds, so this can be coarser than
	// retryDelay without costing measurement precision.
	provisionPollInterval = 500 * time.Millisecond

	// probePort is the port every workload's container listens on. Fixed
	// rather than configurable: it is part of the contract between a
	// workload implementation and the SandboxTemplate it names, same as the
	// template name itself.
	probePort int32 = 8888

	coldCreateMetric  = "ColdCreate"
	poolClaimMetric   = "PoolClaim"
	provisionMetric   = "Provision"
	instructionMetric = "Instruction"

	// Method label for the composite rows. A measurement window spans
	// Kubernetes object creation/wait and an HTTP instruction, so neither
	// transport label the sub-spans use describes it.
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

	// Template overrides the SandboxTemplate the workload would measure.
	// Empty leaves that choice to the workload, which is the only place that
	// knows what it can address.
	Template string

	// WarmPool overrides the SandboxWarmPool the pool-claim window adopts
	// from. Empty selects "<template>-warmpool".
	WarmPool string

	// Timeout is the ceiling on the instruction retry loop. Zero selects
	// DefaultTimeout. Negative is a caller error, rejected where the flag is
	// declared rather than silently treated as unset.
	Timeout time.Duration

	// ProvisionTimeout is the ceiling on waiting for a Sandbox/SandboxClaim's
	// Ready condition. Zero selects DefaultProvisionTimeout.
	ProvisionTimeout time.Duration
}

// configured is what SetOptions recorded. It is read exactly once, by
// initRunWorkload, and passed by value from there on, so nothing on the
// measurement path reads mutable package state.
var configured Options

// SetOptions records the settings initRunWorkload will use. It must run
// before the registered entry's Init, which main satisfies by calling it
// after flag.Parse and before the userclass lookup.
func SetOptions(o Options) { configured = o }

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
		cfg.Tracer = otel.Tracer("agent-sandbox-boomer/runworkload")
	}
	wl, err := resolveWorkload(configured.Workload)
	if err != nil {
		slog.Error("fatal: cannot select the workload",
			slog.String("err", err.Error()))
		os.Exit(1)
	}
	rt := &runWorkloadRuntime{cfg: cfg, opts: configured, workload: wl}
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

// iterate is the task function boomer calls in a loop on each VU goroutine.
// The first call on a goroutine cold-creates a sandbox and measures it;
// every later call releases whatever the goroutine currently holds and
// measures claiming a fresh one from the warm pool.
func (r *runWorkloadRuntime) iterate() {
	gid := boomerutil.GoroutineID()
	val, loaded := r.users.Load(gid)
	if !loaded {
		u, err := r.startUser(context.Background(), r.opts)
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
			u.teardown(context.Background())
			bmetrics.UpdateUsers(runWorkloadUserClass, -1)
		}
		// The cold window was measured inside startUser. Returning here
		// keeps one iteration to one measurement, so the cold and pool rows
		// stay independent samples rather than being averaged into one
		// iteration's think time.
		time.Sleep(r.dynamicWait())
		return
	}
	user := val.(*runWorkloadUser)

	ctx := context.Background()
	// Teardown is the precondition for a pool-claim measurement, not part of
	// it: it runs outside the measured span and reports its own row.
	user.teardown(ctx)
	user.measure(ctx, poolClaimMetric, user.claimFromPool)

	time.Sleep(r.dynamicWait())
}

func (r *runWorkloadRuntime) startUser(ctx context.Context, opts Options) (*runWorkloadUser, error) {
	tmpl := opts.Template
	if tmpl == "" {
		tmpl = r.workload.DefaultTemplate()
	}
	pool := opts.WarmPool
	if pool == "" {
		pool = tmpl + "-warmpool"
	}

	u := &runWorkloadUser{
		cfg:              r.cfg,
		workload:         r.workload,
		templateName:     tmpl,
		warmPoolName:     pool,
		userClass:        runWorkloadUserClass,
		timeout:          resolveTimeout(opts.Timeout, DefaultTimeout),
		provisionTimeout: resolveTimeout(opts.ProvisionTimeout, DefaultProvisionTimeout),
	}
	bmetrics.UpdateUsers(runWorkloadUserClass, 1)

	// A failed cold-create window is a result, not a reason to drop the
	// user: it is already recorded as a ColdCreate failure, and the next
	// iteration's teardown is a no-op if nothing was actually created.
	u.measure(ctx, coldCreateMetric, u.createCold)
	return u, nil
}

func (r *runWorkloadRuntime) shutdown(ctx context.Context) {
	r.users.Range(func(_, val any) bool {
		u := val.(*runWorkloadUser)
		u.teardown(ctx)
		bmetrics.UpdateUsers(runWorkloadUserClass, -1)
		return true
	})
}

type runWorkloadUser struct {
	cfg          *userclass.Config
	workload     workload
	templateName string
	warmPoolName string
	userClass    string

	timeout          time.Duration
	provisionTimeout time.Duration

	// name and usingPool describe whatever object is currently live for this
	// user: a bare Sandbox (usingPool false) or a SandboxClaim (usingPool
	// true). Empty name means nothing is live.
	name      string
	usingPool bool
}

// measure times one window and reports it as metricName. The parent span's
// duration is the measurement, so both provisioning and the instruction that
// proves the sandbox is usable run inside it, and nothing else does.
func (u *runWorkloadUser) measure(ctx context.Context, metricName string, provision func(context.Context) (sandboxTarget, time.Duration, error)) {
	ctx, span := u.cfg.Tracer.Start(ctx, metricName)
	defer span.End()
	span.SetAttributes(
		attribute.String("workload", u.workload.Name()),
		attribute.String("template", u.templateName),
	)

	start := time.Now()
	target, provisionElapsed, err := provision(ctx)
	if err != nil {
		u.recordWindow(span, metricName, time.Since(start), 0, provisionElapsed, 0, err)
		return
	}

	attempts, instruction, err := u.firstInstruction(ctx, target, u.timeout)
	u.recordWindow(span, metricName, time.Since(start), attempts, provisionElapsed, instruction, err)
}

// recordWindow closes out a measurement window: the attributes make one
// sampled trace self-explaining, and the row carries client wall clock —
// there is no server-elapsed trailer to prefer here the way substrate's
// gRPC calls had one, since provisioning is a client-side create-then-poll
// loop against the Kubernetes API rather than a single RPC.
func (u *runWorkloadUser) recordWindow(span trace.Span, metricName string, total time.Duration, attempts int, provision, instruction time.Duration, err error) {
	span.SetAttributes(
		attribute.Float64("total_ms", boomerutil.MsFloat(total)),
		attribute.Int("attempts", attempts),
	)
	if provision > 0 {
		span.SetAttributes(attribute.Float64("provision_ms", boomerutil.MsFloat(provision)))
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
// lands, the client cannot use the sandbox, so a request the sandbox rejected
// while its application was still starting is part of the wait.
func (u *runWorkloadUser) firstInstruction(ctx context.Context, target sandboxTarget, budget time.Duration) (int, time.Duration, error) {
	ctx, span := u.cfg.Tracer.Start(ctx, instructionMetric)
	defer span.End()

	start := time.Now()
	deadline := start.Add(budget)

	var attempts int
	var lastErr error
	for {
		attempts++
		retryable, err := u.workload.SendInstruction(ctx, target)
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

// createCold times creating a Sandbox directly from the template's blueprint,
// bypassing any warm pool, through to Ready. This is agent-sandbox's true
// cold-boot path: image pull, scheduling, and application start all happen
// inside the window.
func (u *runWorkloadUser) createCold(ctx context.Context) (sandboxTarget, time.Duration, error) {
	ctx, span := u.cfg.Tracer.Start(ctx, provisionMetric)
	defer span.End()
	start := time.Now()

	target, err := u.doCreateCold(ctx)
	elapsed := time.Since(start)
	boomerutil.LogSampledTrace(span, provisionMetric, elapsed, boomerutil.SourceClient, err)
	if err != nil {
		bmetrics.RecordFailure("k8s", provisionMetric, u.userClass, elapsed, err.Error())
		return sandboxTarget{}, elapsed, err
	}
	bmetrics.RecordSuccess("k8s", provisionMetric, u.userClass, elapsed, 0)
	return target, elapsed, nil
}

func (u *runWorkloadUser) doCreateCold(ctx context.Context) (sandboxTarget, error) {
	tmpl, err := u.cfg.Extensions.ExtensionsV1beta1().SandboxTemplates(u.cfg.Namespace).Get(ctx, u.templateName, metav1.GetOptions{})
	if err != nil {
		return sandboxTarget{}, fmt.Errorf("get SandboxTemplate %s: %w", u.templateName, err)
	}

	name := "sb-cold-" + uuid.NewString()
	sb := &sandboxv1beta1.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: u.cfg.Namespace},
		Spec:       sandboxv1beta1.SandboxSpec{SandboxBlueprint: tmpl.Spec.SandboxBlueprint},
	}
	if _, err := u.cfg.Sandboxes.AgentsV1beta1().Sandboxes(u.cfg.Namespace).Create(ctx, sb, metav1.CreateOptions{}); err != nil {
		return sandboxTarget{}, fmt.Errorf("create Sandbox %s: %w", name, err)
	}
	// Recorded before the wait so a failed or interrupted wait still leaves
	// teardown something to clean up.
	u.name, u.usingPool = name, false

	fqdn, err := u.waitReady(ctx, u.provisionTimeout, func(ctx context.Context) (string, bool, error) {
		got, err := u.cfg.Sandboxes.AgentsV1beta1().Sandboxes(u.cfg.Namespace).Get(ctx, name, metav1.GetOptions{})
		if err != nil {
			return "", false, err
		}
		if apimeta.IsStatusConditionTrue(got.Status.Conditions, string(sandboxv1beta1.SandboxConditionReady)) {
			return got.Status.ServiceFQDN, true, nil
		}
		return "", false, nil
	})
	if err != nil {
		return sandboxTarget{}, err
	}
	return sandboxTarget{HTTPClient: u.cfg.HTTPClient, ServiceFQDN: fqdn, Port: probePort}, nil
}

// claimFromPool times claiming a pre-warmed sandbox from the SandboxWarmPool
// through to Ready. Adoption is a label flip onto an already-running pod, so
// this is the "warm" number ColdCreate is compared against.
func (u *runWorkloadUser) claimFromPool(ctx context.Context) (sandboxTarget, time.Duration, error) {
	ctx, span := u.cfg.Tracer.Start(ctx, provisionMetric)
	defer span.End()
	start := time.Now()

	target, err := u.doClaimFromPool(ctx)
	elapsed := time.Since(start)
	boomerutil.LogSampledTrace(span, provisionMetric, elapsed, boomerutil.SourceClient, err)
	if err != nil {
		bmetrics.RecordFailure("k8s", provisionMetric, u.userClass, elapsed, err.Error())
		return sandboxTarget{}, elapsed, err
	}
	bmetrics.RecordSuccess("k8s", provisionMetric, u.userClass, elapsed, 0)
	return target, elapsed, nil
}

func (u *runWorkloadUser) doClaimFromPool(ctx context.Context) (sandboxTarget, error) {
	name := "sb-pool-" + uuid.NewString()
	claim := &extensionsv1beta1.SandboxClaim{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: u.cfg.Namespace},
		Spec: extensionsv1beta1.SandboxClaimSpec{
			WarmPoolRef: extensionsv1beta1.SandboxWarmPoolRef{Name: u.warmPoolName},
		},
	}
	if _, err := u.cfg.Extensions.ExtensionsV1beta1().SandboxClaims(u.cfg.Namespace).Create(ctx, claim, metav1.CreateOptions{}); err != nil {
		return sandboxTarget{}, fmt.Errorf("create SandboxClaim %s: %w", name, err)
	}
	u.name, u.usingPool = name, true

	fqdn, err := u.waitReady(ctx, u.provisionTimeout, func(ctx context.Context) (string, bool, error) {
		got, err := u.cfg.Extensions.ExtensionsV1beta1().SandboxClaims(u.cfg.Namespace).Get(ctx, name, metav1.GetOptions{})
		if err != nil {
			return "", false, err
		}
		if apimeta.IsStatusConditionTrue(got.Status.Conditions, string(sandboxv1beta1.SandboxConditionReady)) {
			return got.Status.SandboxStatus.ServiceFQDN, true, nil
		}
		return "", false, nil
	})
	if err != nil {
		return sandboxTarget{}, err
	}
	return sandboxTarget{HTTPClient: u.cfg.HTTPClient, ServiceFQDN: fqdn, Port: probePort}, nil
}

// waitReady polls poll until it reports ready, timeout elapses, or ctx ends.
// poll returns the sandbox's serviceFQDN once ready; a ready report with no
// FQDN is a template misconfiguration (spec.service must be true) rather
// than something worth retrying past.
func (u *runWorkloadUser) waitReady(ctx context.Context, timeout time.Duration, poll func(context.Context) (fqdn string, ready bool, err error)) (string, error) {
	deadline := time.Now().Add(timeout)
	for {
		fqdn, ready, err := poll(ctx)
		if err != nil {
			return "", err
		}
		if ready {
			if fqdn == "" {
				return "", fmt.Errorf("ready with no serviceFQDN; the SandboxTemplate must set spec.service: true")
			}
			return fqdn, nil
		}
		if time.Now().After(deadline) {
			return "", fmt.Errorf("not ready within %s", timeout)
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(provisionPollInterval):
		}
	}
}

// teardown deletes whatever object is currently live for this user, if any.
// Best-effort: the delete is reported to metrics/tracing, but a failure here
// does not stop the run — the next createCold/claimFromPool call starts a
// fresh object under a fresh name regardless.
func (u *runWorkloadUser) teardown(ctx context.Context) {
	if u.name == "" {
		return
	}
	name, usingPool := u.name, u.usingPool
	u.name = ""

	spanName, deleteFn := "DeleteSandbox", func(ctx context.Context) error {
		return u.cfg.Sandboxes.AgentsV1beta1().Sandboxes(u.cfg.Namespace).Delete(ctx, name, metav1.DeleteOptions{})
	}
	if usingPool {
		spanName, deleteFn = "DeleteSandboxClaim", func(ctx context.Context) error {
			return u.cfg.Extensions.ExtensionsV1beta1().SandboxClaims(u.cfg.Namespace).Delete(ctx, name, metav1.DeleteOptions{})
		}
	}

	ctx, span := u.cfg.Tracer.Start(ctx, spanName)
	defer span.End()
	start := time.Now()
	err := deleteFn(ctx)
	latency := time.Since(start)
	if err != nil && !apierrors.IsNotFound(err) {
		boomerutil.LogSampledTrace(span, spanName, latency, boomerutil.SourceClient, err)
		bmetrics.RecordFailure("k8s", spanName, u.userClass, latency, err.Error())
		return
	}
	boomerutil.LogSampledTrace(span, spanName, latency, boomerutil.SourceClient, nil)
	bmetrics.RecordSuccess("k8s", spanName, u.userClass, latency, 0)
}
