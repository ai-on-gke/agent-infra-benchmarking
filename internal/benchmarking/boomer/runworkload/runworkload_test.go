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
	"bytes"
	"cmp"
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/boomerutil"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/userclass"
	"github.com/prometheus/client_golang/prometheus"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// captureLogs points slog at a buffer for the duration of one test.
func captureLogs(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return &buf
}

// newTestRuntime builds a runtime around ctrl, with no user registered yet.
func newTestRuntime(t *testing.T, ctrl *fakeControlClient) *runWorkloadRuntime {
	t.Helper()
	cfg := newTestConfig(t, &pingServer{}, &userclass.Config{APIStub: ctrl})
	return &runWorkloadRuntime{cfg: cfg, workload: gluttonWorkload{}}
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

// The first iteration on a goroutine creates the actor; every later one
// resumes it and suspends it again, so the run settles into resume/suspend
// rather than creating an actor per iteration, and the actor ends every
// iteration suspended.
func TestIterateCreatesOnceThenCycles(t *testing.T) {
	ctrl := &fakeControlClient{}
	rt := newTestRuntime(t, ctrl)

	rt.iterate()
	if got := ctrl.countCalls("CreateActor"); got != 1 {
		t.Fatalf("CreateActor after the first iteration: got %d, want 1", got)
	}
	if got := ctrl.countCalls("SuspendActor"); got != 0 {
		t.Errorf("SuspendActor during the creating iteration: got %d, want 0", got)
	}

	rt.iterate()
	rt.iterate()

	want := []string{
		"CreateAtespace", "CreateActor",
		"ResumeActor", "SuspendActor",
		"ResumeActor", "SuspendActor",
	}
	if got := ctrl.recordedCalls(); !slices.Equal(got, want) {
		t.Errorf("calls after three iterations:\n got  %v\n want %v", got, want)
	}
}

// boomer.Run returns while iterations are still in flight, so the teardown
// drains the task function first and the gate stays shut afterwards. An
// iteration admitted past that point would create an actor the walk has
// already passed, which is how one leaks.
func TestIterateIsGatedByTheTeardownDrain(t *testing.T) {
	ctrl := &fakeControlClient{}
	rt := newTestRuntime(t, ctrl)

	rt.shutdown(context.Background())
	rt.iterate()

	if got := ctrl.countCalls("CreateActor"); got != 0 {
		t.Errorf("CreateActor after the teardown drained: got %d, want 0", got)
	}
}

// newRecordingTracer returns a tracer whose ended spans land in the returned
// recorder, so a test can assert on the window a measurement builds.
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

// newMeasuringUser builds a user whose spans are recorded, with an actor
// already registered on this goroutine so iterate goes down the cycling path.
func newMeasuringUser(t *testing.T, srv *pingServer, ctrl *fakeControlClient) (*runWorkloadRuntime, *tracetest.SpanRecorder) {
	t.Helper()
	tracer, sr := newRecordingTracer(t)
	cfg := newTestConfig(t, srv, &userclass.Config{APIStub: ctrl, Tracer: tracer})
	rt := &runWorkloadRuntime{cfg: cfg, workload: gluttonWorkload{}}
	rt.users.Store(boomerutil.GoroutineID(), &runWorkloadUser{
		cfg:          cfg,
		workload:     gluttonWorkload{},
		actorName:    "sb-measuring",
		templateName: gluttonWorkload{}.DefaultTemplate(),
		userClass:    runWorkloadUserClass,
	})
	return rt, sr
}

// Explicit mode wakes the actor itself; implicit leaves it to the router,
// which does it while holding the instruction, so a ResumeActor there would
// measure a path no client takes.
func TestResumeModeSelectsTheControlPlaneCall(t *testing.T) {
	tests := []struct {
		name        string
		resumeMode  string
		wantResumes int
	}{
		{
			name:        "explicit resume mode",
			resumeMode:  ResumeModeExplicit,
			wantResumes: 1,
		},
		{
			name:       "implicit resume mode",
			resumeMode: ResumeModeImplicit,
		},
		{
			// Same default as common/resume_mode.py, so an unset value behaves
			// like the one locust always sends.
			name:        "unset defaults to explicit",
			resumeMode:  "",
			wantResumes: 1,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			ctrl := &fakeControlClient{}
			rt, sr := newMeasuringUser(t, &pingServer{}, ctrl)
			rt.opts.ResumeMode = tc.resumeMode

			rt.iterate()

			if got := ctrl.countCalls("ResumeActor"); got != tc.wantResumes {
				t.Errorf("ResumeActor: got %d, want %d", got, tc.wantResumes)
			}
			// The instruction goes out either way; only the wake differs.
			span := spanByName(t, sr, ttfiMetric)
			if got := mustAttr(t, span, "resume_mode").AsString(); got != cmp.Or(tc.resumeMode, ResumeModeExplicit) {
				t.Errorf("resume_mode: got %q, want the effective mode", got)
			}
		})
	}
}

