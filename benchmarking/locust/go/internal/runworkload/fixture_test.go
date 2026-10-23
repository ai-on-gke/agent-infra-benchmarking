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
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/harness"
	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/scenario"
	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/workload"
	"github.com/agent-substrate/substrate/internal/ateinterceptors"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/userclass"
	"github.com/agent-substrate/substrate/pkg/proto/ateapipb"
	"go.opentelemetry.io/otel"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

var (
	// errResumeRefused stands in for a control plane that could not place the
	// actor on a worker.
	errResumeRefused = errors.New("no worker available")
	// errActorBusy stands in for a call refused because the actor is still in
	// a transitional state.
	errActorBusy = errors.New("actor is not SUSPENDED")
)

type fakeControlClient struct {
	ateapipb.ControlClient
	// Each, when set, fails every call of its name.
	resumeErr  error
	suspendErr error
	deleteErr  error
	egressErr  error
	// resumeErrTimes, when set, limits resumeErr to the first that many
	// ResumeActor calls, standing in for a conflict that clears.
	resumeErrTimes int
	resumeCalls    int
	// resumeHang holds each ResumeActor until its context ends.
	resumeHang bool
	// serverElapsedUs, when set, is returned as the x-server-elapsed-us
	// trailer the real ServerUnaryInterceptor emits.
	serverElapsedUs string
	// suspendDelay holds each SuspendActor open, so a test can tell whether a
	// window that should start after the suspend counted it.
	suspendDelay time.Duration
	// deleteDelay holds each DeleteActor open, so a teardown test can see how
	// many run at once.
	deleteDelay time.Duration

	mu sync.Mutex
	// inFlight and peak track overlapping DeleteActor calls.
	inFlight int
	peak     int
	// deleteAnyState is the AnyState flag from the most recent DeleteActor.
	deleteAnyState bool
	calls          []string
	// template and actor are the ActorTemplate and actor names from the last
	// CreateActor.
	template string
	actor    string
	// egressPolicy is the policy from the last CreateActorEgressPolicy.
	egressPolicy *ateapipb.EgressPolicy
}

// setTrailer fills the caller's grpc.Trailer destination so a faked call can
// carry the server's handler duration the way a real one does.
func (f *fakeControlClient) setTrailer(opts []grpc.CallOption) {
	if f.serverElapsedUs == "" {
		return
	}
	for _, o := range opts {
		if t, ok := o.(grpc.TrailerCallOption); ok && t.TrailerAddr != nil {
			*t.TrailerAddr = metadata.Pairs(ateinterceptors.ServerElapsedTrailer, f.serverElapsedUs)
		}
	}
}

func (f *fakeControlClient) CreateAtespace(ctx context.Context, in *ateapipb.CreateAtespaceRequest, opts ...grpc.CallOption) (*ateapipb.Atespace, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "CreateAtespace")
	return &ateapipb.Atespace{}, nil
}

func (f *fakeControlClient) CreateActor(ctx context.Context, in *ateapipb.CreateActorRequest, opts ...grpc.CallOption) (*ateapipb.Actor, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "CreateActor")
	f.template = in.GetActor().GetActorTemplate().GetName()
	f.actor = in.GetActor().GetMetadata().GetName()
	return &ateapipb.Actor{}, nil
}

func (f *fakeControlClient) CreateActorEgressPolicy(ctx context.Context, in *ateapipb.CreateActorEgressPolicyRequest, opts ...grpc.CallOption) (*ateapipb.EgressPolicy, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "CreateActorEgressPolicy")
	if f.egressErr != nil {
		return nil, f.egressErr
	}
	f.egressPolicy = in.GetEgressPolicy()
	return in.GetEgressPolicy(), nil
}

// recordedEgressPolicy returns the policy the most recent
// CreateActorEgressPolicy carried.
func (f *fakeControlClient) recordedEgressPolicy() *ateapipb.EgressPolicy {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.egressPolicy
}

// recordedTemplate returns the ActorTemplate the most recent CreateActor
// named, which is where a template choice stops being a local variable.
func (f *fakeControlClient) recordedTemplate() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.template
}

// recordedActor returns the name of the actor the most recent CreateActor made.
func (f *fakeControlClient) recordedActor() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.actor
}

func (f *fakeControlClient) ResumeActor(ctx context.Context, in *ateapipb.ResumeActorRequest, opts ...grpc.CallOption) (*ateapipb.ResumeActorResponse, error) {
	if f.resumeHang {
		<-ctx.Done()
		return nil, status.FromContextError(ctx.Err()).Err()
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "ResumeActor")
	f.setTrailer(opts)
	f.resumeCalls++
	if f.resumeErr != nil && (f.resumeErrTimes == 0 || f.resumeCalls <= f.resumeErrTimes) {
		return nil, f.resumeErr
	}
	return &ateapipb.ResumeActorResponse{}, nil
}

