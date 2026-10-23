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

package harness

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"
)

var (
	errIterate = errors.New("iteration refused")
	errClose   = errors.New("actor is not SUSPENDED")
)

// fakeScenario starts fakeSessions and counts what the runtime does with
// them. Each field set before the run applies to every session.
type fakeScenario struct {
	startErr error
	// iterateErr fails every iteration, naming the call "FakeCall".
	iterateErr error
	// iterateUntilDone holds each iteration until its context ends.
	iterateUntilDone bool
	closeErr         error
	// closeDelay holds each Close open, so a teardown test can see how many
	// run at once.
	closeDelay time.Duration

	mu         sync.Mutex
	starts     int
	iterations int
	iterating  bool
	closes     int
	inFlight   int
	peak       int
}

func (f *fakeScenario) Start(context.Context) (Session, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.starts++
	if f.startErr != nil {
		return nil, f.startErr
	}
	return f.newSession(), nil
}

// newSession is a session for f, started without going through the runtime.
func (f *fakeScenario) newSession() *fakeSession {
	return &fakeSession{f: f, name: fmt.Sprintf("actor-%d", f.starts)}
}

// counts reads the counters under the lock.
func (f *fakeScenario) counts() (starts, iterations, closes int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.starts, f.iterations, f.closes
}

type fakeSession struct {
	f    *fakeScenario
	name string
}

func (s *fakeSession) Iterate(ctx context.Context) (string, error) {
	f := s.f
	f.mu.Lock()
	f.iterations++
	f.iterating = true
	f.mu.Unlock()
	if f.iterateUntilDone {
		<-ctx.Done()
		return "FakeCall", ctx.Err()
	}
	if f.iterateErr != nil {
		return "FakeCall", f.iterateErr
	}
	return "", nil
}

func (s *fakeSession) Close(context.Context) error {
	f := s.f
	f.mu.Lock()
	f.closes++
	f.inFlight++
	f.peak = max(f.peak, f.inFlight)
	f.mu.Unlock()

	// Outside the lock, so concurrent callers actually overlap here.
	time.Sleep(f.closeDelay)

	f.mu.Lock()
	defer f.mu.Unlock()
	f.inFlight--
	return f.closeErr
}

func (s *fakeSession) LogAttrs() []slog.Attr {
	return []slog.Attr{slog.String("actor", s.name)}
}

func (s *fakeSession) LeftBehindAttrs() []slog.Attr {
	return []slog.Attr{slog.String("template", "fake-template")}
}

// captureLogs points slog at a buffer for the duration of one test.
func captureLogs(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return &buf
}

func newTestTask(f *fakeScenario) (task func(), shutdown func(context.Context)) {
	return New(f, Options{Name: "fake", UserClass: "FakeUser"})
}

// The first iteration on a goroutine starts the session; every later one
// iterates it, so the run settles into iterations rather than starting a
// session per iteration.
func TestTaskStartsOnceThenIterates(t *testing.T) {
	f := &fakeScenario{}
	task, _ := newTestTask(f)

	task()
	if starts, iterations, _ := f.counts(); starts != 1 || iterations != 0 {
		t.Fatalf("after the first iteration: got %d starts and %d iterations, want 1 and 0", starts, iterations)
	}

	task()
	task()

	if starts, iterations, _ := f.counts(); starts != 1 || iterations != 2 {
		t.Errorf("after three iterations: got %d starts and %d iterations, want 1 and 2", starts, iterations)
	}
}

// A VU whose start failed has nothing to iterate, so its next iteration tries
// the start again.
func TestAFailedStartIsRetried(t *testing.T) {
	f := &fakeScenario{startErr: errIterate}
	task, _ := newTestTask(f)
	buf := captureLogs(t)

	task()
	task()

	if starts, iterations, _ := f.counts(); starts != 2 || iterations != 0 {
		t.Errorf("got %d starts and %d iterations, want 2 and 0", starts, iterations)
	}
	if !strings.Contains(buf.String(), "fake on_start failed; goroutine will retry next iter") {
		t.Errorf("the failed start was not logged; got:\n%s", buf.String())
	}
}

// boomer.Run returns while iterations are still in flight, so the teardown
// drains the task function first and the gate stays shut afterwards. An
// iteration admitted past that point would start a session the walk has
// already passed, which is how an actor leaks.
func TestTaskIsGatedByTheTeardownDrain(t *testing.T) {
	f := &fakeScenario{}
	task, shutdown := newTestTask(f)

	shutdown(context.Background())
	task()

	if starts, _, _ := f.counts(); starts != 0 {
		t.Errorf("starts after the teardown drained: got %d, want 0", starts)
	}
}

