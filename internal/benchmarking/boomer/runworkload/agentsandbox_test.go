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

package runworkload

import (
	"context"
	"sync"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clienttesting "k8s.io/client-go/testing"
	sandboxv1beta1 "sigs.k8s.io/agent-sandbox/api/v1beta1"
	sandboxfake "sigs.k8s.io/agent-sandbox/clients/k8s/clientset/versioned/fake"
	extensionsfake "sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned/fake"
	extensionsv1beta1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"

	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/dynconfig"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/userclass"
)

const testNamespace = "benchmarking"

// newReadySandboxes returns a fake core clientset where every Sandbox
// Create() is captured, and every subsequent Get() on that name reports it
// Ready with fqdn regardless of what a real controller would do — there is
// no controller here to flip status, so the test stands in for one.
// preseed names an existing sandbox as already present (Ready, empty spec)
// without going through Create() — standing in for a warm-pool spare that a
// SandboxClaim adopts rather than creates.
func newReadySandboxes(fqdn string, preseed ...string) *sandboxfake.Clientset {
	cs := sandboxfake.NewSimpleClientset()
	var mu sync.Mutex
	specs := map[string]sandboxv1beta1.SandboxSpec{}
	for _, name := range preseed {
		specs[name] = sandboxv1beta1.SandboxSpec{}
	}

	cs.PrependReactor("create", "sandboxes", func(action clienttesting.Action) (bool, runtime.Object, error) {
		sb := action.(clienttesting.CreateAction).GetObject().(*sandboxv1beta1.Sandbox)
		mu.Lock()
		specs[sb.Name] = sb.Spec
		mu.Unlock()
		return false, nil, nil // let the default tracker-backed reactor also store it.
	})
	cs.PrependReactor("get", "sandboxes", func(action clienttesting.Action) (bool, runtime.Object, error) {
		ga := action.(clienttesting.GetAction)
		mu.Lock()
		spec, ok := specs[ga.GetName()]
		mu.Unlock()
		if !ok {
			return false, nil, nil
		}
		return true, &sandboxv1beta1.Sandbox{
			ObjectMeta: metav1.ObjectMeta{Name: ga.GetName(), Namespace: ga.GetNamespace()},
			Spec:       spec,
			Status: sandboxv1beta1.SandboxStatus{
				Conditions:  []metav1.Condition{readyCondition()},
				ServiceFQDN: fqdn,
			},
		}, nil
	})
	cs.PrependReactor("delete", "sandboxes", func(action clienttesting.Action) (bool, runtime.Object, error) {
		da := action.(clienttesting.DeleteAction)
		mu.Lock()
		delete(specs, da.GetName())
		mu.Unlock()
		return false, nil, nil // let the default tracker-backed reactor also delete it.
	})
	return cs
}

// newReadyExtensions returns a fake extensions clientset seeded with
// templates, where every SandboxClaim Create() is captured and every
// subsequent Get() reports it Ready, having adopted adoptedSandboxName —
// the same stand-in as newReadySandboxes, for the pool-claim path. Mirrors
// only status.sandbox.name (not serviceFQDN), matching controller versions
// that predate the FQDN mirror; doClaimFromPool is expected to resolve the
// FQDN from the named Sandbox object itself, not from the claim.
func newReadyExtensions(adoptedSandboxName string, templates ...*extensionsv1beta1.SandboxTemplate) *extensionsfake.Clientset {
	objs := make([]runtime.Object, len(templates))
	for i, tpl := range templates {
		objs[i] = tpl
	}
	cs := extensionsfake.NewSimpleClientset(objs...)
	var mu sync.Mutex
	warmPoolRefs := map[string]string{}

	cs.PrependReactor("create", "sandboxclaims", func(action clienttesting.Action) (bool, runtime.Object, error) {
		claim := action.(clienttesting.CreateAction).GetObject().(*extensionsv1beta1.SandboxClaim)
		mu.Lock()
		warmPoolRefs[claim.Name] = claim.Spec.WarmPoolRef.Name
		mu.Unlock()
		return false, nil, nil
	})
	cs.PrependReactor("get", "sandboxclaims", func(action clienttesting.Action) (bool, runtime.Object, error) {
		ga := action.(clienttesting.GetAction)
		mu.Lock()
		ref, ok := warmPoolRefs[ga.GetName()]
		mu.Unlock()
		if !ok {
			return false, nil, nil
		}
		return true, &extensionsv1beta1.SandboxClaim{
			ObjectMeta: metav1.ObjectMeta{Name: ga.GetName(), Namespace: ga.GetNamespace()},
			Spec:       extensionsv1beta1.SandboxClaimSpec{WarmPoolRef: extensionsv1beta1.SandboxWarmPoolRef{Name: ref}},
			Status: extensionsv1beta1.SandboxClaimStatus{
				Conditions:    []metav1.Condition{readyCondition()},
				SandboxStatus: extensionsv1beta1.SandboxStatus{Name: adoptedSandboxName},
			},
		}, nil
	})
	cs.PrependReactor("delete", "sandboxclaims", func(action clienttesting.Action) (bool, runtime.Object, error) {
		da := action.(clienttesting.DeleteAction)
		mu.Lock()
		delete(warmPoolRefs, da.GetName())
		mu.Unlock()
		return false, nil, nil // let the default tracker-backed reactor also delete it.
	})
	return cs
}

