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

	"github.com/myzhan/boomer"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/harness"
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
	return &pacedSession{dutyCycle: func() float64 { return dutyCycle }, random: func() float64 { return random }}
}

// The VUs are the count of the last spawn boomer publishes.
func TestUsersFollowTheSpawns(t *testing.T) {
	s := &densityScenario{active: 80}
	if err := s.followSpawns(); err != nil {
		t.Fatal(err)
	}

	for _, users := range []int{10, 250} {
		boomer.Events.Publish(boomer.EVENT_SPAWN, users, float64(users))
		if got := s.users.Load(); got != int64(users) {
			t.Errorf("after a spawn of %d: got %d", users, got)
		}
	}
}

// The duty cycle is the active actors over the VUs locust last spawned, so it
// follows them while they ramp up, and every actor cycles all of its time
// while they are no more than the active ones.
func TestTheDutyCycleIsTheActiveActorsOverTheVUs(t *testing.T) {
	s := &densityScenario{active: 80}
	for _, tc := range []struct {
		users int64
		want  float64
	}{
		{0, 1},
		{40, 1},
		{80, 1},
		{100, 0.8},
		{250, 0.32},
		{500, 0.16},
	} {
		s.users.Store(tc.users)
		if got := s.dutyCycle(); got != tc.want {
			t.Errorf("80 active actors among %d VUs: got %v, want %v", tc.users, got, tc.want)
		}
	}
}

// The first wait is a random share of one whole period, so that VUs spawned
// together spread their cycles over it; every later one is the rest of its
// period, so the actor spends dutyCycle of its time in a cycle.
func TestNextStaggersTheFirstWaitThenKeepsTheDutyCycle(t *testing.T) {
	s := newPaced(0.25, 0.5)

	if got, want := s.next(time.Second, time.Second), 2*time.Second; got != want {
		t.Errorf("first wait: got %v, want %v, half of a 4s period", got, want)
	}
	if got, want := s.next(time.Second, time.Second), 3*time.Second; got != want {
		t.Errorf("second wait: got %v, want %v, the rest of a 4s period", got, want)
	}
	if got, want := s.next(2*time.Second, 2*time.Second), 6*time.Second; got != want {
		t.Errorf("after a 2s cycle: got %v, want %v, the rest of an 8s period", got, want)
	}
}

// An actor that never suspends is paced as if its cycle took the resume and
// suspend it skips: the skipped time is waited out as part of its period.
func TestNextCountsTheSkippedResumeAndSuspend(t *testing.T) {
	s := newPaced(0.5, 0)
	s.skipped = 750 * time.Millisecond
	s.next(250*time.Millisecond, 250*time.Millisecond)

	if got, want := s.next(250*time.Millisecond, 250*time.Millisecond), 1750*time.Millisecond; got != want {
		t.Errorf("got %v, want %v, the rest of a 2s period, (250ms + 750ms) / 0.5", got, want)
	}
}

// At a duty cycle of 1 the actor is always in a cycle, so after the first
// there is no wait.
func TestAFullDutyCycleDoesNotWait(t *testing.T) {
	s := newPaced(1, 0)
	s.next(time.Second, time.Second)
	if got := s.next(time.Second, time.Second); got != 0 {
		t.Errorf("got %v, want 0", got)
	}
}

// Wait is none before the first cycle, and after each cycle paced on the time
// it took.
func TestIterateSetsTheWaitAfterTheCycle(t *testing.T) {
	inner := &fakeSession{busy: 20 * time.Millisecond}
	s := newPaced(0.5, 0)
	s.Session, s.staggered = inner, true

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

// A cycle a refused resume ends at once is paced on the last full one's
// period, so the VU does not try again at once; before any has gone through,
// it is paced on its own.
func TestAFailedCycleIsPacedOnTheLastFullOne(t *testing.T) {
	const full = 40 * time.Millisecond
	inner := &fakeSession{err: errCycle}
	s := newPaced(0.5, 0)
	s.Session, s.staggered = inner, true

	s.Iterate(context.Background())
	if got := s.Wait(); got > full/2 {
		t.Errorf("a failed first cycle: got %v, want it paced on the moment it took", got)
	}

	inner.busy, inner.err = full, nil
	s.Iterate(context.Background())
	inner.busy, inner.err = 0, errCycle
	s.Iterate(context.Background())
	if got, want := s.Wait(), 2*full; got < want-full/2 || got > want+full/2 {
		t.Errorf("a failed cycle after a %v one at half duty: got %v, want about its %v period", full, got, want)
	}
}

// pacedSession is what the harness paces by.
var _ harness.Pacer = (*pacedSession)(nil)