// The suspend that ends the cycle comes after the window has closed, so
// however long it took must not appear in TTFI.
func TestTTFIExcludesTheSuspend(t *testing.T) {
	const suspend = 200 * time.Millisecond
	rt, sr := newMeasuringUser(t, &pingServer{}, &fakeControlClient{suspendDelay: suspend})

	rt.iterate()

	total := mustAttr(t, spanByName(t, sr, ttfiMetric), "total_ms").AsFloat64()
	if want := boomerutil.MsFloat(suspend); total >= want {
		t.Errorf("total_ms: got %v, want well under %v — the suspend landed inside the window", total, want)
	}
}

// The window closes where the instruction is issued, so serving it is outside.
// What it left out is not lost: the ExecutionTime span spans that round trip.
func TestTTFIEndsBeforeTheInstruction(t *testing.T) {
	const service = 200 * time.Millisecond
	rt, sr := newMeasuringUser(t, &pingServer{PingDelay: service}, &fakeControlClient{})

	rt.iterate()

	total := mustAttr(t, spanByName(t, sr, ttfiMetric), "total_ms").AsFloat64()
	if want := boomerutil.MsFloat(service); total >= want {
		t.Errorf("total_ms: got %v, want well under %v — the window kept the instruction's service time", total, want)
	}
	instruction := spanByName(t, sr, executionTimeMetric)
	if got := instruction.EndTime().Sub(instruction.StartTime()); got < service {
		t.Errorf("%s span duration: got %v, want >= %v", executionTimeMetric, got, service)
	}
}

// CycleTime runs from the resume to the suspend returning, so unlike TTFI it
// holds both the instruction's service time and the suspend, and its span is
// the one the suspend is traced under.
func TestCycleTimeSpansTheResumeThroughTheSuspend(t *testing.T) {
	const service, suspend = 100 * time.Millisecond, 100 * time.Millisecond
	rt, sr := newMeasuringUser(t, &pingServer{PingDelay: service}, &fakeControlClient{suspendDelay: suspend})

	rt.iterate()

	cycle := spanByName(t, sr, cycleTimeMetric)
	if total, want := mustAttr(t, cycle, "total_ms").AsFloat64(), boomerutil.MsFloat(service+suspend); total < want {
		t.Errorf("total_ms: got %v, want >= %v", total, want)
	}
	for _, name := range []string{ttfiMetric, suspendMetric} {
		if got := spanByName(t, sr, name).Parent().SpanID(); got != cycle.SpanContext().SpanID() {
			t.Errorf("%s span is not a child of the %s span", name, cycleTimeMetric)
		}
	}
}

// A refused suspend still reports the cycle, as a failure over the time it
// spent, and the actor is discarded like after any other failed call.
func TestAFailedSuspendReportsAFailedCycle(t *testing.T) {
	ctrl := &fakeControlClient{suspendErr: errActorBusy}
	rt, sr := newMeasuringUser(t, &pingServer{}, ctrl)

	rt.iterate()

	if got := mustAttr(t, spanByName(t, sr, cycleTimeMetric), "total_ms").AsFloat64(); got <= 0 {
		t.Errorf("total_ms: got %v, want > 0", got)
	}
	if _, ok := rt.users.Load(boomerutil.GoroutineID()); ok {
		t.Error("the user kept its slot after the suspend was refused")
	}
}

// The instruction is sent once. A refused one is a fault to surface, not a
// state to wait out, so nothing retries it.
func TestTheInstructionIsSentOnce(t *testing.T) {
	srv := &pingServer{PingFailures: 1}
	rt, _ := newMeasuringUser(t, srv, &fakeControlClient{})

	rt.iterate()

	if got := srv.RecordedPings(); got != 1 {
		t.Errorf("instructions sent: got %d, want 1", got)
	}
}

