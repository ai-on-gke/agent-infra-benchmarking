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

// Package density runs runworkload's cycle with a set number of actors in a
// cycle at once, however many VUs share them: each actor is paced to be in a
// cycle for its duty cycle's share of its time, the active actors over the
// worker's VUs, so that after each cycle a VU waits for as long as that keeps
// it, whatever the workload's turn takes. A case's variants then differ in
// their VUs alone.
//
// The VUs are the count locust last spawned the worker with, which grows step
// by step while it ramps up, so the actors cycle more often then and the
// active ones reach the set number before the VUs do; up to it, every actor
// is always in a cycle.
//
// A VU's first wait is drawn at random over one whole period instead, so the
// VUs' cycles spread out over it rather than keep in step with their spawn.
//
// An actor that never suspends has a cycle of its instructions alone, so a
// baseline of them is paced as if its cycle took the resume and suspend it
// skips too, and sends instructions at the rate of the actors it is compared
// with.
//
// A resume that finds every worker full waits for one to free up, as the
// router waits with a request, rather than fail the cycle. The wait is part of
// TTFI and is reported on its own as SlotWait, so its count is how many cycles
// waited and its percentiles how long.
package density

import (
	"context"
	"log/slog"
	"math/rand/v2"
	"os"
	"sync/atomic"
	"time"

	"github.com/myzhan/boomer"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/harness"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/runworkload"
	"go.opentelemetry.io/otel"
)

const (
	// Name is the --user-class value that selects this user class.
	Name = "density"

	// UserClass is the Locust class name from master-image/tests/density.py;
	// must match boomer.Task.Name.
	UserClass = "DensityUser"
)

// Config are the settings of a run.
type Config struct {
	// Cycle is the cycle each VU runs. Its MinWait and MaxWait are only the
	// wait after a start that failed.
	Cycle runworkload.Config

	// ActiveActors is how many of this worker's actors are in a cycle at
	// once, at least 1. A run on several workers splits its VUs among them,
	// so each is given its share.
	ActiveActors int

	// SkippedResumeSuspend is the resume and suspend a cycle with
	// Cycle.SuspendDisabled skips, which the pacing counts as part of it.
	SkippedResumeSuspend time.Duration
}

// New returns a boomer-compatible task function running c, plus a shutdown
// hook to run before exit.
func New(c Config) (task func(), shutdown func(context.Context)) {
	if c.ActiveActors < 1 {
		slog.Error("fatal: --active-actors must be at least 1", slog.Int("active_actors", c.ActiveActors))
		os.Exit(1)
	}
	if c.SkippedResumeSuspend < 0 || (c.SkippedResumeSuspend > 0 && !c.Cycle.SuspendDisabled) {
		slog.Error("fatal: --skipped-resume-suspend must not be negative, and is for --suspend-disabled only",
			slog.Duration("skipped_resume_suspend", c.SkippedResumeSuspend))
		os.Exit(1)
	}
	if c.Cycle.Config.Tracer == nil {
		c.Cycle.Config.Tracer = otel.Tracer("substrate-boomer/density")
	}
	c.Cycle.WaitForWorker = true
	s := &densityScenario{
		cycle:   runworkload.NewScenario(c.Cycle, UserClass),
		active:  c.ActiveActors,
		skipped: c.SkippedResumeSuspend,
	}
	if err := s.followSpawns(); err != nil {
		slog.Error("fatal: cannot follow locust's spawns", slog.String("err", err.Error()))
		os.Exit(1)
	}
	return harness.New(s, harness.Options{
		Name:      Name,
		UserClass: UserClass,
		MinWait:   c.Cycle.MinWait,
		MaxWait:   c.Cycle.MaxWait,
	})
}

// densityScenario starts runworkload's sessions, paced.
type densityScenario struct {
	cycle   harness.Scenario
	active  int
	skipped time.Duration

	// users is the VUs locust last spawned the worker with.
	users atomic.Int64
}

func (s *densityScenario) Start(ctx context.Context) (harness.Session, error) {
	session, err := s.cycle.Start(ctx)
	if err != nil {
		return nil, err
	}
	return &pacedSession{Session: session, dutyCycle: s.dutyCycle, skipped: s.skipped, random: rand.Float64}, nil
}

// followSpawns keeps users at the count of each spawn locust sends.
func (s *densityScenario) followSpawns() error {
	return boomer.Events.Subscribe(boomer.EVENT_SPAWN, func(users int, _ float64) { s.users.Store(int64(users)) })
}

// dutyCycle is the share of its time each actor is in a cycle: the active
// actors over the VUs, and all of it while they are no more.
func (s *densityScenario) dutyCycle() float64 {
	return min(1, float64(s.active)/float64(max(s.users.Load(), 1)))
}

// pacedSession is one VU's cycles, paced by its duty cycle.
type pacedSession struct {
	harness.Session

	// dutyCycle is the share of its time the actor is to be in a cycle, read
	// after each one, above 0 and at most 1.
	dutyCycle func() float64

	// skipped is the resume and suspend each cycle skips, paced as if it ran.
	skipped time.Duration

	// random is the share of a period the first wait takes, in [0, 1).
	random func() float64

	staggered bool
	wait      time.Duration

	// full is how long the last cycle that went through took.
	full time.Duration
}

// Iterate runs one cycle and sets the wait after it from how long it took. A
// failed one is paced as if it took at least as long as the last that went
// through: a refused call ends it in milliseconds, and the VU would otherwise
// try again almost at once.
func (s *pacedSession) Iterate(ctx context.Context) (string, error) {
	start := time.Now()
	call, err := s.Session.Iterate(ctx)
	busy := time.Since(start)
	paced := busy
	if err == nil {
		s.full = busy
	} else {
		paced = max(busy, s.full)
	}
	s.wait = s.next(paced, busy)
	return call, err
}

// next is the wait after a cycle that took busy, paced as one that took paced:
// the rest of its period, (paced + skipped) / dutyCycle, or after the VU's
// first cycle a random share of a whole one.
func (s *pacedSession) next(paced, busy time.Duration) time.Duration {
	period := time.Duration(float64(paced+s.skipped) / s.dutyCycle())
	if !s.staggered {
		s.staggered = true
		return time.Duration(s.random() * float64(period))
	}
	return period - busy
}

// Wait is the wait after the last cycle, and none before the first.
func (s *pacedSession) Wait() time.Duration { return s.wait }
