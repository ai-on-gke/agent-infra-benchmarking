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
	"encoding/json"
	"log/slog"
	"maps"
	"net/http"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/measure"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/scenario"
	"go.ate.dev/substrate/api/ateapi/v1"
	"go.ate.dev/substrate/internal/benchmarking/boomer/boomerutil"
	"go.ate.dev/substrate/internal/benchmarking/boomer/userclass"
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

// newTestRuntime runs a scenario around ctrl, with no user started yet.
func newTestRuntime(t *testing.T, ctrl *fakeControlClient) *testRun {
	t.Helper()
	cfg := newTestConfig(t, &pingServer{}, &userclass.Config{APIStub: ctrl})
	return newTestRunOn(newTestScenarioOn(cfg, scenario.Options{}))
}

// The master's spawn messages match on the Python class name in
// master-image/tests/runworkload.py, so a mismatch here starts no users at all.
func TestUserClassIsTheMasters(t *testing.T) {
	if UserClass != "RunWorkloadUser" {
		t.Errorf("UserClass: got %q, want %q", UserClass, "RunWorkloadUser")
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

// newMeasuringUser runs a scenario for opts whose spans are recorded, with an
// actor already started on this goroutine so the next iteration cycles it.
func newMeasuringUser(t *testing.T, srv *pingServer, ctrl *fakeControlClient, opts scenario.Options) (*testRun, *tracetest.SpanRecorder) {
	t.Helper()
	tracer, sr := newRecordingTracer(t)
	cfg := newTestConfig(t, srv, &userclass.Config{APIStub: ctrl, Tracer: tracer})
	rt := newTestRunOn(newTestScenarioOn(cfg, opts))
	rt.iterate()
	if got := ctrl.countCalls("CreateActor"); got != 1 {
		t.Fatalf("CreateActor after the first iteration: got %d, want 1", got)
	}
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
			resumeMode:  scenario.ResumeModeExplicit,
			wantResumes: 1,
		},
		{
			name:       "implicit resume mode",
			resumeMode: scenario.ResumeModeImplicit,
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
			rt, sr := newMeasuringUser(t, &pingServer{}, ctrl, scenario.Options{ResumeMode: tc.resumeMode})

			rt.iterate()

			if got := ctrl.countCalls("ResumeActor"); got != tc.wantResumes {
				t.Errorf("ResumeActor: got %d, want %d", got, tc.wantResumes)
			}
			// The instruction goes out either way; only the wake differs.
			span := spanByName(t, sr, ttfiMetric)
			if got := mustAttr(t, span, "resume_mode").AsString(); got != cmp.Or(tc.resumeMode, scenario.ResumeModeExplicit) {
				t.Errorf("resume_mode: got %q, want the effective mode", got)
			}
		})
	}
}

// The suspend that ends the cycle comes after the window has closed, so
// however long it took must not appear in TTFI.
func TestTTFIExcludesTheSuspend(t *testing.T) {
	const suspend = 200 * time.Millisecond
	rt, sr := newMeasuringUser(t, &pingServer{}, &fakeControlClient{suspendDelay: suspend}, scenario.Options{})

	rt.iterate()

	total := mustAttr(t, spanByName(t, sr, ttfiMetric), "total_ms").AsFloat64()
	if want := boomerutil.MsFloat(suspend); total >= want {
		t.Errorf("total_ms: got %v, want well under %v — the suspend landed inside the window", total, want)
	}
}

// The window closes on the served instruction in both modes, so implicit
// resume, where the router wakes the actor, is not reported as near zero.
func TestTTFIEndsWhenTheInstructionIsServed(t *testing.T) {
	const service = 200 * time.Millisecond
	for _, mode := range []string{scenario.ResumeModeExplicit, scenario.ResumeModeImplicit} {
		t.Run(mode, func(t *testing.T) {
			rt, sr := newMeasuringUser(t, &pingServer{PingDelay: service}, &fakeControlClient{}, scenario.Options{ResumeMode: mode})

			rt.iterate()

			total := mustAttr(t, spanByName(t, sr, ttfiMetric), "total_ms").AsFloat64()
			if want := boomerutil.MsFloat(service); total < want {
				t.Errorf("total_ms: got %v, want >= %v — the window closed before the instruction was served", total, want)
			}
		})
	}
}