// A refused instruction ends the cycle the same way any other failed call
// does: nothing is reported and the actor is discarded.
func TestARefusedInstructionDiscardsTheActor(t *testing.T) {
	ctrl := &fakeControlClient{}
	rt, sr := newMeasuringUser(t, &pingServer{PingFailures: 1}, ctrl)
	buf := captureLogs(t)

	rt.iterate()

	if _, ok := rt.users.Load(boomerutil.GoroutineID()); ok {
		t.Error("the user kept its slot after the instruction was refused")
	}
	if got := ctrl.countCalls("DeleteActor"); got != 1 {
		t.Errorf("DeleteActor: got %d, want 1", got)
	}
	if !strings.Contains(buf.String(), "discarding the actor") {
		t.Errorf("no discard was logged; got:\n%s", buf.String())
	}
	// The window is still reported, as a failure over the wait it spent.
	if got := mustAttr(t, spanByName(t, sr, ttfiMetric), "total_ms").AsFloat64(); got <= 0 {
		t.Errorf("total_ms: got %v, want > 0", got)
	}
}

// A refused resume never gave the actor a worker, so no instruction is sent —
// but the window it opened is still reported, as a failure.
func TestAFailedResumeReportsAFailedWindow(t *testing.T) {
	srv := &pingServer{}
	rt, sr := newMeasuringUser(t, srv, &fakeControlClient{resumeErr: errResumeRefused})

	rt.iterate()

	if got := srv.RecordedPings(); got != 0 {
		t.Errorf("instructions after a failed resume: got %d, want 0", got)
	}
	if got := mustAttr(t, spanByName(t, sr, ttfiMetric), "total_ms").AsFloat64(); got <= 0 {
		t.Errorf("total_ms: got %v, want > 0", got)
	}
}

// A failed call leaves the actor in a state nothing reconciles, so it is
// deleted and the goroutine's slot freed. The next iteration builds a new
// actor instead of working against the broken one for the rest of the run.
func TestAFailedCallDiscardsTheActor(t *testing.T) {
	tests := []struct {
		name string
		ctrl *fakeControlClient
	}{
		{
			name: "a refused suspend",
			ctrl: &fakeControlClient{suspendErr: errActorBusy},
		},
		{
			name: "a refused resume",
			ctrl: &fakeControlClient{resumeErr: errResumeRefused},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rt := newTestRuntime(t, tc.ctrl)
			buf := captureLogs(t)

			rt.iterate() // creates
			rt.iterate() // fails, discards

			if got := tc.ctrl.countCalls("DeleteActor"); got != 1 {
				t.Errorf("DeleteActor: got %d, want 1", got)
			}
			if _, ok := rt.users.Load(boomerutil.GoroutineID()); ok {
				t.Error("the discarded user still holds its goroutine slot")
			}
			if !strings.Contains(buf.String(), "discarding the actor") {
				t.Errorf("no discard was logged; got:\n%s", buf.String())
			}

			// The slot being free is what lets the next iteration start over.
			rt.iterate()
			if got := tc.ctrl.countCalls("CreateActor"); got != 2 {
				t.Errorf("CreateActor after the discard: got %d, want 2", got)
			}
		})
	}
}

// The control plane only deletes a SUSPENDED or CRASHED actor unless the
// request asks for any state. A refused suspend is exactly the case that
// leaves the actor outside those two, so that delete asks — and a suspend
// that worked does not, keeping the precondition as a check on the rest.
func TestDeleteBypassesTheStateCheckOnlyAfterAFailedSuspend(t *testing.T) {
	tests := []struct {
		name         string
		ctrl         *fakeControlClient
		wantAnyState bool
	}{
		{
			name: "the suspend worked",
			ctrl: &fakeControlClient{},
		},
		{
			name:         "the suspend was refused",
			ctrl:         &fakeControlClient{suspendErr: errActorBusy},
			wantAnyState: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			u := newTestRunWorkloadUser(t, &pingServer{}, &userclass.Config{APIStub: tc.ctrl})

			// The fake refuses a plain delete after a refused suspend, as the
			// control plane does, so the actor goes only if the flag is set.
			if err := u.suspendAndDelete(context.Background()); err != nil {
				t.Fatalf("suspendAndDelete failed: %v", err)
			}
			if got := tc.ctrl.deletedFromAnyState(); got != tc.wantAnyState {
				t.Errorf("AnyState: got %t, want %t", got, tc.wantAnyState)
			}
		})
	}
}

// The slot has to be freed even when the actor could not be removed, or the
// VU keeps using an actor it has already given up on. The delete failing is
// named instead, because that actor holds a worker until someone removes it.
func TestADiscardThatCouldNotDeleteStillFreesTheSlot(t *testing.T) {
	ctrl := &fakeControlClient{suspendErr: errActorBusy, deleteErr: errActorBusy}
	rt := newTestRuntime(t, ctrl)
	buf := captureLogs(t)

	rt.iterate()
	rt.iterate()

	if _, ok := rt.users.Load(boomerutil.GoroutineID()); ok {
		t.Error("the slot was kept after a failed delete")
	}
	if !strings.Contains(buf.String(), "discarded actor was left behind") {
		t.Errorf("a leaked actor was not named; got:\n%s", buf.String())
	}
}

