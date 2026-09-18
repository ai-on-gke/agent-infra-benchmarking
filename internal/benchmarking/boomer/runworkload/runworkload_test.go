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

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"

	"github.com/ai-on-gke/agent-infra-benchmarking/internal/benchmarking/boomer/dynconfig"
	"github.com/ai-on-gke/agent-infra-benchmarking/internal/benchmarking/boomer/userclass"
)

const testNamespace = "benchmarking"

// newReadySandboxes returns a fake core clientset where every Sandbox
// Create() is captured, and every subsequent Get() on that name reports it
// Ready with fqdn regardless of what a real controller would do — there is
// no controller here to flip status, so the test stands in for one.
func newReadySandboxes(fqdn string) *sandboxfake.Clientset {
	cs := sandboxfake.NewSimpleClientset()
	var mu sync.Mutex
	specs := map[string]sandboxv1beta1.SandboxSpec{}

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
// subsequent Get() reports it Ready with fqdn — the same stand-in as
// newReadySandboxes, for the pool-claim path.
func newReadyExtensions(fqdn string, templates ...*extensionsv1beta1.SandboxTemplate) *extensionsfake.Clientset {
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
				SandboxStatus: extensionsv1beta1.SandboxStatus{ServiceFQDN: fqdn},
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

func newRecordingTracer(t *testing.T) (trace.Tracer, *tracetest.SpanRecorder) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	return tp.Tracer("runworkload-test"), sr
}

func spanByName(t *testing.T, sr *tracetest.SpanRecorder, name string) sdktrace.ReadOnlySpan {
	t.Helper()
	for _, s := range sr.Ended() {
		if s.Name() == name {
			return s
		}
	}
	t.Fatalf("no span named %q; recorded %v", name, spanNames(sr))
	return nil
}

func spanNames(sr *tracetest.SpanRecorder) []string {
	var names []string
	for _, s := range sr.Ended() {
		names = append(names, s.Name())
	}
	return names
}

func mustAttr(t *testing.T, s sdktrace.ReadOnlySpan, key string) attribute.Value {
	t.Helper()
	for _, kv := range s.Attributes() {
		if string(kv.Key) == key {
			return kv.Value
		}
	}
	t.Fatalf("span %q missing attribute %q", s.Name(), key)
	return attribute.Value{}
}

func TestCreateColdBuildsSandboxFromTemplateBlueprint(t *testing.T) {
	tracer, _ := newRecordingTracer(t)
	extensions := newReadyExtensions("", templateWithImage("ping", "ping-probe:v1"))
	sandboxes := newReadySandboxes("sb-cold-x.benchmarking.svc.cluster.local")

	u := &runWorkloadUser{
		cfg: &userclass.Config{
			Sandboxes:  sandboxes,
			Extensions: extensions,
			Namespace:  testNamespace,
			Tracer:     tracer,
			Dyn:        dynconfig.NewHolder(dynconfig.Config{}),
		},
		workload:         &fakeWorkload{name: "fake", template: "ping"},
		templateName:     "ping",
		warmPoolName:     "ping-warmpool",
		userClass:        runWorkloadUserClass,
		timeout:          time.Second,
		provisionTimeout: 5 * time.Second,
	}

	target, err := u.doCreateCold(context.Background())
	if err != nil {
		t.Fatalf("doCreateCold failed: %v", err)
	}
	if target.ServiceFQDN != "sb-cold-x.benchmarking.svc.cluster.local" {
		t.Errorf("ServiceFQDN: got %q", target.ServiceFQDN)
	}
	if target.Port != probePort {
		t.Errorf("Port: got %d, want %d", target.Port, probePort)
	}
	if u.usingPool || u.name == "" {
		t.Fatalf("user state after doCreateCold: name=%q usingPool=%v", u.name, u.usingPool)
	}

	got, err := sandboxes.AgentsV1beta1().Sandboxes(testNamespace).Get(context.Background(), u.name, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get created Sandbox: %v", err)
	}
	if len(got.Spec.PodTemplate.Spec.Containers) != 1 || got.Spec.PodTemplate.Spec.Containers[0].Image != "ping-probe:v1" {
		t.Errorf("created Sandbox did not carry the template's blueprint: %+v", got.Spec.PodTemplate.Spec)
	}
}

func TestCreateColdFailsWhenTemplateIsMissing(t *testing.T) {
	tracer, _ := newRecordingTracer(t)
	u := &runWorkloadUser{
		cfg: &userclass.Config{
			Sandboxes:  newReadySandboxes(""),
			Extensions: newReadyExtensions(""), // no templates seeded
			Namespace:  testNamespace,
			Tracer:     tracer,
			Dyn:        dynconfig.NewHolder(dynconfig.Config{}),
		},
		templateName:     "does-not-exist",
		userClass:        runWorkloadUserClass,
		provisionTimeout: time.Second,
	}

	_, err := u.doCreateCold(context.Background())
	if err == nil {
		t.Fatal("doCreateCold succeeded for a missing template, want error")
	}
	if !apierrors.IsNotFound(err) {
		t.Errorf("expected a NotFound error, got: %v", err)
	}
}

func TestClaimFromPoolReferencesTheWarmPool(t *testing.T) {
	tracer, _ := newRecordingTracer(t)
	extensions := newReadyExtensions("sb-pool-x.benchmarking.svc.cluster.local")

	u := &runWorkloadUser{
		cfg: &userclass.Config{
			Extensions: extensions,
			Namespace:  testNamespace,
			Tracer:     tracer,
			Dyn:        dynconfig.NewHolder(dynconfig.Config{}),
		},
		templateName:     "ping",
		warmPoolName:     "ping-warmpool",
		userClass:        runWorkloadUserClass,
		provisionTimeout: 5 * time.Second,
	}

	target, err := u.doClaimFromPool(context.Background())
	if err != nil {
		t.Fatalf("doClaimFromPool failed: %v", err)
	}
	if target.ServiceFQDN != "sb-pool-x.benchmarking.svc.cluster.local" {
		t.Errorf("ServiceFQDN: got %q", target.ServiceFQDN)
	}
	if !u.usingPool || u.name == "" {
		t.Fatalf("user state after doClaimFromPool: name=%q usingPool=%v", u.name, u.usingPool)
	}

	got, err := extensions.ExtensionsV1beta1().SandboxClaims(testNamespace).Get(context.Background(), u.name, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get created SandboxClaim: %v", err)
	}
	if got.Spec.WarmPoolRef.Name != "ping-warmpool" {
		t.Errorf("WarmPoolRef: got %q, want %q", got.Spec.WarmPoolRef.Name, "ping-warmpool")
	}
}

// The measurement is the parent span's duration and the phase split lives on
// its attributes, so one sampled trace has to explain its own total without
// the reader joining it to anything else.
func TestMeasureSpanCarriesTheBreakdown(t *testing.T) {
	tracer, sr := newRecordingTracer(t)
	sandboxes := newReadySandboxes("sb-cold-x.benchmarking.svc.cluster.local")
	extensions := newReadyExtensions("", templateWithImage("ping", "ping-probe:v1"))
	wl := &fakeWorkload{name: "ping", template: "ping"}

	u := &runWorkloadUser{
		cfg: &userclass.Config{
			Sandboxes:  sandboxes,
			Extensions: extensions,
			Namespace:  testNamespace,
			Tracer:     tracer,
			Dyn:        dynconfig.NewHolder(dynconfig.Config{}),
		},
		workload:         wl,
		templateName:     "ping",
		userClass:        runWorkloadUserClass,
		timeout:          5 * time.Second,
		provisionTimeout: 5 * time.Second,
	}

	u.measure(context.Background(), coldCreateMetric, u.createCold)

	parent := spanByName(t, sr, coldCreateMetric)
	if got := mustAttr(t, parent, "template").AsString(); got != "ping" {
		t.Errorf("template: got %q, want %q", got, "ping")
	}
	if got := mustAttr(t, parent, "attempts").AsInt64(); got != 1 {
		t.Errorf("attempts: got %d, want 1", got)
	}
	if got := mustAttr(t, parent, "total_ms").AsFloat64(); got <= 0 {
		t.Errorf("total_ms: got %v, want > 0", got)
	}
	if got := mustAttr(t, parent, "provision_ms").AsFloat64(); got <= 0 {
		t.Errorf("provision_ms: got %v, want > 0", got)
	}
	if got := mustAttr(t, parent, "instruction_ms").AsFloat64(); got < 0 {
		t.Errorf("instruction_ms: got %v, want >= 0", got)
	}

	// Both phases must hang off the window parent; a detached child would put
	// the phase in a different trace from the total it belongs to.
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

func TestMeasureRecordsFailureWithoutSendingInstructions(t *testing.T) {
	// Provisioning that never succeeds must not reach the workload at all: a
	// row built from instructions that were never sent would be a lie.
	tracer, sr := newRecordingTracer(t)
	wl := &fakeWorkload{name: "ping", template: "missing"}
	u := &runWorkloadUser{
		cfg: &userclass.Config{
			Sandboxes:  newReadySandboxes(""),
			Extensions: newReadyExtensions(""), // no "missing" template seeded
			Namespace:  testNamespace,
			Tracer:     tracer,
			Dyn:        dynconfig.NewHolder(dynconfig.Config{}),
		},
		workload:         wl,
		templateName:     "missing",
		userClass:        runWorkloadUserClass,
		timeout:          time.Second,
		provisionTimeout: time.Second,
	}

	u.measure(context.Background(), coldCreateMetric, u.createCold)

	if got := len(wl.recordedCalls()); got != 0 {
		t.Errorf("instructions sent after a failed provision: got %d, want 0", got)
	}
	parent := spanByName(t, sr, coldCreateMetric)
	if got := mustAttr(t, parent, "attempts").AsInt64(); got != 0 {
		t.Errorf("attempts: got %d, want 0", got)
	}
}

func TestTeardownDeletesTheRightObjectType(t *testing.T) {
	t.Run("cold Sandbox", func(t *testing.T) {
		tracer, _ := newRecordingTracer(t)
		sandboxes := newReadySandboxes("fqdn")
		u := &runWorkloadUser{
			cfg: &userclass.Config{
				Sandboxes:  sandboxes,
				Extensions: newReadyExtensions("", templateWithImage("ping", "img")),
				Namespace:  testNamespace,
				Tracer:     tracer,
				Dyn:        dynconfig.NewHolder(dynconfig.Config{}),
			},
			templateName:     "ping",
			userClass:        runWorkloadUserClass,
			provisionTimeout: 5 * time.Second,
		}
		if _, err := u.doCreateCold(context.Background()); err != nil {
			t.Fatalf("doCreateCold failed: %v", err)
		}
		name := u.name

		u.teardown(context.Background())

		if u.name != "" {
			t.Errorf("user.name after teardown: got %q, want empty", u.name)
		}
		if _, err := sandboxes.AgentsV1beta1().Sandboxes(testNamespace).Get(context.Background(), name, metav1.GetOptions{}); !apierrors.IsNotFound(err) {
			t.Errorf("Sandbox %s still exists after teardown (err=%v)", name, err)
		}
	})

	t.Run("pool SandboxClaim", func(t *testing.T) {
		tracer, _ := newRecordingTracer(t)
		extensions := newReadyExtensions("fqdn")
		u := &runWorkloadUser{
			cfg: &userclass.Config{
				Extensions: extensions,
				Namespace:  testNamespace,
				Tracer:     tracer,
				Dyn:        dynconfig.NewHolder(dynconfig.Config{}),
			},
			warmPoolName:     "ping-warmpool",
			userClass:        runWorkloadUserClass,
			provisionTimeout: 5 * time.Second,
		}
		if _, err := u.doClaimFromPool(context.Background()); err != nil {
			t.Fatalf("doClaimFromPool failed: %v", err)
		}
		name := u.name

		u.teardown(context.Background())

		if u.name != "" {
			t.Errorf("user.name after teardown: got %q, want empty", u.name)
		}
		if _, err := extensions.ExtensionsV1beta1().SandboxClaims(testNamespace).Get(context.Background(), name, metav1.GetOptions{}); !apierrors.IsNotFound(err) {
			t.Errorf("SandboxClaim %s still exists after teardown (err=%v)", name, err)
		}
	})
}

func TestUserClassIsRegistered(t *testing.T) {
	entry, ok := userclass.Lookup("runworkload")
	if !ok {
		t.Fatalf("runworkload user class not registered; have %v", userclass.Names())
	}
	// The master's spawn messages match on the Python class name, so a
	// mismatch here starts no users at all.
	if entry.UserClass != runWorkloadUserClass {
		t.Errorf("UserClass: got %q, want %q", entry.UserClass, runWorkloadUserClass)
	}
	if entry.LocustFile != "runworkload.py" {
		t.Errorf("LocustFile: got %q, want %q", entry.LocustFile, "runworkload.py")
	}
}

func TestResolveTimeout(t *testing.T) {
	if got := resolveTimeout(0, DefaultTimeout); got != DefaultTimeout {
		t.Errorf("unset: got %v, want %v", got, DefaultTimeout)
	}
	if got := resolveTimeout(5*time.Second, DefaultTimeout); got != 5*time.Second {
		t.Errorf("configured: got %v, want 5s", got)
	}
}