// CycleTime runs from the resume to the suspend returning, so unlike TTFI it
// holds the suspend, and its span is the one the suspend is traced under.
func TestCycleTimeSpansTheResumeThroughTheSuspend(t *testing.T) {
	const service, suspend = 100 * time.Millisecond, 100 * time.Millisecond
	rt, sr := newMeasuringUser(t, &pingServer{PingDelay: service}, &fakeControlClient{suspendDelay: suspend}, scenario.Options{})

	rt.iterate()

	cycle := spanByName(t, sr, cycleTimeMetric)
	if total, want := mustAttr(t, cycle, "total_ms").AsFloat64(), boomerutil.MsFloat(service+suspend); total < want {
		t.Errorf("total_ms: got %v, want >= %v", total, want)
	}
	for _, name := range []string{ttfiMetric, sandbox.SuspendMetric} {
		if got := spanByName(t, sr, name).Parent().SpanID(); got != cycle.SpanContext().SpanID() {
			t.Errorf("%s span is not a child of the %s span", name, cycleTimeMetric)
		}
	}
}

// A refused suspend still reports the cycle, as a failure over the time it
// spent.
func TestAFailedSuspendReportsAFailedCycle(t *testing.T) {
	ctrl := &fakeControlClient{suspendErr: errActorBusy}
	rt, sr := newMeasuringUser(t, &pingServer{}, ctrl, scenario.Options{})

	rt.iterate()

	if got := mustAttr(t, spanByName(t, sr, cycleTimeMetric), "total_ms").AsFloat64(); got <= 0 {
		t.Errorf("total_ms: got %v, want > 0", got)
	}
}

// The instruction is sent once. A refused one is a fault to surface, not a
// state to wait out, so nothing retries it.
func TestTheInstructionIsSentOnce(t *testing.T) {
	srv := &pingServer{PingFailures: 1}
	rt, _ := newMeasuringUser(t, srv, &fakeControlClient{}, scenario.Options{})

	rt.iterate()

	if got := srv.RecordedPings(); got != 1 {
		t.Errorf("instructions sent: got %d, want 1", got)
	}
}

// A refused instruction ends the cycle the same way any other failed call
// does: the window is reported as a failure, and the actor, which settles,
// is kept for the next cycle.
func TestARefusedInstructionEndsTheCycle(t *testing.T) {
	ctrl := &fakeControlClient{}
	rt, sr := newMeasuringUser(t, &pingServer{PingFailures: 1}, ctrl, scenario.Options{})

	rt.iterate()

	if got := mustAttr(t, spanByName(t, sr, ttfiMetric), "total_ms").AsFloat64(); got <= 0 {
		t.Errorf("total_ms: got %v, want > 0", got)
	}
	if startsOver(rt, ctrl) {
		t.Error("the user replaced an actor that settled after the instruction was refused")
	}
}

// A refused resume never gave the actor a worker, so no instruction is sent —
// but the window it opened is still reported, as a failure.
func TestAFailedResumeReportsAFailedWindow(t *testing.T) {
	srv := &pingServer{}
	rt, sr := newMeasuringUser(t, srv, &fakeControlClient{resumeErr: errResumeRefused}, scenario.Options{})

	rt.iterate()

	if got := srv.RecordedPings(); got != 0 {
		t.Errorf("instructions after a failed resume: got %d, want 0", got)
	}
	if got := mustAttr(t, spanByName(t, sr, ttfiMetric), "total_ms").AsFloat64(); got <= 0 {
		t.Errorf("total_ms: got %v, want > 0", got)
	}
}

