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
	"net/http"
	"slices"
	"testing"
	"time"

	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/scenario"
	"go.ate.dev/substrate/api/ateapi/v1"
	"go.ate.dev/substrate/internal/benchmarking/boomer/userclass"
	gluttonpb "go.ate.dev/substrate/internal/proto/glutton"
)

// A cycle of several steps sends each step's instructions between its one
// resume and its one suspend.
func TestCycleSendsEveryStep(t *testing.T) {
	srv := &pingServer{}
	fake := &fakeControlClient{}
	u := newTestRunWorkloadUser(t, srv, &userclass.Config{APIStub: fake})
	u.steps, u.concurrent = 3, 2

	if call, err := u.cycle(context.Background(), scenario.ResumeModeExplicit); err != nil {
		t.Fatalf("cycle failed at %s: %v", call, err)
	}
	if got := srv.RecordedPings(); got != 6 {
		t.Errorf("pings: got %d, want 3 steps of 2", got)
	}
	if got := fake.countCalls("ResumeActor"); got != 1 {
		t.Errorf("ResumeActor calls: got %d, want 1", got)
	}
	if got := fake.countCalls("SuspendActor"); got != 1 {
		t.Errorf("SuspendActor calls: got %d, want 1", got)
	}
}

// A step's instructions are in flight together, not one after another.
func TestConcurrentInstructionsOverlap(t *testing.T) {
	srv := &pingServer{PingDelay: 100 * time.Millisecond}
	u := newTestRunWorkloadUser(t, srv, nil)
	u.concurrent = 4

	if err := u.executeStep(context.Background()); err != nil {
		t.Fatalf("executeStep failed: %v", err)
	}
	if got := srv.PeakPings(); got != 4 {
		t.Errorf("pings at once: got %d, want 4", got)
	}
}

// One failed instruction of a step fails the step, once all of them are done.
func TestConcurrentInstructionFailureFailsTheStep(t *testing.T) {
	srv := &pingServer{PingEcho: "not the nonce"}
	u := newTestRunWorkloadUser(t, srv, nil)
	u.concurrent = 3

	if err := u.executeStep(context.Background()); err == nil {
		t.Fatal("executeStep succeeded with wrong echoes, want failure")
	}
	if got := srv.RecordedPings(); got != 3 {
		t.Errorf("pings: got %d, want all 3 sent", got)
	}
}

// A glutton cycle with a load writes the actor's memory and then its disk
// after the instruction, and suspends after both.
func TestCycleLoadsGluttonBeforeSuspend(t *testing.T) {
	srv := &pingServer{}
	fake := &fakeControlClient{}
	u := newTestRunWorkloadUser(t, srv, &userclass.Config{APIStub: fake})
	u.writeRAM, u.writeDisk = "1Mi", 4096

	if call, err := u.cycle(context.Background(), scenario.ResumeModeExplicit); err != nil {
		t.Fatalf("cycle failed at %s: %v", call, err)
	}
	if got, want := srv.RecordedWrites(), []string{writeRAMPath, writeDiskPath}; !slices.Equal(got, want) {
		t.Errorf("writes: got %v, want %v", got, want)
	}
	if got := fake.countCalls("SuspendActor"); got != 1 {
		t.Errorf("SuspendActor calls: got %d, want 1", got)
	}
}

// An actor's first memory write fills the array, which the later ones rotate
// through.
func TestFirstRAMWriteFillsLaterOnesRotate(t *testing.T) {
	srv := &pingServer{}
	u := newTestRunWorkloadUser(t, srv, nil)
	u.writeRAM = "1Mi"

	for i := range 3 {
		if call, err := u.cycle(context.Background(), scenario.ResumeModeExplicit); err != nil {
			t.Fatalf("cycle %d failed at %s: %v", i, call, err)
		}
	}
	want := []gluttonpb.WriteMode{
		gluttonpb.WriteMode_WRITE_MODE_TRUNCATE,
		gluttonpb.WriteMode_WRITE_MODE_OVERWRITE_ROTATE,
		gluttonpb.WriteMode_WRITE_MODE_OVERWRITE_ROTATE,
	}
	if got := srv.RecordedRAMModes(); !slices.Equal(got, want) {
		t.Errorf("modes: got %v, want %v", got, want)
	}
}

// A failed load fails the cycle at that call, before the suspend, for the
// actor to be discarded.
func TestFailedLoadFailsTheCycle(t *testing.T) {
	srv := &pingServer{LoadStatus: http.StatusInternalServerError}
	fake := &fakeControlClient{}
	u := newTestRunWorkloadUser(t, srv, &userclass.Config{APIStub: fake})
	u.writeRAM = "1Mi"

	call, err := u.cycle(context.Background(), scenario.ResumeModeExplicit)
	if err == nil {
		t.Fatal("cycle succeeded with a failed WriteRAM, want failure")
	}
	if call != writeRAMMetric {
		t.Errorf("failed call: got %q, want %q", call, writeRAMMetric)
	}
	if got := fake.countCalls("SuspendActor"); got != 0 {
		t.Errorf("SuspendActor calls: got %d, want none after the failed load", got)
	}
}

// Users are spread over the connections in turn.
func TestUsersAreSpreadOverConnections(t *testing.T) {
	stubs := []*fakeControlClient{{}, {}, {}}
	cfg := newTestConfig(t, &pingServer{}, nil)
	sc := newTestScenarioOn(cfg, scenario.Options{APIStubs: []ateapi.ControlClient{stubs[0], stubs[1], stubs[2]}})
	for i, want := range []int{0, 1, 2, 0} {
		before := stubs[want].countCalls("CreateActor")
		newTestUser(t, sc)
		if got := stubs[want].countCalls("CreateActor") - before; got != 1 {
			t.Errorf("user %d: its actor was not created over connection %d", i, want)
		}
	}
}