func readyCondition() metav1.Condition {
	return metav1.Condition{
		Type:               string(sandboxv1beta1.SandboxConditionReady),
		Status:             metav1.ConditionTrue,
		Reason:             "Ready",
		LastTransitionTime: metav1.Now(),
	}
}

func templateWithImage(name, image string) *extensionsv1beta1.SandboxTemplate {
	return &extensionsv1beta1.SandboxTemplate{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: testNamespace},
		Spec: extensionsv1beta1.SandboxTemplateSpec{
			SandboxBlueprint: sandboxv1beta1.SandboxBlueprint{
				PodTemplate: sandboxv1beta1.PodTemplate{
					Spec: corev1.PodSpec{
						Containers: []corev1.Container{{Name: "probe", Image: image}},
					},
				},
			},
		},
	}
}

func newAgentSandboxTestBackend(t *testing.T, sandboxes *sandboxfake.Clientset, extensions *extensionsfake.Clientset, templateName string) *agentSandboxBackend {
	t.Helper()
	tracer, _ := newRecordingTracer(t)
	return &agentSandboxBackend{
		cfg:              &userclass.Config{Tracer: tracer},
		asDeps:           AgentSandboxDeps{Sandboxes: sandboxes, Extensions: extensions, Namespace: testNamespace},
		templateName:     templateName,
		warmPoolName:     templateName + "-warmpool",
		provisionTimeout: 5 * time.Second,
	}
}

func TestDoCreateColdBuildsSandboxFromTemplateBlueprint(t *testing.T) {
	extensions := newReadyExtensions("", templateWithImage("ping", "ping-probe:v1"))
	sandboxes := newReadySandboxes("sb-cold-x.benchmarking.svc.cluster.local")
	b := newAgentSandboxTestBackend(t, sandboxes, extensions, "ping")

	actor, err := b.doCreateCold(context.Background())
	if err != nil {
		t.Fatalf("doCreateCold failed: %v", err)
	}
	if actor.ServiceFQDN != "sb-cold-x.benchmarking.svc.cluster.local" {
		t.Errorf("ServiceFQDN: got %q", actor.ServiceFQDN)
	}
	if actor.Port != probePort {
		t.Errorf("Port: got %d, want %d", actor.Port, probePort)
	}
	if b.usingPool || b.liveName == "" {
		t.Fatalf("backend state after doCreateCold: liveName=%q usingPool=%v", b.liveName, b.usingPool)
	}

	got, err := sandboxes.AgentsV1beta1().Sandboxes(testNamespace).Get(context.Background(), b.liveName, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get created Sandbox: %v", err)
	}
	if len(got.Spec.PodTemplate.Spec.Containers) != 1 || got.Spec.PodTemplate.Spec.Containers[0].Image != "ping-probe:v1" {
		t.Errorf("created Sandbox did not carry the template's blueprint: %+v", got.Spec.PodTemplate.Spec)
	}
}

