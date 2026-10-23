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
	"net/http"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/measure"
	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/scenario"
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
// spent, and the actor is discarded like after any other failed call.
func TestAFailedSuspendReportsAFailedCycle(t *testing.T) {
	ctrl := &fakeControlClient{suspendErr: errActorBusy}
	rt, sr := newMeasuringUser(t, &pingServer{}, ctrl, scenario.Options{})

	rt.iterate()

	if got := mustAttr(t, spanByName(t, sr, cycleTimeMetric), "total_ms").AsFloat64(); got <= 0 {
		t.Errorf("total_ms: got %v, want > 0", got)
	}
	if !startsOver(rt, ctrl) {
		t.Error("the user kept its slot after the suspend was refused")
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
// does: nothing is reported and the actor is discarded.
func TestARefusedInstructionDiscardsTheActor(t *testing.T) {
	ctrl := &fakeControlClient{}
	rt, sr := newMeasuringUser(t, &pingServer{PingFailures: 1}, ctrl, scenario.Options{})
	buf := captureLogs(t)

	rt.iterate()

	if got := ctrl.countCalls("DeleteActor"); got != 1 {
		t.Errorf("DeleteActor: got %d, want 1", got)
	}
	if !strings.Contains(buf.String(), "discarding the session") {
		t.Errorf("no discard was logged; got:\n%s", buf.String())
	}
	// The window is still reported, as a failure over the wait it spent.
	if got := mustAttr(t, spanByName(t, sr, ttfiMetric), "total_ms").AsFloat64(); got <= 0 {
		t.Errorf("total_ms: got %v, want > 0", got)
	}
	if !startsOver(rt, ctrl) {
		t.Error("the user kept its slot after the instruction was refused")
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
			if !strings.Contains(buf.String(), "discarding the session") {
				t.Errorf("no discard was logged; got:\n%s", buf.String())
			}

			// The slot being free is what lets the next iteration start over.
			rt.iterate()
			if got := tc.ctrl.countCalls("CreateActor"); got != 2 {
				t.Errorf("CreateActor after the discard: got %d, want 2 — the discarded user still holds its goroutine slot", got)
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
			if err := u.sandbox.Remove(context.Background()); err != nil {
				t.Fatalf("Remove failed: %v", err)
			}
			if got := tc.ctrl.deletedFromAnyState(); got != tc.wantAnyState {
				t.Errorf("AnyState: got %t, want %t", got, tc.wantAnyState)
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
			name: "an actor still in a transitional state",
			ctrl: &fakeControlClient{suspendErr: errActorBusy, deleteErr: errActorBusy},
			// The suspend failure rides along, because the delete failing on
			// its own means something else entirely.
			wantInLog: []string{"teardown left a session behind", "after suspend", `"left":1`},
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
// and the actor is discarded like after any other failed call.
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
			if !startsOver(rt, tc.ctrl) {
				t.Error("the user kept its slot after the call timed out")
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
	if ctrl.deletedFromAnyState() {
		t.Error("AnyState: got true after a suspend that worked, want false")
	}
}

// failureCount is how many failures of the named request have been recorded,
// summed over every user class. The counter is a package global.
func failureCount(t *testing.T, name string) float64 {
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
			if labels["name"] == name && labels["status"] == "failure" {
				total += m.GetCounter().GetValue()
			}
		}
	}
	return total
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