// suspend reports whether the actor actually went down, and says why when it
// did not: tracedCall records the row but logs only a sampled trace.
func TestSuspendReportsWhetherItWent(t *testing.T) {
	tests := []struct {
		name    string
		ctrl    *fakeControlClient
		wantErr bool
	}{
		{
			name: "a completed suspend",
			ctrl: &fakeControlClient{},
		},
		{
			name:    "a suspend the control plane refused",
			ctrl:    &fakeControlClient{suspendErr: errActorBusy},
			wantErr: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			u := newTestRunWorkloadUser(t, &pingServer{}, &userclass.Config{APIStub: tc.ctrl})
			buf := captureLogs(t)

			err := u.suspend(context.Background())

			if tc.wantErr && err == nil {
				t.Fatal("suspend returned nil, want the refusal")
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("suspend failed: %v", err)
			}
			logged := strings.Contains(buf.String(), "suspend failed")
			if logged != tc.wantErr {
				t.Errorf("logged a failure: got %t, want %t; log:\n%s", logged, tc.wantErr, buf.String())
			}
			if tc.wantErr && !strings.Contains(buf.String(), errActorBusy.Error()) {
				t.Errorf("log does not name the reason; got:\n%s", buf.String())
			}
		})
	}
}

// An actor that outlives the run is invisible until someone goes looking for
// it, so teardown has to say which ones it could not remove and why.
func TestShutdownReportsWhatItLeftBehind(t *testing.T) {
	tests := []struct {
		name      string
		ctrl      *fakeControlClient
		wantInLog []string
	}{
		{
			name:      "a clean teardown",
			ctrl:      &fakeControlClient{},
			wantInLog: []string{"teardown removed an actor", `"left":0`, `"removed":1`},
		},
		{
			name: "an actor still in a transitional state",
			ctrl: &fakeControlClient{suspendErr: errActorBusy, deleteErr: errActorBusy},
			// The suspend failure rides along, because the delete failing on
			// its own means something else entirely.
			wantInLog: []string{"teardown left an actor behind", "after suspend", `"left":1`},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			u := newTestRunWorkloadUser(t, &pingServer{}, &userclass.Config{APIStub: tc.ctrl})
			rt := &runWorkloadRuntime{cfg: u.cfg, workload: u.workload}
			rt.users.Store(int64(1), u)
			buf := captureLogs(t)

			rt.shutdown(context.Background())

			got := buf.String()
			if !strings.Contains(got, u.actorName) {
				t.Errorf("teardown log does not name the actor %q; got:\n%s", u.actorName, got)
			}
			for _, want := range tc.wantInLog {
				if !strings.Contains(got, want) {
					t.Errorf("teardown log missing %q; got:\n%s", want, got)
				}
			}
		})
	}
}

// A run's worth of actors removed one at a time would outlast the budget the
// caller allows, so they go together — and the cap is what keeps that from
// becoming a burst of its own against an ateapi still draining the run.
func TestShutdownRemovesActorsConcurrently(t *testing.T) {
	const users = 50

	ctrl := &fakeControlClient{deleteDelay: 10 * time.Millisecond}
	cfg := newTestConfig(t, &pingServer{}, &userclass.Config{APIStub: ctrl})
	rt := &runWorkloadRuntime{cfg: cfg, workload: gluttonWorkload{}}
	for i := range users {
		rt.users.Store(int64(i), &runWorkloadUser{
			cfg:          cfg,
			workload:     gluttonWorkload{},
			actorName:    fmt.Sprintf("sb-%d", i),
			templateName: gluttonWorkload{}.DefaultTemplate(),
			userClass:    runWorkloadUserClass,
		})
	}

	rt.shutdown(context.Background())

	// An actor left behind holds a worker until someone removes it by hand, so
	// the teardown has to reach every one of them however it is scheduled.
	if got := ctrl.countCalls("DeleteActor"); got != users {
		t.Errorf("DeleteActor calls: got %d, want %d", got, users)
	}
	peak := ctrl.peakInFlight()
	if peak < 2 {
		t.Errorf("peak concurrent deletes: got %d, want > 1 — the teardown ran serially", peak)
	}
	if peak > teardownConcurrency {
		t.Errorf("peak concurrent deletes: got %d, want <= %d", peak, teardownConcurrency)
	}
}

