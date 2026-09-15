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

package ttfi

import (
	"context"
	"net/http"
	"reflect"
	"testing"
	"time"

	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/dynconfig"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/userclass"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// newRecordingTracer returns a tracer whose ended spans land in the returned
// recorder, so a test can assert on the span tree the TTFI window builds.
func newRecordingTracer(t *testing.T) (trace.Tracer, *tracetest.SpanRecorder) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	return tp.Tracer("ttfi-test"), sr
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

// newTTFITestUser builds a user whose spans are recorded, returning both.
func newTTFITestUser(t *testing.T, srv *pingServer, dyn dynconfig.Config, ctrl *fakeControlClient) (*ttfiUser, *tracetest.SpanRecorder) {
	t.Helper()
	tracer, sr := newRecordingTracer(t)
	cfg := &userclass.Config{
		APIStub: ctrl,
		Tracer:  tracer,
		Dyn:     dynconfig.NewHolder(dyn),
	}
	return newTestTTFIUser(t, srv, cfg), sr
}

func TestTTFIResumeModeSelectsTheControlPlaneCall(t *testing.T) {
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
			u, _ := newTTFITestUser(t, srv, dyn, ctrl)

			u.measure(context.Background(), ttfiWarmMetric, dyn, ttfiConfig{}, false)

			if got := ctrl.recordedCalls(); !reflect.DeepEqual(got, tc.wantGRPCCall) {
				t.Errorf("gRPC calls: got %v, want %v", got, tc.wantGRPCCall)
			}
			if got := srv.RecordedPings(); got != 1 {
				t.Errorf("ping attempts: got %d, want 1", got)
			}
		})
	}
}

func TestTTFIRetriesWhileTheActorActivates(t *testing.T) {
	// Three rejections then success: a client cannot use the sandbox until
	// the fourth attempt, so all four are inside the window.
	srv := &pingServer{PingFailures: 3}
	dyn := dynconfig.Config{ResumeMode: dynconfig.ResumeModeImplicit}
	u, sr := newTTFITestUser(t, srv, dyn, &fakeControlClient{})

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

	span := spanByName(t, sr, ttfiInstructionMetric)
	if got := mustAttr(t, span, "ttfi.attempts").AsInt64(); got != 4 {
		t.Errorf("ttfi.attempts span attribute: got %d, want 4", got)
	}
}

