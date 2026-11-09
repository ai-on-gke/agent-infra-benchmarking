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

package sandbox

import (
	"context"
	"errors"
	"fmt"
	"syscall"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/measure"
	"go.ate.dev/substrate/internal/benchmarking/boomer/userclass"
	"go.opentelemetry.io/otel"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/watch"
	dynamicfake "k8s.io/client-go/dynamic/fake"
	k8stesting "k8s.io/client-go/testing"
)

func TestParseClaimStatus(t *testing.T) {
	// 1. Not ready claim
	notReady := &unstructured.Unstructured{
		Object: map[string]interface{}{
			"status": map[string]interface{}{
				"sandbox": map[string]interface{}{
					"name": "sb-1234",
				},
				"conditions": []interface{}{
					map[string]interface{}{
						"type":   "Ready",
						"status": "False",
					},
				},
			},
		},
	}
	if name, podIP, ready := parseClaimStatus(notReady); ready || name != "" || podIP != "" {
		t.Fatalf("expected not ready, got name=%q podIP=%q ready=%v", name, podIP, ready)
	}

	// 2. Ready claim
	readyClaim := &unstructured.Unstructured{
		Object: map[string]interface{}{
			"status": map[string]interface{}{
				"sandbox": map[string]interface{}{
					"name": "sb-1234",
					"podIPs": []interface{}{
						"10.0.0.1",
					},
				},
				"conditions": []interface{}{
					map[string]interface{}{
						"type":   "Ready",
						"status": "True",
					},
				},
			},
		},
	}
	name, podIP, ready := parseClaimStatus(readyClaim)
	if !ready || name != "sb-1234" || podIP != "10.0.0.1" {
		t.Fatalf("expected ready with sb-1234 and 10.0.0.1, got name=%q podIP=%q ready=%v", name, podIP, ready)
	}

	// 3. Nil/missing status
	empty := &unstructured.Unstructured{Object: map[string]interface{}{}}
	if name, podIP, ready := parseClaimStatus(empty); ready || name != "" || podIP != "" {
		t.Fatalf("expected not ready for empty claim, got name=%q podIP=%q ready=%v", name, podIP, ready)
	}
}

func TestClaimWarmSandboxLifecycle(t *testing.T) {
	s := wokenAgentSandbox(t, "glutton")

	err := s.Sleep(context.Background())
	if err != nil {
		t.Fatalf("Sleep failed: %v", err)
	}
	if s.isActive {
		t.Fatalf("expected s.isActive=false after release")
	}
}

// A claimed sandbox removed by a discard or the teardown, rather than put to
// sleep, still leaves the active gauge.
func TestAgentSandboxRemoveDropsActiveGauge(t *testing.T) {
	const wl = "gauge-test"
	s := wokenAgentSandbox(t, wl)
	if got := activeGauge(t, wl); got != 1 {
		t.Fatalf("active actors after Wake: got %v, want 1", got)
	}

	if err := s.Remove(context.Background()); err != nil {
		t.Fatalf("Remove failed: %v", err)
	}
	if got := activeGauge(t, wl); got != 0 {
		t.Errorf("active actors after Remove: got %v, want 0", got)
	}
}

// activeGauge is boomer_active_actors for workload, as Prometheus scrapes it.
func activeGauge(t *testing.T, workload string) float64 {
	t.Helper()
	families, err := prometheus.DefaultGatherer.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, f := range families {
		if f.GetName() != "boomer_active_actors" {
			continue
		}
		for _, m := range f.GetMetric() {
			for _, l := range m.GetLabel() {
				if l.GetName() == "workload" && l.GetValue() == workload {
					return m.GetGauge().GetValue()
				}
			}
		}
	}
	t.Fatalf("no boomer_active_actors{workload=%q}", workload)
	return 0
}

func TestAgentSandboxSpreadsActorsOverWarmPools(t *testing.T) {
	p := &AgentSandbox{WarmPools: []string{"pool-0", "pool-1", "pool-2"}}
	counts := map[string]int{}
	for i := 0; i < 9; i++ {
		sb, err := p.Create(context.Background())
		if err != nil {
			t.Fatalf("Create failed: %v", err)
		}
		counts[sb.(*agentSandbox).warmPool]++
	}
	for _, pool := range p.WarmPools {
		if counts[pool] != 3 {
			t.Fatalf("expected 3 actors on %s, got %v", pool, counts)
		}
	}
}

