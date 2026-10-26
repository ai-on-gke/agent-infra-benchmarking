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

package density

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/harness"
)

var errCycle = errors.New("resume refused")

// fakeSession is a cycle that takes busy and fails with err.
type fakeSession struct {
	harness.Session
	busy time.Duration
	err  error
}

func (f *fakeSession) Iterate(context.Context) (string, error) {
	time.Sleep(f.busy)
	if f.err != nil {
		return "Resume", f.err
	}
	return "", nil
}

func newPaced(dutyCycle, random float64) *pacedSession {
	return &pacedSession{dutyCycle: dutyCycle, random: func() float64 { return random }}
}

// The first wait is a random share of one whole period, so that VUs spawned
// together spread their cycles over it; every later one is the rest of its
// period, so the actor spends dutyCycle of its time in a cycle.
func TestNextStaggersTheFirstWaitThenKeepsTheDutyCycle(t *testing.T) {
	s := newPaced(0.25, 0.5)

	if got, want := s.next(time.Second), 2*time.Second; got != want {
		t.Errorf("first wait: got %v, want %v, half of a 4s period", got, want)
	}
	if got, want := s.next(time.Second), 3*time.Second; got != want {
		t.Errorf("second wait: got %v, want %v, the rest of a 4s period", got, want)
	}
	if got, want := s.next(2*time.Second), 6*time.Second; got != want {
		t.Errorf("after a 2s cycle: got %v, want %v, the rest of an 8s period", got, want)
	}
}

// At a duty cycle of 1 the actor is always in a cycle, so after the first
// there is no wait.
func TestAFullDutyCycleDoesNotWait(t *testing.T) {
	s := newPaced(1, 0)
	s.next(time.Second)
	if got := s.next(time.Second); got != 0 {
		t.Errorf("got %v, want 0", got)
	}
}

// Wait is none before the first cycle, and after each cycle, failed or not,
// paced on the time that cycle took.
func TestIterateSetsTheWaitAfterTheCycle(t *testing.T) {
	inner := &fakeSession{busy: 20 * time.Millisecond}
	s := &pacedSession{Session: inner, dutyCycle: 0.5, random: func() float64 { return 0 }, staggered: true}

	if got := s.Wait(); got != 0 {
		t.Fatalf("before the first cycle: got %v, want 0", got)
	}
	if _, err := s.Iterate(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := s.Wait(); got < inner.busy || got > 4*inner.busy {
		t.Errorf("after a %v cycle at half duty: got %v, want about %v", inner.busy, got, inner.busy)
	}

	inner.err = errCycle
	if call, err := s.Iterate(context.Background()); call != "Resume" || !errors.Is(err, errCycle) {
		t.Errorf("a failed cycle: got %q, %v; want its call and error passed on", call, err)
	}
	if got := s.Wait(); got < inner.busy {
		t.Errorf("after a failed cycle: got %v, want it paced on the %v it took", got, inner.busy)
	}
}

// pacedSession is what the harness paces by.
var _ harness.Pacer = (*pacedSession)(nil)