// A failed call can leave the actor mid transition, a suspend that timed out
// still snapshotting on the server, so the actor is polled until it settles.
// One that settles SUSPENDED, or RUNNING and is then suspended, is kept for the
// next cycle; one that crashed, is gone or stays in transition is deleted and
// the goroutine's slot freed, for the next iteration to build a new one. The
// Recover row counts the two: a success kept the actor, a failure replaced it.
func TestAFailedCallKeepsAnActorThatSettlesAndReplacesOneThatDoesNot(t *testing.T) {
	tests := []struct {
		name         string
		ctrl         *fakeControlClient
		wantReplaced bool
		// wantSuspends is the unmetered SuspendActor calls the settle makes.
		wantSuspends int
	}{
		{
			name: "a suspend that finishes on the server",
			ctrl: &fakeControlClient{suspendErr: errActorBusy, states: []ateapi.ActorState{
				ateapi.ActorState_ACTOR_STATE_SUSPENDING, ateapi.ActorState_ACTOR_STATE_SUSPENDED,
			}},
		},
		{
			name:         "a refused instruction on a running actor",
			ctrl:         &fakeControlClient{states: []ateapi.ActorState{ateapi.ActorState_ACTOR_STATE_RUNNING}},
			wantSuspends: 1,
		},
		{
			name:         "a crashed actor",
			ctrl:         &fakeControlClient{resumeErr: errResumeRefused, states: []ateapi.ActorState{ateapi.ActorState_ACTOR_STATE_CRASHED}},
			wantReplaced: true,
		},
		{
			name:         "an actor that stays suspending",
			ctrl:         &fakeControlClient{suspendErr: errActorBusy, states: []ateapi.ActorState{ateapi.ActorState_ACTOR_STATE_SUSPENDING}},
			wantReplaced: true,
		},
		{
			name:         "an actor already gone",
			ctrl:         &fakeControlClient{resumeErr: errResumeRefused, getActorErr: status.Error(codes.NotFound, "Actor benchmark/x not found")},
			wantReplaced: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			shortenSettle(t)
			srv := &pingServer{}
			if tc.wantSuspends > 0 {
				srv.PingFailures = 1
			}
			cfg := newTestConfig(t, srv, &userclass.Config{APIStub: tc.ctrl})
			rt := newTestRunOn(newTestScenarioOn(cfg, scenario.Options{}))
			kept, replaced := requestCount(t, recoverMetric, "success"), requestCount(t, recoverMetric, "failure")
			buf := captureLogs(t)

			rt.iterate() // creates
			rt.iterate() // fails, settles

			wantDeletes, wantLog := 0, "kept the session after a failed call"
			if tc.wantReplaced {
				wantDeletes, wantLog = 1, "discarding the session after a failed call"
			}
			if got := tc.ctrl.countCalls("DeleteActor"); got != wantDeletes {
				t.Errorf("DeleteActor: got %d, want %d", got, wantDeletes)
			}
			if !strings.Contains(buf.String(), wantLog) {
				t.Errorf("log missing %q; got:\n%s", wantLog, buf.String())
			}
			// The cycle's own suspend never ran after a refused instruction, so
			// the only one is the settle's.
			if tc.wantSuspends > 0 {
				if got := tc.ctrl.countCalls("SuspendActor"); got != tc.wantSuspends {
					t.Errorf("SuspendActor: got %d, want %d", got, tc.wantSuspends)
				}
			}
			gotKept := requestCount(t, recoverMetric, "success") - kept
			gotReplaced := requestCount(t, recoverMetric, "failure") - replaced
			if tc.wantReplaced && (gotKept != 0 || gotReplaced != 1) || !tc.wantReplaced && (gotKept != 1 || gotReplaced != 0) {
				t.Errorf("Recover rows: got %v kept and %v replaced, want the actor replaced %t", gotKept, gotReplaced, tc.wantReplaced)
			}

			// A replaced actor's slot being free is what lets the next
			// iteration start over; a kept one cycles on.
			if got := startsOver(rt, tc.ctrl); got != tc.wantReplaced {
				t.Errorf("next iteration created an actor: got %t, want %t", got, tc.wantReplaced)
			}
		})
	}
}