func TestDynamicWait(t *testing.T) {
	rt := &runWorkloadRuntime{opts: Options{MinWait: 0, MaxWait: 0}}
	if got := rt.dynamicWait(); got != 0 {
		t.Errorf("dynamicWait with no bounds: got %v, want 0", got)
	}
}

// The atespace is one per run, so it is created once however many users
// start, rather than once per actor.
func TestAtespaceIsCreatedOncePerRun(t *testing.T) {
	ctrl := &fakeControlClient{}
	rt := newTestRuntime(t, ctrl)

	// Users are keyed by goroutine, so each start below is a new user.
	for range 3 {
		var wg sync.WaitGroup
		wg.Add(1)
		go func() {
			defer wg.Done()
			rt.iterate()
		}()
		wg.Wait()
	}

	if got := ctrl.countCalls("CreateActor"); got != 3 {
		t.Fatalf("CreateActor: got %d, want 3", got)
	}
	if got := ctrl.countCalls("CreateAtespace"); got != 1 {
		t.Errorf("CreateAtespace: got %d, want 1", got)
	}
}

// gaugeValue reads a workload's value of one of the actor gauges as exported.
// The gauges are package globals, so tests compare it before and after.
func gaugeValue(t *testing.T, metric, workload string) float64 {
	t.Helper()
	families, err := prometheus.DefaultGatherer.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, mf := range families {
		if mf.GetName() != metric {
			continue
		}
		for _, m := range mf.GetMetric() {
			for _, l := range m.GetLabel() {
				if l.GetName() == "workload" && l.GetValue() == workload {
					return m.GetGauge().GetValue()
				}
			}
		}
	}
	return 0
}

// CreateActor leaves the actor SUSPENDED, so that is how it is counted until
// the first resume.
func TestCreatedActorCountsAsSuspended(t *testing.T) {
	ctrl := &fakeControlClient{}
	rt := newTestRuntime(t, ctrl)
	name := rt.workload.Name()
	active := gaugeValue(t, "boomer_active_actors", name)
	suspended := gaugeValue(t, "boomer_suspended_actors", name)

	rt.iterate()

	if got := gaugeValue(t, "boomer_active_actors", name) - active; got != 0 {
		t.Errorf("active actors after a create: changed by %v, want 0", got)
	}
	if got := gaugeValue(t, "boomer_suspended_actors", name) - suspended; got != 1 {
		t.Errorf("suspended actors after a create: changed by %v, want 1", got)
	}
}

// Aborted is a lost race with a concurrent update, which clears on its own,
// so the call is made again rather than the actor thrown away.
func TestAbortedCallIsRetried(t *testing.T) {
	for _, tc := range []struct {
		name        string
		abortTimes  int
		wantResumes int
		wantErr     bool
	}{
		{name: "clears", abortTimes: 2, wantResumes: 3, wantErr: false},
		{name: "persists", abortTimes: 0, wantResumes: abortedAttempts, wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctrl := &fakeControlClient{
				resumeErr:      status.Error(codes.Aborted, "concurrent update conflict, please retry"),
				resumeErrTimes: tc.abortTimes,
			}
			u := newTestRunWorkloadUser(t, &pingServer{}, &userclass.Config{APIStub: ctrl})

			err := u.resume(context.Background())
			if (err != nil) != tc.wantErr {
				t.Fatalf("resume: got err %v, want error %v", err, tc.wantErr)
			}
			if got := ctrl.countCalls("ResumeActor"); got != tc.wantResumes {
				t.Errorf("ResumeActor calls: got %d, want %d", got, tc.wantResumes)
			}
		})
	}
}

// A 421 is a stale worker assignment that the next request routes around, so
// the instruction is sent again; any other 4xx is not.
func TestInstructionIsResentAfterAStaleAssignment(t *testing.T) {
	for _, tc := range []struct {
		name      string
		status    int
		wantPings int
		wantErr   bool
	}{
		{name: "421", status: http.StatusMisdirectedRequest, wantPings: 2, wantErr: false},
		{name: "400", status: http.StatusBadRequest, wantPings: 1, wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv := &pingServer{PingFailures: 1, PingFailStatus: tc.status}
			u := newTestRunWorkloadUser(t, srv, nil)

			err := u.executeInstruction(context.Background())
			if (err != nil) != tc.wantErr {
				t.Fatalf("executeInstruction: got err %v, want error %v", err, tc.wantErr)
			}
			if got := srv.RecordedPings(); got != tc.wantPings {
				t.Errorf("pings: got %d, want %d", got, tc.wantPings)
			}
		})
	}
}
