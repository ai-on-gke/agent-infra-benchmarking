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
	"context"
	"errors"
	"log/slog"
	"net/http"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/agent-substrate/substrate/internal/atenet"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/dynconfig"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/userclass"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// newRecordingTracer returns a tracer whose ended spans land in the returned
// recorder, so a test can assert on the span tree the measurement window
// builds.
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

func attrValue(t *testing.T, s sdktrace.ReadOnlySpan, key string) (attribute.Value, bool) {
	t.Helper()
	for _, kv := range s.Attributes() {
		if string(kv.Key) == key {
			return kv.Value, true
		}
	}
	return attribute.Value{}, false
}

func mustAttr(t *testing.T, s sdktrace.ReadOnlySpan, key string) attribute.Value {
	t.Helper()
	v, ok := attrValue(t, s, key)
	if !ok {
		t.Fatalf("span %q missing attribute %q", s.Name(), key)
	}
	return v
}

// newRunWorkloadTestUser builds a user whose spans are recorded, returning
// both.
func newRunWorkloadTestUser(t *testing.T, srv *pingServer, dyn dynconfig.Config, ctrl *fakeControlClient) (*runWorkloadUser, *tracetest.SpanRecorder) {
	t.Helper()
	tracer, sr := newRecordingTracer(t)
	cfg := &userclass.Config{
		APIStub: ctrl,
		Tracer:  tracer,
		Dyn:     dynconfig.NewHolder(dyn),
	}
	return newTestRunWorkloadUser(t, srv, cfg), sr
}

func TestResumeModeSelectsTheControlPlaneCall(t *testing.T) {
	tests := []struct {
		name         string
		resumeMode   string
		wantGRPCCall []string
	}{
		{
			name:         "explicit resume mode",
			resumeMode:   dynconfig.ResumeModeExplicit,
			wantGRPCCall: []string{"ResumeActor"},
		},
		{
			// The window is one instruction: the router owns the wake, so a
			// ResumeActor RPC here would measure a path no client takes.
			name:         "implicit resume mode",
			resumeMode:   dynconfig.ResumeModeImplicit,
			wantGRPCCall: nil,
		},
		{
			// Same default as common/resume_mode.py, so an unset value
			// behaves identically to the one locust always sends.
			name:         "unset defaults to explicit",
			resumeMode:   "",
			wantGRPCCall: []string{"ResumeActor"},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			srv := &pingServer{}
			ctrl := &fakeControlClient{}
			dyn := dynconfig.Config{ResumeMode: tc.resumeMode}
			u, _ := newRunWorkloadTestUser(t, srv, dyn, ctrl)

			u.measure(context.Background(), ttfiMetric, dyn, Options{}, false)

			if got := ctrl.recordedCalls(); !reflect.DeepEqual(got, tc.wantGRPCCall) {
				t.Errorf("gRPC calls: got %v, want %v", got, tc.wantGRPCCall)
			}
			if got := srv.RecordedPings(); got != 1 {
				t.Errorf("ping attempts: got %d, want 1", got)
			}
		})
	}
}

func TestRetriesWhileTheActorActivates(t *testing.T) {
	// Three rejections then success: a client cannot use the sandbox until
	// the fourth attempt, so all four are inside the window.
	srv := &pingServer{PingFailures: 3}
	dyn := dynconfig.Config{ResumeMode: dynconfig.ResumeModeImplicit}
	u, sr := newRunWorkloadTestUser(t, srv, dyn, &fakeControlClient{})

	attempts, _, err := u.firstInstruction(context.Background(), 5*time.Second)
	if err != nil {
		t.Fatalf("firstInstruction failed: %v", err)
	}
	if attempts != 4 {
		t.Errorf("attempts: got %d, want 4", attempts)
	}
	if got := srv.RecordedPings(); got != 4 {
		t.Errorf("ping requests: got %d, want 4", got)
	}

	span := spanByName(t, sr, instructionMetric)
	if got := mustAttr(t, span, "attempts").AsInt64(); got != 4 {
		t.Errorf("attempts span attribute: got %d, want 4", got)
	}
}