// DeleteActor removes an actor in any state, so Remove does not suspend it
// first, not even when a suspend would be refused.
func TestRemoveDeletesWithoutSuspending(t *testing.T) {
	ctrl := &fakeControlClient{suspendErr: errActorBusy}
	u := newTestRunWorkloadUser(t, &pingServer{}, &userclass.Config{APIStub: ctrl})

	if err := u.sandbox.Remove(context.Background()); err != nil {
		t.Fatalf("Remove failed: %v", err)
	}
	if got := ctrl.countCalls("SuspendActor"); got != 0 {
		t.Errorf("SuspendActor calls: got %d, want none", got)
	}
	if got := ctrl.countCalls("DeleteActor"); got != 1 {
		t.Errorf("DeleteActor calls: got %d, want 1", got)
	}
}

// Sleep asks for the suspend mode the scenario names, and leaves it to
// substrate when it names none.
func TestSuspendAsksForTheSuspendMode(t *testing.T) {
	tests := []struct {
		mode string
		want ateapi.DurabilityMode
	}{
		{mode: scenario.SuspendModeAsync, want: ateapi.DurabilityMode_DURABILITY_MODE_ASYNCHRONOUS},
		{mode: scenario.SuspendModeSync, want: ateapi.DurabilityMode_DURABILITY_MODE_SYNCHRONOUS},
		{mode: "", want: ateapi.DurabilityMode_DURABILITY_MODE_UNSPECIFIED},
	}

	for _, tc := range tests {
		t.Run(tc.mode, func(t *testing.T) {
			ctrl := &fakeControlClient{}
			cfg := newTestConfig(t, &pingServer{}, &userclass.Config{APIStub: ctrl})
			u := newTestUser(t, newTestScenarioOn(cfg, scenario.Options{SuspendMode: tc.mode}))

			if err := u.sandbox.Sleep(context.Background()); err != nil {
				t.Fatalf("Sleep failed: %v", err)
			}
			if got := ctrl.recordedSuspendModes(); len(got) != 1 || got[0] != tc.want {
				t.Errorf("suspend modes: got %v, want [%v]", got, tc.want)
			}
		})
	}
}

// Sleep reports whether the actor actually went down, and says why when it
// did not: measure records the row but logs only a sampled trace.
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

			err := u.sandbox.Sleep(context.Background())

			if tc.wantErr && err == nil {
				t.Fatal("Sleep returned nil, want the refusal")
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("Sleep failed: %v", err)
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
			wantInLog: []string{"teardown closed a session", `"left":0`, `"removed":1`},
		},
		{
			name:      "a delete the control plane refused",
			ctrl:      &fakeControlClient{deleteErr: errActorBusy},
			wantInLog: []string{"teardown left a session behind", errActorBusy.Error(), `"left":1`},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rt := newTestRuntime(t, tc.ctrl)
			rt.iterate()
			buf := captureLogs(t)

			rt.shutdown(context.Background())

			got := buf.String()
			if name := tc.ctrl.recordedActor(); name == "" || !strings.Contains(got, name) {
				t.Errorf("teardown log does not name the actor %q; got:\n%s", name, got)
			}
			for _, want := range tc.wantInLog {
				if !strings.Contains(got, want) {
					t.Errorf("teardown log missing %q; got:\n%s", want, got)
				}
			}
		})
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
	name := rt.c.Workload.Name()
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
	shortenRetries(t)
	for _, tc := range []struct {
		name       string
		abortTimes int
		wantErr    bool
	}{
		{name: "clears", abortTimes: 4, wantErr: false},
		{name: "persists", abortTimes: 0, wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctrl := &fakeControlClient{
				resumeErr:      status.Error(codes.Aborted, "concurrent update conflict, please retry"),
				resumeErrTimes: tc.abortTimes,
			}
			u := newTestRunWorkloadUser(t, &pingServer{}, &userclass.Config{APIStub: ctrl})

			start := time.Now()
			err := u.sandbox.Wake(context.Background())
			if (err != nil) != tc.wantErr {
				t.Fatalf("Wake: got err %v, want error %v", err, tc.wantErr)
			}
			got := ctrl.countCalls("ResumeActor")
			if !tc.wantErr && got != tc.abortTimes+1 {
				t.Errorf("ResumeActor calls: got %d, want %d", got, tc.abortTimes+1)
			}
			if tc.wantErr {
				if got < 2 {
					t.Errorf("ResumeActor calls: got %d, want it retried", got)
				}
				if elapsed := time.Since(start); elapsed > measure.RetryBudget+measure.RetryMaxDelay {
					t.Errorf("gave up after %v, want within the %v budget", elapsed, measure.RetryBudget)
				}
			}
		})
	}
}

