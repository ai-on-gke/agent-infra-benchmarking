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

// This file is the --backend agent-sandbox half of runworkload: the substrate
// half lives in runworkload.go and neither imports the other's control plane.
// agent-sandbox has no actor that persists across iterations the way
// substrate's does — each measurement provisions a fresh Sandbox (ColdCreate)
// or SandboxClaim (PoolClaim), probes it, and deletes it — so its window is
// provisioning + instruction rather than a resume, and it addresses the pod's
// own Service directly instead of the atenet router. See the package doc.

package runworkload

import (
	"context"
	"fmt"
	"time"

	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/boomerutil"
	bmetrics "github.com/agent-substrate/substrate/internal/benchmarking/boomer/metrics"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/userclass"
	"github.com/google/uuid"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	sandboxv1beta1 "sigs.k8s.io/agent-sandbox/api/v1beta1"
	sandboxclientset "sigs.k8s.io/agent-sandbox/clients/k8s/clientset/versioned"
	extensionsclientset "sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned"
	extensionsv1beta1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

const (
	// DefaultProvisionTimeout bounds the agent-sandbox backend's wait for a
	// Sandbox/SandboxClaim's Ready condition when Options leaves it zero.
	// Substrate has no equivalent: ResumeActor is a single RPC, not a poll
	// loop. Generous relative to DefaultTimeout because a cold create can
	// include a real image pull.
	DefaultProvisionTimeout = 5 * time.Minute

	// retryDelay is the pause between instruction attempts, short relative to
	// a create/claim so the retry granularity does not show up in the
	// measurement.
	retryDelay = 25 * time.Millisecond

	// provisionPollInterval is the pause between Ready-condition polls.
	// Provisioning takes seconds, not milliseconds, so this can be coarser
	// than retryDelay.
	provisionPollInterval = 500 * time.Millisecond

	// probePort is the port the agent-sandbox ping workload's container
	// listens on. Fixed rather than configurable: it is part of the contract
	// between that workload and the SandboxTemplate it names.
	probePort int32 = 8888

	coldCreateMetric  = "ColdCreate"
	poolClaimMetric   = "PoolClaim"
	provisionMetric   = "Provision"
	instructionMetric = "Instruction"
)

// AgentSandboxDeps carries the agent-sandbox backend's dependencies.
// substrate's userclass.Config (an external type this package does not own)
// has no fields for them, so — like Options — they arrive via a setter rather
// than through the registry's Init parameter. Only read when the resolved
// backend is BackendAgentSandbox.
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

// resolveTimeout returns configured if positive, else def.
func resolveTimeout(configured, def time.Duration) time.Duration {
	if configured > 0 {
		return configured
	}
	return def
}

// startUserAgentSandbox builds one VU's agent-sandbox backend and measures its
// cold-create window. A failed cold window is a result, not a reason to refuse
// the VU: it is recorded as a ColdCreate failure and the next iteration's pool
// claim recovers whatever state was left behind, so this never returns an
// error the way substrate's startUser can.
func (r *runWorkloadRuntime) startUserAgentSandbox(ctx context.Context) *agentSandboxBackend {
	tmpl := r.opts.Template
	if tmpl == "" {
		tmpl = r.workload.DefaultTemplate()
	}
	pool := r.opts.WarmPool
	if pool == "" {
		pool = tmpl + "-warmpool"
	}
	b := &agentSandboxBackend{
		cfg:              r.cfg,
		workload:         r.workload,
		asDeps:           configuredASDeps,
		templateName:     tmpl,
		warmPoolName:     pool,
		provisionTimeout: resolveTimeout(r.opts.ProvisionTimeout, DefaultProvisionTimeout),
		timeout:          r.opts.Timeout,
	}
	bmetrics.UpdateUsers(runWorkloadUserClass, 1)
	b.coldWindow(ctx)
	return b
}

// iterateAgentSandbox is the agent-sandbox backend's iteration: the first call
// on a goroutine cold-creates a Sandbox and measures it; every later one
// deletes what the VU holds and measures a fresh pool claim.
func (r *runWorkloadRuntime) iterateAgentSandbox(ctx context.Context, gid int64) {
	val, loaded := r.users.Load(gid)
	if !loaded {
		u := r.startUserAgentSandbox(ctx)
		if _, loaded := r.users.LoadOrStore(gid, u); loaded {
			// Another user already holds this goroutine id, so the sandbox
			// just created would never be reached again. Drop it now rather
			// than leak it for the rest of the run.
			_ = u.teardown(ctx)
			bmetrics.UpdateUsers(runWorkloadUserClass, -1)
		}
		// The cold window was measured inside startUserAgentSandbox; returning
		// here keeps one iteration to one measurement.
		time.Sleep(r.dynamicWait())
		return
	}
	val.(*agentSandboxBackend).warmWindow(ctx)
	time.Sleep(r.dynamicWait())
}

// agentSandboxBackend drives sigs.k8s.io/agent-sandbox for one VU. It owns
// whatever object is currently live (a bare Sandbox or a SandboxClaim) and the
// window/retry machinery that measures a provisioning + instruction round —
// the agent-sandbox counterpart to runWorkloadUser's substrate methods.
type agentSandboxBackend struct {
	cfg              *userclass.Config
	workload         workload
	asDeps           AgentSandboxDeps
	templateName     string
	warmPoolName     string
	provisionTimeout time.Duration
	timeout          time.Duration // instruction retry budget

	// liveName and usingPool describe whatever object is currently live for
	// this VU: a bare Sandbox (usingPool false) or a SandboxClaim (usingPool
	// true). Empty liveName means nothing is live.
	liveName  string
	usingPool bool
	// counted tracks whether this VU currently contributes to the
	// boomer_active_actors gauge, so setLive/clearLive stay balanced.
	counted bool
}

// logName and logTemplate make agentSandboxBackend a sandboxUser, so the
// shared discard/shutdown teardown can name what it removed.
func (b *agentSandboxBackend) logName() string     { return b.liveName }
func (b *agentSandboxBackend) logTemplate() string { return b.templateName }

// setLive records the object teardown must later remove and, the first time a
// VU has one, counts it on the active gauge. clearLive is its inverse; the two
// keep the gauge balanced across the delete-and-reprovision cycle.
func (b *agentSandboxBackend) setLive(name string, usingPool bool) {
	b.liveName, b.usingPool = name, usingPool
	if !b.counted {
		activeActors.WithLabelValues(b.workload.Name()).Inc()
		b.counted = true
	}
}

func (b *agentSandboxBackend) clearLive() {
	b.liveName = ""
	if b.counted {
		activeActors.WithLabelValues(b.workload.Name()).Dec()
		b.counted = false
	}
}

// coldWindow measures the VU's first window: a Sandbox created directly from
// the template's blueprint, through Ready, then probed.
func (b *agentSandboxBackend) coldWindow(ctx context.Context) {
	b.measure(ctx, coldCreateMetric, true, b.first)
}

// warmWindow measures a later window. The delete of whatever the VU currently
// holds is the precondition for a warm measurement, not part of it: it runs
// before the measured span and reports its own row.
func (b *agentSandboxBackend) warmWindow(ctx context.Context) {
	_ = b.teardown(ctx)
	b.measure(ctx, poolClaimMetric, false, b.warm)
}

func (b *agentSandboxBackend) first(ctx context.Context) (actorRef, time.Duration, error) {
	return b.timedProvision(ctx, b.doCreateCold)
}

func (b *agentSandboxBackend) warm(ctx context.Context) (actorRef, time.Duration, error) {
	return b.timedProvision(ctx, b.doClaimFromPool)
}

// measure times one window and reports it as metricName. The parent span's
// duration is the measurement, so every phase a client would wait through —
// provisioning and the instruction — runs inside it and nothing else does.
// This is the agent-sandbox analog of substrate's measureTTFI, but the window
// is provisioning + instruction rather than a resume, so it carries client
// wall clock and a control_plane/instruction split rather than a server
// elapsed trailer.
func (b *agentSandboxBackend) measure(ctx context.Context, metricName string, firstWindow bool, provision func(context.Context) (actorRef, time.Duration, error)) {
	ctx, span := b.cfg.Tracer.Start(ctx, metricName)
	defer span.End()
	span.SetAttributes(
		attribute.String("workload", b.workload.Name()),
		attribute.String("template", b.templateName),
		attribute.String("backend", string(BackendAgentSandbox)),
		attribute.Bool("cold_window", firstWindow),
	)

	start := time.Now()

	actor, controlPlane, err := provision(ctx)
	if err != nil {
		b.recordWindow(span, metricName, time.Since(start), 0, controlPlane, 0, err)
		return
	}

	attempts, instruction, err := b.firstInstruction(ctx, actor)
	b.recordWindow(span, metricName, time.Since(start), attempts, controlPlane, instruction, err)
}

// recordWindow closes out a measurement window. It carries client wall clock,
// a deliberate departure from substrate's server-elapsed rows: the gaps
// between provisioning and the instruction are exactly what this backend
// measures, and a server-side number cannot see them.
func (b *agentSandboxBackend) recordWindow(span trace.Span, metricName string, total time.Duration, attempts int, controlPlane, instruction time.Duration, err error) {
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
// lands, the client cannot use the sandbox, so a request rejected while it was
// still activating is part of the wait. Whether a given failure is worth
// retrying is the workload's call, since only it knows what its answers mean.
func (b *agentSandboxBackend) firstInstruction(ctx context.Context, actor actorRef) (int, time.Duration, error) {
	ctx, span := b.cfg.Tracer.Start(ctx, instructionMetric)
	defer span.End()

	budget := b.timeout
	if budget <= 0 {
		budget = DefaultTimeout
	}
	start := time.Now()
	deadline := start.Add(budget)

	var attempts int
	var lastErr error
	for {
		attempts++
		retryable, err := b.workload.SendInstruction(ctx, actor)
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

// timedProvision runs do inside a Provision span and returns how long it took,
// so the caller can attribute the control-plane share of a window.
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
	b.setLive(name, false)

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

// doClaimFromPool claims a pre-warmed sandbox from the SandboxWarmPool through
// to Ready. Adoption is a label flip onto an already-running pod, so this is
// the "warm" number ColdCreate is compared against.
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
	b.setLive(name, true)

	// The claim mirrors the adopted Sandbox's name onto its own status as soon
	// as it's Ready, but not every controller version also mirrors
	// serviceFQDN there (that field is newer than status.sandbox.name, which
	// has been present since the claim API's first release). Resolving the
	// FQDN from the Sandbox object itself, once we know its name, works
	// regardless of which the running controller mirrors.
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
// sandbox name, depending on the caller); want names that result for the error
// message. A ready report with an empty result is a configuration problem
// (e.g. the SandboxTemplate not setting spec.service: true) rather than
// something worth retrying past.
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

// teardown deletes whatever object is currently live for this VU, if any, and
// reports whether it went so the shared teardown walker can name a leak. A
// NotFound is success: the object is gone, which is the point. It doubles as
// the warm-window precondition (delete the previous claim before the next),
// which is why it is safe to call every iteration.
func (b *agentSandboxBackend) teardown(ctx context.Context) error {
	if b.liveName == "" {
		return nil
	}
	name, usingPool := b.liveName, b.usingPool
	b.clearLive()

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
		return err
	}
	boomerutil.LogSampledTrace(span, spanName, latency, boomerutil.SourceClient, nil)
	bmetrics.RecordSuccess("k8s", spanName, runWorkloadUserClass, latency, 0)
	return nil
}
