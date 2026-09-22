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
	"net/url"
	"reflect"
	"strconv"
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

// newRunWorkloadTestUser builds a substrate-backed user whose spans are
// recorded, returning both.
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

// substrateActor returns the actorRef u's substrate backend addresses, and
// fails the test if u was not built with one.
func substrateActor(t *testing.T, u *runWorkloadUser) actorRef {
	t.Helper()
	b, ok := u.backend.(*substrateBackend)
	if !ok {
		t.Fatalf("user's backend is %T, want *substrateBackend", u.backend)
	}
	return b.actor()
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

			u.measure(context.Background(), warmStartMetric, Options{}, false, func(ctx context.Context) (actorRef, time.Duration, error) {
				return u.backend.warm(ctx, dyn)
			})

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

	attempts, _, err := u.firstInstruction(context.Background(), substrateActor(t, u), 5*time.Second)
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

			attempts, _, err := u.firstInstruction(context.Background(), substrateActor(t, u), 5*time.Second)
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
	_, _, err := u.firstInstruction(context.Background(), substrateActor(t, u), 100*time.Millisecond)
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

	u.measure(context.Background(), firstResumeMetric, Options{}, true, func(ctx context.Context) (actorRef, time.Duration, error) {
		return u.backend.first(ctx, dyn)
	})

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
	if got := mustAttr(t, parent, "backend").AsString(); got != string(BackendSubstrate) {
		t.Errorf("backend: got %q, want %q", got, BackendSubstrate)
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

	u.measure(context.Background(), warmStartMetric, Options{}, false, func(ctx context.Context) (actorRef, time.Duration, error) {
		return u.backend.warm(ctx, dyn)
	})

	if got := srv.RecordedPings(); got != 0 {
		t.Errorf("ping requests after a failed resume: got %d, want 0", got)
	}
	parent := spanByName(t, sr, warmStartMetric)
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
	actor := substrateActor(t, u)

	if _, _, err := u.firstInstruction(context.Background(), actor, 5*time.Second); err != nil {
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
	if ref.Atespace != actor.Atespace || ref.Name != actor.Name {
		t.Errorf("target actor: got %s/%s, want %s/%s",
			ref.Atespace, ref.Name, actor.Atespace, actor.Name)
	}
}

// agent-sandbox addressing has no router or header to resolve the sandbox
// from — actor.baseURL() already points straight at its own Service — so
// sendHTTPInstruction must not attach the substrate-only header in that case.
func TestSendHTTPInstructionOmitsTheHeaderForAgentSandbox(t *testing.T) {
	srv := &pingServer{}
	ts := srv.Start(t)
	u, err := url.Parse(ts.URL)
	if err != nil {
		t.Fatalf("parse %q: %v", ts.URL, err)
	}
	port, err := strconv.Atoi(u.Port())
	if err != nil {
		t.Fatalf("parse port from %q: %v", ts.URL, err)
	}
	actor := actorRef{HTTPClient: ts.Client(), ServiceFQDN: u.Hostname(), Port: int32(port)}

	if _, err := sendHTTPInstruction(context.Background(), actor, httpInstruction{Method: http.MethodPost, Path: pingPath}); err != nil {
		t.Fatalf("sendHTTPInstruction failed: %v", err)
	}
	if got := srv.RecordedTargetActor(); got != "" {
		t.Errorf("ate-target-actor header sent for agent-sandbox addressing: %q, want none", got)
	}
}