func TestDoesNotRetryUnrecoverableFailures(t *testing.T) {
	tests := []struct {
		name string
		srv  *pingServer
	}{
		{
			// A 4xx is a misrouted or misconfigured request; waiting will
			// not turn it into a running sandbox.
			name: "client error",
			srv:  &pingServer{Status: http.StatusNotFound},
		},
		{
			// The actor answered, but did not run the instruction.
			name: "echo mismatch",
			srv:  &pingServer{PingEcho: "not-what-was-sent"},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			dyn := dynconfig.Config{ResumeMode: dynconfig.ResumeModeImplicit}
			u, _ := newRunWorkloadTestUser(t, tc.srv, dyn, &fakeControlClient{})

			attempts, _, err := u.firstInstruction(context.Background(), 5*time.Second)
			if err == nil {
				t.Fatal("firstInstruction succeeded, want failure")
			}
			if attempts != 1 {
				t.Errorf("attempts: got %d, want 1", attempts)
			}
		})
	}
}

func TestFailsWhenTheBudgetElapses(t *testing.T) {
	// Never becomes ready, so the loop must be stopped by the budget rather
	// than run for the rest of the test.
	srv := &pingServer{PingFailures: 1 << 30}
	dyn := dynconfig.Config{ResumeMode: dynconfig.ResumeModeImplicit}
	u, _ := newRunWorkloadTestUser(t, srv, dyn, &fakeControlClient{})

	start := time.Now()
	_, _, err := u.firstInstruction(context.Background(), 100*time.Millisecond)
	elapsed := time.Since(start)

	if err == nil {
		t.Fatal("firstInstruction succeeded, want budget failure")
	}
	if elapsed > 5*time.Second {
		t.Errorf("budget not enforced: loop ran for %v", elapsed)
	}
}

func TestBudget(t *testing.T) {
	u := &runWorkloadUser{}
	if got := u.budget(Options{}); got != DefaultTimeout {
		t.Errorf("unset budget: got %v, want %v", got, DefaultTimeout)
	}
	if got := u.budget(Options{Timeout: 5 * time.Second}); got != 5*time.Second {
		t.Errorf("configured budget: got %v, want 5s", got)
	}
}

// The measurement is the parent span's duration and the phase split lives on
// its attributes, so one sampled trace has to explain its own total without
// the reader joining it to anything else.
func TestSpanCarriesTheBreakdown(t *testing.T) {
	srv := &pingServer{}
	ctrl := &fakeControlClient{serverElapsedUs: "4000"} // 4ms of ateapi handler time
	dyn := dynconfig.Config{ResumeMode: dynconfig.ResumeModeExplicit}
	u, sr := newRunWorkloadTestUser(t, srv, dyn, ctrl)

	u.measure(context.Background(), firstResumeMetric, dyn, Options{}, true)

	parent := spanByName(t, sr, firstResumeMetric)
	if got := mustAttr(t, parent, "resume_mode").AsString(); got != dynconfig.ResumeModeExplicit {
		t.Errorf("resume_mode: got %q, want %q", got, dynconfig.ResumeModeExplicit)
	}
	if got := mustAttr(t, parent, "first_resume").AsBool(); !got {
		t.Error("first_resume: got false, want true")
	}
	wantTemplate := gluttonWorkload{}.DefaultTemplate()
	if got := mustAttr(t, parent, "template").AsString(); got != wantTemplate {
		t.Errorf("template: got %q, want %q", got, wantTemplate)
	}
	if got := mustAttr(t, parent, "workload").AsString(); got != gluttonWorkloadName {
		t.Errorf("workload: got %q, want %q", got, gluttonWorkloadName)
	}
	if got := mustAttr(t, parent, "attempts").AsInt64(); got != 1 {
		t.Errorf("attempts: got %d, want 1", got)
	}
	if got := mustAttr(t, parent, "total_ms").AsFloat64(); got <= 0 {
		t.Errorf("total_ms: got %v, want > 0", got)
	}
	if got := mustAttr(t, parent, "instruction_ms").AsFloat64(); got <= 0 {
		t.Errorf("instruction_ms: got %v, want > 0", got)
	}
	// The resume trailer is the server's own handler duration, so the
	// control-plane share is attributable rather than inferred.
	if got := mustAttr(t, parent, "control_plane_ms").AsFloat64(); got != 4 {
		t.Errorf("control_plane_ms: got %v, want 4", got)
	}

	// Both phases must hang off the window parent; a detached child would put
	// the phase in a different trace from the total it belongs to.
	for _, child := range []string{resumeMetric, instructionMetric} {
		s := spanByName(t, sr, child)
		if s.Parent().SpanID() != parent.SpanContext().SpanID() {
			t.Errorf("span %q is not a child of %q", child, firstResumeMetric)
		}
	}
}