// errNoRoom is the refusal of a resume while every worker is full.
var errNoRoom = status.Error(codes.ResourceExhausted, "no worker has room for the actor")

// waitForWorker has rt's resumes wait for a free worker, as density's do.
func waitForWorker(rt *testRun) { rt.provider.(*sandbox.Substrate).WaitForWorker = true }

// rowCounts is how many rows of each name and status have been recorded, to
// tell what one iteration added.
func rowCounts(t *testing.T) map[string]float64 {
	t.Helper()
	counts := map[string]float64{}
	for _, name := range []string{sandbox.ResumeMetric, sandbox.SlotWaitMetric, ttfiMetric} {
		for _, status := range []string{"success", "failure"} {
			counts[name+" "+status] = requestCount(t, name, status)
		}
	}
	return counts
}

// rowsAdded is what rowCounts gained since before, leaving out what did not
// change.
func rowsAdded(t *testing.T, before map[string]float64) map[string]float64 {
	t.Helper()
	added := map[string]float64{}
	for key, n := range rowCounts(t) {
		if n != before[key] {
			added[key] = n - before[key]
		}
	}
	return added
}

// A resume refused for want of a free worker is made again until one frees up,
// and the cycle goes on: the refusals report no Resume rows of their own, and
// the wait is one SlotWait row, inside TTFI.
func TestANoRoomResumeWaitsForAWorker(t *testing.T) {
	shortenRetries(t)
	srv := &pingServer{}
	ctrl := &fakeControlClient{resumeErr: errNoRoom, resumeErrTimes: 3}
	rt, sr := newMeasuringUser(t, srv, ctrl, scenario.Options{})
	waitForWorker(rt)
	before := rowCounts(t)

	rt.iterate()

	if got := ctrl.countCalls("ResumeActor"); got != 4 {
		t.Errorf("ResumeActor calls: got %d, want 4", got)
	}
	if got := srv.RecordedPings(); got != 1 {
		t.Errorf("instructions: got %d, want the cycle's 1", got)
	}
	want := map[string]float64{"Resume success": 1, "SlotWait success": 1, "TTFI success": 1}
	if got := rowsAdded(t, before); !maps.Equal(got, want) {
		t.Errorf("rows: got %v, want %v", got, want)
	}
	wait := mustAttr(t, spanByName(t, sr, sandbox.SlotWaitMetric), "total_ms").AsFloat64()
	ttfi := mustAttr(t, spanByName(t, sr, ttfiMetric), "total_ms").AsFloat64()
	if wait <= 0 || wait > ttfi {
		t.Errorf("SlotWait total_ms: got %v, want above 0 and within TTFI's %v", wait, ttfi)
	}
}

