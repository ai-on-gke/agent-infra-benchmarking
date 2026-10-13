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

package e2bworkload

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"sync"
	"testing"
	"time"

	"github.com/agent-substrate/substrate/agent-infra-benchmarking/internal/benchmarking/e2b/control"
	"github.com/agent-substrate/substrate/agent-infra-benchmarking/internal/benchmarking/e2b/envd"
)

// eventLog records the lifecycle calls both fakes make, so a test can assert
// the exact order of control-plane and envd operations a cycle performs.
type eventLog struct {
	mu     sync.Mutex
	events []string
}

func (l *eventLog) add(s string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.events = append(l.events, s)
}

func (l *eventLog) snapshot() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]string(nil), l.events...)
}

// fakeControl is a controlClient that records calls and returns injected
// results. Safe for the concurrent kills Shutdown issues.
type fakeControl struct {
	mu  sync.Mutex
	log *eventLog

	creates, pauses, resumes, kills int
	killedIDs                       []string

	createErr, pauseErr, resumeErr, killErr error

	nextCreateID int
	createToken  string

	// resumeSandbox is what Resume returns; nil models a 2xx resume with no
	// body (control.Resume yields nil, nil).
	resumeSandbox *control.Sandbox
}

func (f *fakeControl) Create(_ context.Context, _ *control.CreateRequest) (*control.Sandbox, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.creates++
	f.log.add("create")
	if f.createErr != nil {
		return nil, f.createErr
	}
	f.nextCreateID++
	tok := f.createToken
	if tok == "" {
		tok = "tok-create"
	}
	return &control.Sandbox{SandboxID: fmt.Sprintf("sb-%d", f.nextCreateID), EnvdAccessToken: tok}, nil
}

func (f *fakeControl) Pause(_ context.Context, _ string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.pauses++
	f.log.add("pause")
	return f.pauseErr
}

func (f *fakeControl) Resume(_ context.Context, _ string, _ int) (*control.Sandbox, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.resumes++
	f.log.add("resume")
	if f.resumeErr != nil {
		return nil, f.resumeErr
	}
	return f.resumeSandbox, nil
}

func (f *fakeControl) Kill(_ context.Context, sandboxID string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.kills++
	f.killedIDs = append(f.killedIDs, sandboxID)
	f.log.add("kill")
	return f.killErr
}

type builtExec struct{ sandboxID, domain, token string }

// execRecorder is the injected exec factory; it records the connection details
// every envd client is built from (so a test can prove resume refreshed them)
// and the commands executed, returning an injected result/error.
type execRecorder struct {
	mu       sync.Mutex
	log      *eventLog
	result   *envd.ExecResult
	err      error
	built    []builtExec
	commands []string
}

func (r *execRecorder) factory(sandboxID, domain, token string) execClient {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.built = append(r.built, builtExec{sandboxID, domain, token})
	return &recExec{r}
}

func (r *execRecorder) builtSnapshot() []builtExec {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]builtExec(nil), r.built...)
}

func (r *execRecorder) commandsSnapshot() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.commands...)
}

type recExec struct{ r *execRecorder }

func (e *recExec) Exec(_ context.Context, command string) (*envd.ExecResult, error) {
	e.r.mu.Lock()
	e.r.commands = append(e.r.commands, command)
	e.r.mu.Unlock()
	e.r.log.add("exec")
	if e.r.err != nil {
		return nil, e.r.err
	}
	if e.r.result != nil {
		return e.r.result, nil
	}
	return &envd.ExecResult{ExitCode: 0}, nil
}

func newTestRuntime() (*Runtime, *fakeControl, *execRecorder) {
	log := &eventLog{}
	fc := &fakeControl{log: log}
	rec := &execRecorder{log: log}
	cfg := Config{
		Control:  fc,
		Domain:   "test.e2b",
		Template: "base",
		TTL:      300,
		newExec:  rec.factory,
	}
	rt := New(cfg, Options{OpTimeout: time.Second, Command: "echo hi"})
	return rt, fc, rec
}

func mustStart(t *testing.T, rt *Runtime) *user {
	t.Helper()
	u, err := rt.startUser(context.Background())
	if err != nil {
		t.Fatalf("startUser: %v", err)
	}
	return u
}

func strptr(s string) *string { return &s }