func TestDoCreateColdFailsWhenTemplateIsMissing(t *testing.T) {
	b := newAgentSandboxTestBackend(t, newReadySandboxes(""), newReadyExtensions(""), "does-not-exist")

	_, err := b.doCreateCold(context.Background())
	if err == nil {
		t.Fatal("doCreateCold succeeded for a missing template, want error")
	}
	if !apierrors.IsNotFound(err) {
		t.Errorf("expected a NotFound error, got: %v", err)
	}
}

func TestDoClaimFromPoolReferencesTheWarmPool(t *testing.T) {
	sandboxes := newReadySandboxes("sb-pool-x.benchmarking.svc.cluster.local", "adopted-sandbox")
	extensions := newReadyExtensions("adopted-sandbox")
	b := newAgentSandboxTestBackend(t, sandboxes, extensions, "ping")

	actor, err := b.doClaimFromPool(context.Background())
	if err != nil {
		t.Fatalf("doClaimFromPool failed: %v", err)
	}
	if actor.ServiceFQDN != "sb-pool-x.benchmarking.svc.cluster.local" {
		t.Errorf("ServiceFQDN: got %q", actor.ServiceFQDN)
	}
	if !b.usingPool || b.liveName == "" {
		t.Fatalf("backend state after doClaimFromPool: liveName=%q usingPool=%v", b.liveName, b.usingPool)
	}

	got, err := extensions.ExtensionsV1beta1().SandboxClaims(testNamespace).Get(context.Background(), b.liveName, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get created SandboxClaim: %v", err)
	}
	if got.Spec.WarmPoolRef.Name != "ping-warmpool" {
		t.Errorf("WarmPoolRef: got %q, want %q", got.Spec.WarmPoolRef.Name, "ping-warmpool")
	}
}

func TestAgentSandboxTeardownDeletesTheRightObjectType(t *testing.T) {
	t.Run("cold Sandbox", func(t *testing.T) {
		sandboxes := newReadySandboxes("fqdn")
		b := newAgentSandboxTestBackend(t, sandboxes, newReadyExtensions("", templateWithImage("ping", "img")), "ping")
		if _, err := b.doCreateCold(context.Background()); err != nil {
			t.Fatalf("doCreateCold failed: %v", err)
		}
		name := b.liveName

		b.teardown(context.Background())

		if b.liveName != "" {
			t.Errorf("liveName after teardown: got %q, want empty", b.liveName)
		}
		if _, err := sandboxes.AgentsV1beta1().Sandboxes(testNamespace).Get(context.Background(), name, metav1.GetOptions{}); !apierrors.IsNotFound(err) {
			t.Errorf("Sandbox %s still exists after teardown (err=%v)", name, err)
		}
	})

	t.Run("pool SandboxClaim", func(t *testing.T) {
		sandboxes := newReadySandboxes("fqdn", "adopted-sandbox")
		extensions := newReadyExtensions("adopted-sandbox")
		b := newAgentSandboxTestBackend(t, sandboxes, extensions, "ping")
		if _, err := b.doClaimFromPool(context.Background()); err != nil {
			t.Fatalf("doClaimFromPool failed: %v", err)
		}
		name := b.liveName

		b.teardown(context.Background())

		if b.liveName != "" {
			t.Errorf("liveName after teardown: got %q, want empty", b.liveName)
		}
		if _, err := extensions.ExtensionsV1beta1().SandboxClaims(testNamespace).Get(context.Background(), name, metav1.GetOptions{}); !apierrors.IsNotFound(err) {
			t.Errorf("SandboxClaim %s still exists after teardown (err=%v)", name, err)
		}
	})
}