func (f *fakeControlClient) SuspendActor(ctx context.Context, in *ateapipb.SuspendActorRequest, opts ...grpc.CallOption) (*ateapipb.SuspendActorResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "SuspendActor")
	time.Sleep(f.suspendDelay)
	if f.suspendErr != nil {
		return nil, f.suspendErr
	}
	return &ateapipb.SuspendActorResponse{}, nil
}

func (f *fakeControlClient) DeleteActor(ctx context.Context, in *ateapipb.DeleteActorRequest, opts ...grpc.CallOption) (*ateapipb.Actor, error) {
	f.mu.Lock()
	f.calls = append(f.calls, "DeleteActor")
	f.deleteAnyState = in.GetAnyState()
	f.inFlight++
	f.peak = max(f.peak, f.inFlight)
	anyState, suspendErr, deleteErr := in.GetAnyState(), f.suspendErr, f.deleteErr
	f.mu.Unlock()

	// Outside the lock, so concurrent callers actually overlap here.
	time.Sleep(f.deleteDelay)

	f.mu.Lock()
	defer f.mu.Unlock()
	f.inFlight--
	// Mirrors ensureMarkedDeleting: a refused suspend leaves the actor in a
	// state only an any-state delete may remove.
	if suspendErr != nil && !anyState {
		return nil, errActorBusy
	}
	if deleteErr != nil {
		return nil, deleteErr
	}
	return &ateapipb.Actor{}, nil
}

// peakInFlight is the most DeleteActor calls that overlapped.
func (f *fakeControlClient) peakInFlight() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.peak
}

// deletedFromAnyState reports whether the most recent DeleteActor asked to
// bypass the state precondition.
func (f *fakeControlClient) deletedFromAnyState() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.deleteAnyState
}

func (f *fakeControlClient) recordedCalls() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...)
}

// countCalls is how many times the named RPC was made.
func (f *fakeControlClient) countCalls(name string) int {
	var n int
	for _, c := range f.recordedCalls() {
		if c == name {
			n++
		}
	}
	return n
}

// newTestConfig starts srv, sets HTTPClient and RouterURL, and ensures
// APIStub and Tracer are populated if nil.
func newTestConfig(t *testing.T, srv *pingServer, cfg *userclass.Config) *userclass.Config {
	t.Helper()
	ts := srv.Start(t)
	if cfg == nil {
		cfg = &userclass.Config{}
	}
	if cfg.APIStub == nil {
		cfg.APIStub = &fakeControlClient{}
	}
	if cfg.Tracer == nil {
		cfg.Tracer = otel.Tracer("test")
	}
	if cfg.Atespace == "" {
		cfg.Atespace = "benchmark"
	}
	cfg.HTTPClient = ts.Client()
	cfg.RouterURL = ts.URL
	return cfg
}

// newTestScenarioOn builds a glutton scenario on cfg for opts, its actors
// created on substrate.
func newTestScenarioOn(cfg *userclass.Config, opts scenario.Options) *runWorkloadScenario {
	wl, err := workload.Resolve(workload.GluttonName)
	if err != nil {
		panic(err)
	}
	return newTestScenarioRunning(cfg, opts, wl)
}

// newTestScenarioRunning builds a scenario on cfg for opts that runs wl.
func newTestScenarioRunning(cfg *userclass.Config, opts scenario.Options, wl workload.Workload) *runWorkloadScenario {
	opts.Config = cfg
	c := Config{Common: scenario.Common{Options: opts, Workload: wl}}
	provider, err := c.NewProvider(recorder(c), false)
	if err != nil {
		panic(err)
	}
	return &runWorkloadScenario{c: c, provider: provider}
}

// testRun is a scenario running on the harness, as New starts it.
type testRun struct {
	*runWorkloadScenario
	iterate  func()
	shutdown func(context.Context)
}

// newTestRunOn runs s on the harness, with no user started yet.
func newTestRunOn(s *runWorkloadScenario) *testRun {
	task, shutdown := harness.New(s, harness.Options{Name: Name, UserClass: UserClass})
	return &testRun{runWorkloadScenario: s, iterate: task, shutdown: shutdown}
}

// startsOver reports whether the next iteration on this goroutine creates a
// new actor, which it does only once the VU's slot has been freed.
func startsOver(rt *testRun, ctrl *fakeControlClient) bool {
	before := ctrl.countCalls("CreateActor")
	rt.iterate()
	return ctrl.countCalls("CreateActor") == before+1
}

// newTestUser starts a user on s, its actor created and still suspended.
func newTestUser(t *testing.T, s *runWorkloadScenario) *runWorkloadUser {
	t.Helper()
	sess, err := s.Start(context.Background())
	if err != nil {
		t.Fatalf("Start failed: %v", err)
	}
	return sess.(*runWorkloadUser)
}

func newTestRunWorkloadUser(t *testing.T, srv *pingServer, cfg *userclass.Config) *runWorkloadUser {
	t.Helper()
	return newTestUser(t, newTestScenarioOn(newTestConfig(t, srv, cfg), scenario.Options{}))
}