func TestStartUserCreatesThenPausesToBaseline(t *testing.T) {
	rt, fc, rec := newTestRuntime()

	u := mustStart(t, rt)

	if fc.creates != 1 || fc.pauses != 1 {
		t.Fatalf("want 1 create + 1 setup pause, got creates=%d pauses=%d", fc.creates, fc.pauses)
	}
	if fc.kills != 0 {
		t.Fatalf("happy start should not kill, got kills=%d", fc.kills)
	}
	if u.id != "sb-1" {
		t.Fatalf("sandbox id = %q, want sb-1", u.id)
	}
	built := rec.builtSnapshot()
	if len(built) != 1 {
		t.Fatalf("want 1 envd client built, got %d", len(built))
	}
	// Domain was nil on the create response, so the base domain is used.
	if built[0] != (builtExec{"sb-1", "test.e2b", "tok-create"}) {
		t.Fatalf("envd built from %+v, want sb-1/test.e2b/tok-create", built[0])
	}
	if got := fc.log.snapshot(); !reflect.DeepEqual(got, []string{"create", "pause"}) {
		t.Fatalf("event order = %v, want [create pause]", got)
	}
}

func TestStartUserCreateErrorDoesNotPause(t *testing.T) {
	rt, fc, _ := newTestRuntime()
	fc.createErr = errors.New("boom")

	if _, err := rt.startUser(context.Background()); err == nil {
		t.Fatal("startUser should fail when create fails")
	}
	if fc.pauses != 0 || fc.kills != 0 {
		t.Fatalf("create failure should not pause or kill, got pauses=%d kills=%d", fc.pauses, fc.kills)
	}
}

func TestStartUserPauseErrorKillsSandbox(t *testing.T) {
	rt, fc, _ := newTestRuntime()
	fc.pauseErr = errors.New("pause refused")

	if _, err := rt.startUser(context.Background()); err == nil {
		t.Fatal("startUser should fail when the setup pause fails")
	}
	// The sandbox booted, so a failed baseline pause must not leak it.
	if fc.kills != 1 {
		t.Fatalf("want the booted sandbox killed once, got kills=%d", fc.kills)
	}
}

func TestCycleHappyPathOrder(t *testing.T) {
	rt, fc, rec := newTestRuntime()
	u := mustStart(t, rt)

	call, err := u.cycle(context.Background())
	if err != nil || call != "" {
		t.Fatalf("cycle = (%q, %v), want (\"\", nil)", call, err)
	}
	if fc.resumes != 1 {
		t.Fatalf("want 1 resume, got %d", fc.resumes)
	}
	if got := rec.commandsSnapshot(); len(got) != 1 || got[0] != "echo hi" {
		t.Fatalf("exec commands = %v, want [echo hi]", got)
	}
	// create, pause (setup), then the cycle: resume, exec, pause.
	want := []string{"create", "pause", "resume", "exec", "pause"}
	if got := fc.log.snapshot(); !reflect.DeepEqual(got, want) {
		t.Fatalf("event order = %v, want %v", got, want)
	}
}

func TestCycleResumeErrorStopsBeforeExec(t *testing.T) {
	rt, fc, rec := newTestRuntime()
	u := mustStart(t, rt)
	fc.resumeErr = errors.New("resume failed")

	call, err := u.cycle(context.Background())
	if err == nil || call != ttfiMetric {
		t.Fatalf("cycle = (%q, %v), want (%q, err)", call, err, ttfiMetric)
	}
	if len(rec.commandsSnapshot()) != 0 {
		t.Fatal("exec must not run after a failed resume")
	}
	// No pause after the failed resume.
	want := []string{"create", "pause", "resume"}
	if got := fc.log.snapshot(); !reflect.DeepEqual(got, want) {
		t.Fatalf("event order = %v, want %v", got, want)
	}
}

func TestCycleExecErrorReportedUnderTTFI(t *testing.T) {
	rt, _, rec := newTestRuntime()
	u := mustStart(t, rt)
	rec.err = errors.New("stream broke")

	call, err := u.cycle(context.Background())
	if err == nil || call != ttfiMetric {
		t.Fatalf("cycle = (%q, %v), want (%q, err)", call, err, ttfiMetric)
	}
}

func TestCycleNonZeroExitIsFailure(t *testing.T) {
	rt, _, rec := newTestRuntime()
	u := mustStart(t, rt)
	rec.result = &envd.ExecResult{ExitCode: 7, Stderr: "nope"}

	call, err := u.cycle(context.Background())
	if err == nil || call != ttfiMetric {
		t.Fatalf("cycle = (%q, %v), want (%q, err)", call, err, ttfiMetric)
	}
}