func TestReportsAFailedResumeWithoutSendingInstructions(t *testing.T) {
	// The window cannot be measured if the actor never gets a worker, and a
	// row built from instructions that were never sent would be a lie.
	srv := &pingServer{}
	ctrl := &fakeControlClient{resumeErr: errResumeRefused}
	dyn := dynconfig.Config{ResumeMode: dynconfig.ResumeModeExplicit}
	u, sr := newRunWorkloadTestUser(t, srv, dyn, ctrl)

	u.measure(context.Background(), ttfiMetric, dyn, Options{}, false)

	if got := srv.RecordedPings(); got != 0 {
		t.Errorf("ping requests after a failed resume: got %d, want 0", got)
	}
	parent := spanByName(t, sr, ttfiMetric)
	if got := mustAttr(t, parent, "attempts").AsInt64(); got != 0 {
		t.Errorf("attempts: got %d, want 0", got)
	}
	if _, ok := attrValue(t, parent, "instruction_ms"); ok {
		t.Error("instruction_ms set after a failed resume, want absent")
	}
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

// The router resolves the actor from ate-target-actor and nothing else: an
// absent or misformatted value is a 404 "invalid actor reference" no matter
// what the request's Host says. Asserted through substrate's own parser rather
// than by string compare, so this fails if the wire format moves again.
func TestAddressesTheActorWithTheTargetActorHeader(t *testing.T) {
	srv := &pingServer{}
	u, _ := newRunWorkloadTestUser(t, srv, dynconfig.Config{}, &fakeControlClient{})

	if _, _, err := u.firstInstruction(context.Background(), 5*time.Second); err != nil {
		t.Fatalf("firstInstruction failed: %v", err)
	}

	got := srv.RecordedTargetActor()
	if got == "" {
		t.Fatal("no ate-target-actor header sent; the router would 404 this request")
	}
	ref, err := atenet.ParseTargetActor(got)
	if err != nil {
		t.Fatalf("router would reject %q: %v", got, err)
	}
	if ref.Atespace != u.cfg.Atespace || ref.Name != u.actorName {
		t.Errorf("target actor: got %s/%s, want %s/%s",
			ref.Atespace, ref.Name, u.cfg.Atespace, u.actorName)
	}
}

// errActorBusy stands in for the control plane refusing an actor that is still
// in a transitional state. Suspend and delete fail together then, which is the
// pair that leaves an actor holding a worker after the run.
var errActorBusy = errors.New("actor is not SUSPENDED")

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
			u, _ := newRunWorkloadTestUser(t, &pingServer{}, dynconfig.Config{}, tc.ctrl)
			rt := &runWorkloadRuntime{cfg: u.cfg, workload: u.workload}
			rt.users.Store(int64(1), u)

			var buf bytes.Buffer
			prev := slog.Default()
			slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, nil)))
			t.Cleanup(func() { slog.SetDefault(prev) })

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