// wokenAgentSandbox is a sandbox whose Wake claimed a warm one from a fake
// controller, its gauges labelled workload.
func wokenAgentSandbox(t *testing.T, workload string) *agentSandbox {
	t.Helper()
	p, fakeDyn := fakeAgentSandbox(workload)
	return wake(t, p, fakeDyn, nil)
}

// fakeAgentSandbox is an AgentSandbox on a fake API server, its gauges
// labelled workload.
func fakeAgentSandbox(workload string) (*AgentSandbox, *dynamicfake.FakeDynamicClient) {
	scheme := runtime.NewScheme()
	fakeDyn := dynamicfake.NewSimpleDynamicClientWithCustomListKinds(scheme, map[schema.GroupVersionResource]string{
		sandboxClaimsGVR: "SandboxClaimList",
	})

	return &AgentSandbox{
		Config: &userclass.Config{Atespace: "benchmark-workloads"},
		Client: fakeDyn,
		Rec: measure.Recorder{
			Tracer:       otel.Tracer("test"),
			UserClass:    "RunWorkloadUser",
			Timeout:      5 * time.Second,
			WindowMethod: "runworkload",
		},
		Workload:  workload,
		WarmPools: []string{"glutton-warm-pool-0"},
	}, fakeDyn
}

// wake creates a sandbox on p and wakes it, the fake controller making its
// claim ready once it exists and, if ready is not nil, ready is closed.
func wake(t *testing.T, p *AgentSandbox, fakeDyn *dynamicfake.FakeDynamicClient, ready <-chan struct{}) *agentSandbox {
	t.Helper()
	ctx := context.Background()
	sb, err := p.Create(ctx)
	if err != nil {
		t.Fatalf("Create failed: %v", err)
	}
	s := sb.(*agentSandbox)

	go func() {
		if ready != nil {
			<-ready
		}
		for i := 0; i < 50; i++ {
			time.Sleep(20 * time.Millisecond)
			claims, err := fakeDyn.Resource(sandboxClaimsGVR).Namespace("benchmark-workloads").List(ctx, metav1.ListOptions{})
			if err == nil && len(claims.Items) > 0 {
				claim := &claims.Items[0]
				claim.Object["status"] = map[string]interface{}{
					"sandbox": map[string]interface{}{
						"name": "warm-sb-test",
					},
					"conditions": []interface{}{
						map[string]interface{}{
							"type":   "Ready",
							"status": "True",
						},
					},
				}
				_, _ = fakeDyn.Resource(sandboxClaimsGVR).Namespace("benchmark-workloads").Update(ctx, claim, metav1.UpdateOptions{})
				return
			}
		}
	}()

	err = s.Wake(ctx)
	if err != nil {
		t.Fatalf("Wake failed: %v", err)
	}
	if got := s.Actor().Name; got != "warm-sb-test" {
		t.Fatalf("expected actor name warm-sb-test, got %q", got)
	}
	if !s.isActive {
		t.Fatalf("expected s.isActive=true")
	}
	return s
}

// shortRetries makes the API retries back off in milliseconds.
func shortRetries(t *testing.T) {
	initial, maxDelay := measure.RetryInitialDelay, measure.RetryMaxDelay
	measure.RetryInitialDelay, measure.RetryMaxDelay = time.Millisecond, 4*time.Millisecond
	t.Cleanup(func() { measure.RetryInitialDelay, measure.RetryMaxDelay = initial, maxDelay })
}

var errRefused = fmt.Errorf("dial tcp 34.118.224.1:443: connect: %w", syscall.ECONNREFUSED)

func TestAgentSandboxWakeRetriesRefusedCreate(t *testing.T) {
	shortRetries(t)
	p, fakeDyn := fakeAgentSandbox("retry-create")
	refused := 0
	fakeDyn.PrependReactor("create", "sandboxclaims", func(k8stesting.Action) (bool, runtime.Object, error) {
		if refused < 2 {
			refused++
			return true, nil, errRefused
		}
		return false, nil, nil
	})

	wake(t, p, fakeDyn, nil)
	if refused != 2 {
		t.Errorf("refused creates: got %d, want 2", refused)
	}
}