func TestCyclePauseErrorNamesPause(t *testing.T) {
	rt, fc, _ := newTestRuntime()
	u := mustStart(t, rt)
	// Fail only the in-cycle pause, not the setup pause that already ran.
	fc.pauseErr = errors.New("pause failed")

	call, err := u.cycle(context.Background())
	if err == nil || call != pauseMetric {
		t.Fatalf("cycle = (%q, %v), want (%q, err)", call, err, pauseMetric)
	}
}

func TestResumeRefreshesConnectionDetails(t *testing.T) {
	rt, fc, rec := newTestRuntime()
	u := mustStart(t, rt)
	// Resume hands back a rotated token and a new serving domain.
	fc.resumeSandbox = &control.Sandbox{
		SandboxID:       "sb-1",
		EnvdAccessToken: "tok-resume",
		Domain:          strptr("resumed.domain"),
	}

	if _, err := u.cycle(context.Background()); err != nil {
		t.Fatalf("cycle: %v", err)
	}
	built := rec.builtSnapshot()
	if len(built) != 2 {
		t.Fatalf("want a second envd client after resume, got %d", len(built))
	}
	if built[1] != (builtExec{"sb-1", "resumed.domain", "tok-resume"}) {
		t.Fatalf("post-resume envd built from %+v, want sb-1/resumed.domain/tok-resume", built[1])
	}
}

func TestResumeWithNoBodyKeepsDetails(t *testing.T) {
	rt, fc, rec := newTestRuntime()
	u := mustStart(t, rt)
	fc.resumeSandbox = nil // 2xx with no body

	if _, err := u.cycle(context.Background()); err != nil {
		t.Fatalf("cycle: %v", err)
	}
	if built := rec.builtSnapshot(); len(built) != 1 {
		t.Fatalf("no-body resume must not rebuild the envd client, got %d built", len(built))
	}
	if len(rec.commandsSnapshot()) != 1 {
		t.Fatal("exec should still run on the retained client")
	}
}

func TestResumePartialBodyKeepsValidToken(t *testing.T) {
	rt, fc, rec := newTestRuntime()
	u := mustStart(t, rt)
	// A resume body that carries the id but omits the token/domain must not
	// clobber the still-valid ones from create.
	fc.resumeSandbox = &control.Sandbox{SandboxID: "sb-1", EnvdAccessToken: "", Domain: nil}

	if _, err := u.cycle(context.Background()); err != nil {
		t.Fatalf("cycle: %v", err)
	}
	built := rec.builtSnapshot()
	last := built[len(built)-1]
	if last.token != "tok-create" || last.domain != "test.e2b" {
		t.Fatalf("post-resume envd built from %+v, want token tok-create / domain test.e2b retained", last)
	}
}

func TestDiscardKillsAndFreesSlot(t *testing.T) {
	rt, fc, _ := newTestRuntime()
	u := mustStart(t, rt)
	rt.users.Store(int64(7), u)

	rt.discard(context.Background(), 7, u, "Exec", errors.New("boom"))

	if fc.kills != 1 || len(fc.killedIDs) != 1 || fc.killedIDs[0] != u.id {
		t.Fatalf("discard should kill the sandbox once, got kills=%d ids=%v", fc.kills, fc.killedIDs)
	}
	if _, ok := rt.users.Load(int64(7)); ok {
		t.Fatal("discard should remove the user from the map")
	}
}

func TestShutdownKillsEverySandbox(t *testing.T) {
	rt, fc, _ := newTestRuntime()
	const n = 5
	for i := 0; i < n; i++ {
		rt.users.Store(int64(i), mustStart(t, rt))
	}

	rt.Shutdown(context.Background())

	if fc.kills != n {
		t.Fatalf("Shutdown killed %d sandboxes, want %d", fc.kills, n)
	}
}

func TestInflightEnterLeaveDrain(t *testing.T) {
	var g inflight
	if !g.enter() {
		t.Fatal("enter should admit on an open gate")
	}

	done := make(chan bool, 1)
	go func() { done <- g.drain(context.Background()) }()
	select {
	case <-done:
		t.Fatal("drain returned before the in-flight iteration left")
	case <-time.After(50 * time.Millisecond):
	}

	g.leave()
	select {
	case ok := <-done:
		if !ok {
			t.Fatal("drain should report true once drained")
		}
	case <-time.After(time.Second):
		t.Fatal("drain did not return after leave")
	}

	if g.enter() {
		t.Fatal("enter after drain should be refused")
	}
}

func TestInflightDrainTimesOut(t *testing.T) {
	var g inflight
	if !g.enter() {
		t.Fatal("enter should admit")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if g.drain(ctx) {
		t.Fatal("drain should time out while an iteration is still in flight")
	}
}