// The measurement is the parent span's duration and the phase split lives on
// its attributes — the same contract substrate's TestSpanCarriesTheBreakdown
// checks, exercised here through the agent-sandbox backend instead.
func TestAgentSandboxMeasureSpanCarriesTheBreakdown(t *testing.T) {
	tracer, sr := newRecordingTracer(t)
	sandboxes := newReadySandboxes("sb-cold-x.benchmarking.svc.cluster.local")
	extensions := newReadyExtensions("", templateWithImage("ping", "ping-probe:v1"))
	wl := &fakeAgentSandboxWorkload{name: "ping"}

	cfg := &userclass.Config{Tracer: tracer, Dyn: dynconfig.NewHolder(dynconfig.Config{})}
	b := &agentSandboxBackend{
		cfg:              cfg,
		asDeps:           AgentSandboxDeps{Sandboxes: sandboxes, Extensions: extensions, Namespace: testNamespace},
		templateName:     "ping",
		provisionTimeout: 5 * time.Second,
	}
	u := &runWorkloadUser{cfg: cfg, workload: wl, backend: b}

	u.measure(context.Background(), coldCreateMetric, Options{Timeout: 5 * time.Second}, true, func(ctx context.Context) (actorRef, time.Duration, error) {
		return b.first(ctx, dynconfig.Config{})
	})

	parent := spanByName(t, sr, coldCreateMetric)
	if got := mustAttr(t, parent, "template").AsString(); got != "ping" {
		t.Errorf("template: got %q, want %q", got, "ping")
	}
	if got := mustAttr(t, parent, "backend").AsString(); got != string(BackendAgentSandbox) {
		t.Errorf("backend: got %q, want %q", got, BackendAgentSandbox)
	}
	if got := mustAttr(t, parent, "attempts").AsInt64(); got != 1 {
		t.Errorf("attempts: got %d, want 1", got)
	}
	if got := mustAttr(t, parent, "control_plane_ms").AsFloat64(); got <= 0 {
		t.Errorf("control_plane_ms: got %v, want > 0", got)
	}
	if got := mustAttr(t, parent, "instruction_ms").AsFloat64(); got <= 0 {
		t.Errorf("instruction_ms: got %v, want > 0", got)
	}

	for _, child := range []string{provisionMetric, instructionMetric} {
		s := spanByName(t, sr, child)
		if s.Parent().SpanID() != parent.SpanContext().SpanID() {
			t.Errorf("span %q is not a child of %q", child, coldCreateMetric)
		}
	}

	if calls := wl.recordedCalls(); len(calls) != 1 {
		t.Fatalf("instructions sent: got %d, want 1", len(calls))
	} else if calls[0].ServiceFQDN != "sb-cold-x.benchmarking.svc.cluster.local" {
		t.Errorf("instruction addressed %q, want the created sandbox's FQDN", calls[0].ServiceFQDN)
	}
}

func TestAgentSandboxMeasureRecordsFailureWithoutSendingInstructions(t *testing.T) {
	tracer, sr := newRecordingTracer(t)
	wl := &fakeAgentSandboxWorkload{name: "ping"}
	cfg := &userclass.Config{Tracer: tracer, Dyn: dynconfig.NewHolder(dynconfig.Config{})}
	b := &agentSandboxBackend{
		cfg:              cfg,
		asDeps:           AgentSandboxDeps{Sandboxes: newReadySandboxes(""), Extensions: newReadyExtensions(""), Namespace: testNamespace},
		templateName:     "missing",
		provisionTimeout: time.Second,
	}
	u := &runWorkloadUser{cfg: cfg, workload: wl, backend: b}

	u.measure(context.Background(), coldCreateMetric, Options{}, true, func(ctx context.Context) (actorRef, time.Duration, error) {
		return b.first(ctx, dynconfig.Config{})
	})

	if got := len(wl.recordedCalls()); got != 0 {
		t.Errorf("instructions sent after a failed provision: got %d, want 0", got)
	}
	parent := spanByName(t, sr, coldCreateMetric)
	if got := mustAttr(t, parent, "attempts").AsInt64(); got != 0 {
		t.Errorf("attempts: got %d, want 0", got)
	}
}

// fakeAgentSandboxWorkload targets the agent-sandbox backend and speaks no
// real protocol, mirroring fakeWorkload's role in workload_test.go for the
// substrate backend.
type fakeAgentSandboxWorkload struct {
	name string

	mu    sync.Mutex
	calls []actorRef
}

func (w *fakeAgentSandboxWorkload) Name() string            { return w.name }
func (w *fakeAgentSandboxWorkload) Backend() Backend        { return BackendAgentSandbox }
func (w *fakeAgentSandboxWorkload) DefaultTemplate() string { return "fake-template" }
func (w *fakeAgentSandboxWorkload) SendInstruction(_ context.Context, actor actorRef) (bool, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.calls = append(w.calls, actor)
	return false, nil
}
func (w *fakeAgentSandboxWorkload) recordedCalls() []actorRef {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]actorRef(nil), w.calls...)
}