// A failed iteration leaves the session in a state nothing reconciles, so it
// is closed and the goroutine's slot freed. The next iteration starts a new
// session instead of working against the broken one for the rest of the run.
func TestAFailedIterationDiscardsTheSession(t *testing.T) {
	f := &fakeScenario{iterateErr: errIterate}
	task, _ := newTestTask(f)
	buf := captureLogs(t)

	task() // starts
	task() // fails, discards

	if _, _, closes := f.counts(); closes != 1 {
		t.Errorf("closes: got %d, want 1", closes)
	}
	got := buf.String()
	for _, want := range []string{"discarding the actor after a failed call", `"actor":"actor-1"`, `"call":"FakeCall"`, errIterate.Error()} {
		if !strings.Contains(got, want) {
			t.Errorf("discard log missing %q; got:\n%s", want, got)
		}
	}

	// The slot being free is what lets the next iteration start over.
	task()
	if starts, _, _ := f.counts(); starts != 2 {
		t.Errorf("starts after the discard: got %d, want 2 — the discarded session still holds its goroutine slot", starts)
	}
}

// The slot has to be freed even when the session could not be closed, or the
// VU keeps using an actor it has already given up on. The close failing is
// named instead, because that actor holds a worker until someone removes it.
func TestADiscardThatCouldNotCloseStillFreesTheSlot(t *testing.T) {
	f := &fakeScenario{iterateErr: errIterate, closeErr: errClose}
	task, _ := newTestTask(f)
	buf := captureLogs(t)

	task()
	task()

	got := buf.String()
	for _, want := range []string{"discarded actor was left behind", `"actor":"actor-1"`, `"template":"fake-template"`, errClose.Error()} {
		if !strings.Contains(got, want) {
			t.Errorf("a leaked actor was not named: log missing %q; got:\n%s", want, got)
		}
	}
	task()
	if starts, _, _ := f.counts(); starts != 2 {
		t.Errorf("starts after the discard: got %d, want 2 — the slot was kept after a failed close", starts)
	}
}

// An actor that outlives the run is invisible until someone goes looking for
// it, so teardown has to say which ones it could not remove and why.
func TestShutdownReportsWhatItLeftBehind(t *testing.T) {
	tests := []struct {
		name      string
		closeErr  error
		wantInLog []string
	}{
		{
			name:      "a clean teardown",
			wantInLog: []string{"teardown removed an actor", `"actor":"actor-1"`, `"left":0`, `"removed":1`},
		},
		{
			name:      "a session that could not be closed",
			closeErr:  errClose,
			wantInLog: []string{"teardown left an actor behind", `"actor":"actor-1"`, `"template":"fake-template"`, errClose.Error(), `"left":1`},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeScenario{closeErr: tc.closeErr}
			task, shutdown := newTestTask(f)
			task()
			buf := captureLogs(t)

			shutdown(context.Background())

			got := buf.String()
			for _, want := range tc.wantInLog {
				if !strings.Contains(got, want) {
					t.Errorf("teardown log missing %q; got:\n%s", want, got)
				}
			}
		})
	}
}

// A run's worth of sessions closed one at a time would outlast the budget the
// caller allows, so they go together — and the cap is what keeps that from
// becoming a burst of its own against an ateapi still draining the run.
func TestShutdownClosesSessionsConcurrently(t *testing.T) {
	const sessions = 50

	f := &fakeScenario{closeDelay: 10 * time.Millisecond}
	r := newRuntime(f, Options{})
	for i := range sessions {
		r.sessions.Store(int64(i), f.newSession())
	}

	r.shutdown(context.Background())

	// An actor left behind holds a worker until someone removes it by hand, so
	// the teardown has to reach every session however it is scheduled.
	if _, _, closes := f.counts(); closes != sessions {
		t.Errorf("closes: got %d, want %d", closes, sessions)
	}
	f.mu.Lock()
	peak := f.peak
	f.mu.Unlock()
	if peak < 2 {
		t.Errorf("peak concurrent closes: got %d, want > 1 — the teardown ran serially", peak)
	}
	if peak > teardownConcurrency {
		t.Errorf("peak concurrent closes: got %d, want <= %d", peak, teardownConcurrency)
	}
}

func TestDynamicWait(t *testing.T) {
	r := &runtime{opts: Options{MinWait: 0, MaxWait: 0}}
	if got := r.dynamicWait(); got != 0 {
		t.Errorf("dynamicWait with no bounds: got %v, want 0", got)
	}
}

// An iteration still in flight when the run ends is cut off, not waited for,
// and is not a reason to discard the session: the teardown closes it.
func TestTheEndOfTheRunCutsOffAnIterationWithoutADiscard(t *testing.T) {
	f := &fakeScenario{iterateUntilDone: true}
	task, shutdown := newTestTask(f)
	buf := captureLogs(t)

	// Sessions are keyed by goroutine, so both iterations run on this one: the
	// first starts the session, the second iterates it until the run ends.
	done := make(chan struct{})
	go func() {
		defer close(done)
		task()
		task()
	}()
	for {
		f.mu.Lock()
		iterating := f.iterating
		f.mu.Unlock()
		if iterating {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}

	start := time.Now()
	shutdown(context.Background())
	<-done

	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Errorf("teardown took %v; the iteration should have been cut off", elapsed)
	}
	if _, _, closes := f.counts(); closes != 1 {
		t.Errorf("closes: got %d, want the teardown's one", closes)
	}
	if strings.Contains(buf.String(), "discarding the actor") {
		t.Errorf("the cut-off session was discarded; log:\n%s", buf.String())
	}
}