// A create whose connection drops after the server took it is not made twice.
func TestAgentSandboxWakeTakesCreateThatLanded(t *testing.T) {
	shortRetries(t)
	p, fakeDyn := fakeAgentSandbox("landed-create")
	creates := 0
	fakeDyn.PrependReactor("create", "sandboxclaims", func(action k8stesting.Action) (bool, runtime.Object, error) {
		creates++
		if creates == 1 {
			obj := action.(k8stesting.CreateAction).GetObject()
			if err := fakeDyn.Tracker().Create(sandboxClaimsGVR, obj, action.GetNamespace()); err != nil {
				return true, nil, err
			}
			return true, nil, errors.New("http2: server sent GOAWAY and closed the connection; LastStreamID=7")
		}
		return false, nil, nil
	})

	wake(t, p, fakeDyn, nil)
	if creates != 2 {
		t.Errorf("creates: got %d, want 2", creates)
	}
}

func TestAgentSandboxWakeReopensClosedWatch(t *testing.T) {
	shortRetries(t)
	p, fakeDyn := fakeAgentSandbox("reopen-watch")
	watches := 0
	reopened := make(chan struct{})
	fakeDyn.PrependWatchReactor("sandboxclaims", func(action k8stesting.Action) (bool, watch.Interface, error) {
		watches++
		if watches == 1 {
			closed := watch.NewFake()
			closed.Stop()
			return true, closed, nil
		}
		w, err := fakeDyn.Tracker().Watch(sandboxClaimsGVR, action.GetNamespace())
		if watches == 2 {
			close(reopened)
		}
		return true, w, err
	})

	wake(t, p, fakeDyn, reopened)
	if watches != 2 {
		t.Errorf("watches: got %d, want 2", watches)
	}
}

func TestAgentSandboxRemoveRetriesRefusedDelete(t *testing.T) {
	shortRetries(t)
	s := wokenAgentSandbox(t, "retry-delete")
	fake := s.p.Client.(*dynamicfake.FakeDynamicClient)
	refused := false
	fake.PrependReactor("delete", "sandboxclaims", func(k8stesting.Action) (bool, runtime.Object, error) {
		if !refused {
			refused = true
			return true, nil, errRefused
		}
		return false, nil, nil
	})

	if err := s.Remove(context.Background()); err != nil {
		t.Fatalf("Remove failed: %v", err)
	}
	claims, err := s.claims().List(context.Background(), metav1.ListOptions{})
	if err != nil {
		t.Fatalf("List failed: %v", err)
	}
	if len(claims.Items) != 0 {
		t.Errorf("claims left: %d", len(claims.Items))
	}
}

func TestTransientAPIError(t *testing.T) {
	gr := sandboxClaimsGVR.GroupResource()
	for _, tc := range []struct {
		err  error
		want bool
	}{
		{errRefused, true},
		{errors.New("http2: server sent GOAWAY and closed the connection; LastStreamID=7"), true},
		{apierrors.NewServiceUnavailable("resizing"), true},
		{apierrors.NewTooManyRequests("busy", 1), true},
		{apierrors.NewNotFound(gr, "claim"), false},
		{apierrors.NewAlreadyExists(gr, "claim"), false},
		{apierrors.NewForbidden(gr, "claim", errors.New("denied")), false},
	} {
		if got := transientAPIError(tc.err); got != tc.want {
			t.Errorf("transientAPIError(%v): got %v, want %v", tc.err, got, tc.want)
		}
	}
}

// The router finds a claimed sandbox by these headers alone.
func TestAgentSandboxActorHeaders(t *testing.T) {
	s := &agentSandbox{
		p:     &AgentSandbox{Config: &userclass.Config{Atespace: "benchmark-workloads"}},
		name:  "warm-sb-test",
		podIP: "10.0.0.1",
	}
	h := s.Actor().Header
	for key, want := range map[string]string{
		"X-Sandbox-ID":        "warm-sb-test",
		"X-Sandbox-Port":      "80",
		"X-Sandbox-Namespace": "benchmark-workloads",
		"X-Sandbox-Pod-IP":    "10.0.0.1",
	} {
		if got := h.Get(key); got != want {
			t.Errorf("%s: got %q, want %q", key, got, want)
		}
	}
}