func TestTTFIDoesNotRetryUnrecoverableFailures(t *testing.T) {
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
			u, _ := newTTFITestUser(t, tc.srv, dyn, &fakeControlClient{})

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

func TestTTFIFailsWhenTheBudgetElapses(t *testing.T) {
	// Never becomes ready, so the loop must be stopped by the budget rather
	// than run for the rest of the test.
	srv := &pingServer{PingFailures: 1 << 30}
	dyn := dynconfig.Config{ResumeMode: dynconfig.ResumeModeImplicit}
	u, _ := newTTFITestUser(t, srv, dyn, &fakeControlClient{})

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

func TestTTFIBudget(t *testing.T) {
	u := &ttfiUser{}
	if got := u.budget(ttfiConfig{}); got != defaultTTFITimeout {
		t.Errorf("unset budget: got %v, want %v", got, defaultTTFITimeout)
	}
	if got := u.budget(ttfiConfig{Timeout: 5 * time.Second}); got != 5*time.Second {
		t.Errorf("configured budget: got %v, want 5s", got)
	}
}

// The TTFI number is the parent span's duration and the phase split lives on
// its attributes, so one sampled trace has to explain its own total without
// the reader joining it to anything else.
func TestTTFISpanCarriesTheBreakdown(t *testing.T) {
	srv := &pingServer{}
	ctrl := &fakeControlClient{serverElapsedUs: "4000"} // 4ms of ateapi handler time
	dyn := dynconfig.Config{ResumeMode: dynconfig.ResumeModeExplicit}
	u, sr := newTTFITestUser(t, srv, dyn, ctrl)

	u.measure(context.Background(), ttfiFirstResumeMetric, dyn, ttfiConfig{}, true)

	parent := spanByName(t, sr, ttfiFirstResumeMetric)
	if got := mustAttr(t, parent, "ttfi.resume_mode").AsString(); got != dynconfig.ResumeModeExplicit {
		t.Errorf("ttfi.resume_mode: got %q, want %q", got, dynconfig.ResumeModeExplicit)
	}
	if got := mustAttr(t, parent, "ttfi.first_resume").AsBool(); !got {
		t.Error("ttfi.first_resume: got false, want true")
	}
	if got := mustAttr(t, parent, "ttfi.template").AsString(); got != defaultTTFITemplate {
		t.Errorf("ttfi.template: got %q, want %q", got, defaultTTFITemplate)
	}
	if got := mustAttr(t, parent, "ttfi.attempts").AsInt64(); got != 1 {
		t.Errorf("ttfi.attempts: got %d, want 1", got)
	}
	if got := mustAttr(t, parent, "ttfi.total_ms").AsFloat64(); got <= 0 {
		t.Errorf("ttfi.total_ms: got %v, want > 0", got)
	}
	if got := mustAttr(t, parent, "ttfi.instruction_ms").AsFloat64(); got <= 0 {
		t.Errorf("ttfi.instruction_ms: got %v, want > 0", got)
	}
	// The resume trailer is the server's own handler duration, so the
	// control-plane share is attributable rather than inferred.
	if got := mustAttr(t, parent, "ttfi.control_plane_ms").AsFloat64(); got != 4 {
		t.Errorf("ttfi.control_plane_ms: got %v, want 4", got)
	}

	// Both phases must hang off the TTFI parent; a detached child would put
	// the phase in a different trace from the total it belongs to.
	for _, child := range []string{ttfiResumeMetric, ttfiInstructionMetric} {
		s := spanByName(t, sr, child)
		if s.Parent().SpanID() != parent.SpanContext().SpanID() {
			t.Errorf("span %q is not a child of %q", child, ttfiFirstResumeMetric)
		}
	}
}

func TestTTFIReportsAFailedResumeWithoutSendingInstructions(t *testing.T) {
	// The window cannot be measured if the actor never gets a worker, and a
	// TTFI row built from instructions that were never sent would be a lie.
	srv := &pingServer{}
	ctrl := &fakeControlClient{resumeErr: errResumeRefused}
	dyn := dynconfig.Config{ResumeMode: dynconfig.ResumeModeExplicit}
	u, sr := newTTFITestUser(t, srv, dyn, ctrl)

	u.measure(context.Background(), ttfiWarmMetric, dyn, ttfiConfig{}, false)

	if got := srv.RecordedPings(); got != 0 {
		t.Errorf("ping requests after a failed resume: got %d, want 0", got)
	}
	parent := spanByName(t, sr, ttfiWarmMetric)
	if got := mustAttr(t, parent, "ttfi.attempts").AsInt64(); got != 0 {
		t.Errorf("ttfi.attempts: got %d, want 0", got)
	}
	if _, ok := attrValue(t, parent, "ttfi.instruction_ms"); ok {
		t.Error("ttfi.instruction_ms set after a failed resume, want absent")
	}
}

func TestTTFIUserClassIsRegistered(t *testing.T) {
	entry, ok := userclass.Lookup("ttfi")
	if !ok {
		t.Fatalf("ttfi user class not registered; have %v", userclass.Names())
	}
	// The master's spawn messages match on the Python class name, so a
	// mismatch here starts no users at all.
	if entry.UserClass != ttfiUserClass {
		t.Errorf("UserClass: got %q, want %q", entry.UserClass, ttfiUserClass)
	}
	if entry.LocustFile != "ttfi.py" {
		t.Errorf("LocustFile: got %q, want %q", entry.LocustFile, "ttfi.py")
	}
}