// A resume that goes through at once waited for nothing and reports no
// SlotWait row; and without the wait turned on, a refused one fails the cycle
// at once, as a run that is not about density wants.
func TestOnlyARefusedResumeWaits(t *testing.T) {
	shortenRetries(t)
	for _, tc := range []struct {
		name        string
		ctrl        *fakeControlClient
		wait        bool
		wantResumes int
		want        map[string]float64
	}{
		{
			name:        "not refused",
			ctrl:        &fakeControlClient{},
			wait:        true,
			wantResumes: 1,
			want:        map[string]float64{"Resume success": 1, "TTFI success": 1},
		},
		{
			name:        "not waiting",
			ctrl:        &fakeControlClient{resumeErr: errNoRoom, resumeErrTimes: 3},
			wantResumes: 1,
			want:        map[string]float64{"Resume failure": 1, "TTFI failure": 1},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rt, _ := newMeasuringUser(t, &pingServer{}, tc.ctrl, scenario.Options{})
			if tc.wait {
				waitForWorker(rt)
			}
			before := rowCounts(t)

			rt.iterate()

			if got := tc.ctrl.countCalls("ResumeActor"); got != tc.wantResumes {
				t.Errorf("ResumeActor calls: got %d, want %d", got, tc.wantResumes)
			}
			if got := rowsAdded(t, before); !maps.Equal(got, tc.want) {
				t.Errorf("rows: got %v, want %v", got, tc.want)
			}
		})
	}
}

// A worker that does not free up within the timeout fails the cycle, with one
// failed Resume row for the last refusal and a failed SlotWait over the wait.
func TestAWaitForAWorkerEndsAtTheTimeout(t *testing.T) {
	shortenRetries(t)
	const deadline = 50 * time.Millisecond
	ctrl := &fakeControlClient{resumeErr: errNoRoom}
	srv := &pingServer{}
	rt, sr := newMeasuringUser(t, srv, ctrl, scenario.Options{Timeout: deadline})
	waitForWorker(rt)
	before := rowCounts(t)

	start := time.Now()
	rt.iterate()

	if got := ctrl.countCalls("ResumeActor"); got < 3 {
		t.Errorf("ResumeActor calls: got %d, want several", got)
	}
	if got := srv.RecordedPings(); got != 0 {
		t.Errorf("instructions: got %d, want none", got)
	}
	want := map[string]float64{"Resume failure": 1, "SlotWait failure": 1, "TTFI failure": 1}
	if got := rowsAdded(t, before); !maps.Equal(got, want) {
		t.Errorf("rows: got %v, want %v", got, want)
	}
	if elapsed := time.Since(start); elapsed > 10*deadline {
		t.Errorf("gave up after %v, want about the %v timeout", elapsed, deadline)
	}
	if got := mustAttr(t, spanByName(t, sr, sandbox.SlotWaitMetric), "total_ms").AsFloat64(); got <= 0 {
		t.Errorf("SlotWait total_ms: got %v, want > 0", got)
	}
}

// The end of the run ends a wait for a worker without a failure, however long
// the timeout would have let it go on.
func TestTheEndOfTheRunEndsAWaitForAWorker(t *testing.T) {
	shortenRetries(t)
	ctrl := &fakeControlClient{resumeErr: errNoRoom}
	rt := newTestRuntime(t, ctrl)
	waitForWorker(rt)
	before := rowCounts(t)

	done := make(chan struct{})
	go func() {
		defer close(done)
		rt.iterate()
		rt.iterate()
	}()
	for ctrl.countCalls("ResumeActor") < 2 {
		time.Sleep(5 * time.Millisecond)
	}

	start := time.Now()
	rt.shutdown(context.Background())
	<-done

	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Errorf("teardown took %v; the wait should have ended with the run", elapsed)
	}
	if got := rowsAdded(t, before); len(got) != 0 {
		t.Errorf("rows: got %v, want none", got)
	}
}

// A 421 is a worker assignment that was stale or not live yet, which the
// router gets right once the actor's operation lands, so the instruction is
// sent again; any other 4xx is not.
func TestInstructionIsResentAfterAStaleAssignment(t *testing.T) {
	shortenRetries(t)
	for _, tc := range []struct {
		name      string
		status    int
		failures  int
		wantPings int
		wantErr   bool
	}{
		{name: "421", status: http.StatusMisdirectedRequest, failures: 3, wantPings: 4, wantErr: false},
		{name: "400", status: http.StatusBadRequest, failures: 1, wantPings: 1, wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv := &pingServer{PingFailures: tc.failures, PingFailStatus: tc.status}
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

// The waits between retries are not what these tests are about.
func shortenRetries(t *testing.T) {
	t.Helper()
	initial, maxDelay, budget := measure.RetryInitialDelay, measure.RetryMaxDelay, measure.RetryBudget
	measure.RetryInitialDelay, measure.RetryMaxDelay, measure.RetryBudget = time.Millisecond, 4*time.Millisecond, 50*time.Millisecond
	t.Cleanup(func() {
		measure.RetryInitialDelay, measure.RetryMaxDelay, measure.RetryBudget = initial, maxDelay, budget
	})
}

// A call that outlives the timeout is cut off at it and reported as a failure,
// and the actor, which settles, is kept like after any other failed call.
func TestACallPastTheDeadlineFails(t *testing.T) {
	const deadline = 50 * time.Millisecond
	for _, tc := range []struct {
		name string
		srv  *pingServer
		ctrl *fakeControlClient
	}{
		{name: "resume", srv: &pingServer{}, ctrl: &fakeControlClient{resumeHang: true}},
		{name: "instruction", srv: &pingServer{PingDelay: 10 * deadline}, ctrl: &fakeControlClient{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rt, sr := newMeasuringUser(t, tc.srv, tc.ctrl, scenario.Options{Timeout: deadline})
			buf := captureLogs(t)

			rt.iterate()

			if got := failedWindowErr(buf, ttfiMetric); !strings.Contains(got, "deadline exceeded") {
				t.Errorf("TTFI failure: got %q, want a deadline exceeded; log:\n%s", got, buf.String())
			}
			total := mustAttr(t, spanByName(t, sr, ttfiMetric), "total_ms").AsFloat64()
			if lo, hi := boomerutil.MsFloat(deadline), boomerutil.MsFloat(10*deadline); total < lo || total >= hi {
				t.Errorf("total_ms: got %v, want about the deadline, in [%v, %v)", total, lo, hi)
			}
			if startsOver(rt, tc.ctrl) {
				t.Error("the user replaced an actor that settled after the call timed out")
			}
		})
	}
}

// failedWindowErr is the error the sampled trace log gives for the window
// reported as name, or empty when it was not reported as a failure.
func failedWindowErr(buf *bytes.Buffer, name string) string {
	for _, line := range strings.Split(buf.String(), "\n") {
		var rec struct{ Msg, Name, Err string }
		if json.Unmarshal([]byte(line), &rec) == nil && rec.Msg == "traced span (failed)" && rec.Name == name {
			return rec.Err
		}
	}
	return ""
}

// The teardown and a VU discarding the same actor can both delete it, and the
// second finds it gone; that is the outcome the delete wanted, not a failure.
func TestDeletingAnActorAlreadyGoneSucceeds(t *testing.T) {
	ctrl := &fakeControlClient{deleteErr: status.Error(codes.NotFound, "Actor benchmark/x not found")}
	u := newTestRunWorkloadUser(t, &pingServer{}, &userclass.Config{APIStub: ctrl})

	if err := u.sandbox.Remove(context.Background()); err != nil {
		t.Fatalf("Remove: got %v, want nil", err)
	}
}

// failureCount is how many failures of the named request have been recorded,
// summed over every user class. The counter is a package global.
func failureCount(t *testing.T, name string) float64 {
	t.Helper()
	return requestCount(t, name, "failure")
}

// requestCount is how many requests of the name with the status, success or
// failure, have been recorded, summed over every user class.
func requestCount(t *testing.T, name, status string) float64 {
	t.Helper()
	families, err := prometheus.DefaultGatherer.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	var total float64
	for _, mf := range families {
		if mf.GetName() != "locust_requests_total" {
			continue
		}
		for _, m := range mf.GetMetric() {
			labels := map[string]string{}
			for _, l := range m.GetLabel() {
				labels[l.GetName()] = l.GetValue()
			}
			if labels["name"] == name && labels["status"] == status {
				total += m.GetCounter().GetValue()
			}
		}
	}
	return total
}

// A suspend still in flight when the run ends is let finish, because ateapi
// abandons the checkpoint of one cut off and crashes the actor. The teardown
// then deletes the actor.
func TestTheEndOfTheRunLetsASuspendFinish(t *testing.T) {
	const delay = 200 * time.Millisecond
	ctrl := &fakeControlClient{suspendDelay: delay}
	rt := newTestRuntime(t, ctrl)
	before := failureCount(t, sandbox.SuspendMetric)

	// Users are keyed by goroutine, so both iterations run on this one: the
	// first creates the actor, the second cycles it into the slow suspend.
	start := time.Now()
	done := make(chan struct{})
	go func() {
		defer close(done)
		rt.iterate()
		rt.iterate()
	}()
	for ctrl.countCalls("SuspendActor") == 0 {
		time.Sleep(5 * time.Millisecond)
	}

	rt.shutdown(context.Background())
	<-done

	if got := ctrl.cutSuspends(); got != 0 {
		t.Errorf("suspends cut off: got %d, want none", got)
	}
	if elapsed := time.Since(start); elapsed < delay {
		t.Errorf("teardown took %v; it should have waited for the %v suspend", elapsed, delay)
	}
	if got := failureCount(t, sandbox.SuspendMetric) - before; got != 0 {
		t.Errorf("Suspend failures: got %v more, want none", got)
	}
}

// An instruction still in flight when the run ends is cut off, not waited for,
// and is neither a failure nor a reason to discard the actor: the teardown
// removes it.
func TestTheEndOfTheRunCutsOffAnInstructionWithoutAFailure(t *testing.T) {
	ctrl := &fakeControlClient{}
	cfg := newTestConfig(t, &pingServer{PingDelay: 5 * time.Second}, &userclass.Config{APIStub: ctrl})
	rt := newTestRunOn(newTestScenarioOn(cfg, scenario.Options{}))

	before := map[string]float64{}
	for _, name := range []string{ttfiMetric, cycleTimeMetric, executionTimeMetric} {
		before[name] = failureCount(t, name)
	}

	// Users are keyed by goroutine, so both iterations run on this one: the
	// first creates the actor, the second resumes it and sends the slow
	// instruction.
	done := make(chan struct{})
	go func() {
		defer close(done)
		rt.iterate()
		rt.iterate()
	}()
	for ctrl.countCalls("ResumeActor") == 0 {
		time.Sleep(5 * time.Millisecond)
	}
	time.Sleep(50 * time.Millisecond)

	start := time.Now()
	rt.shutdown(context.Background())
	<-done

	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Errorf("teardown took %v; the instruction should have been cut off", elapsed)
	}
	for name, n := range before {
		if got := failureCount(t, name) - n; got != 0 {
			t.Errorf("%s failures: got %v more, want none", name, got)
		}
	}
	if got := ctrl.countCalls("DeleteActor"); got != 1 {
		t.Errorf("DeleteActor calls: got %d, want the teardown's one", got)
	}
}
