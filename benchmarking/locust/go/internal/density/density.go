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

// Package density runs runworkload's cycle paced by a duty cycle: after each
// cycle a VU waits for as long as keeps its actor in a cycle for that share
// of its time, whatever the workload's turn takes. A case then sets how many
// actors are active at once, about VUs × duty cycle, with those two alone,
// rather than with a think time tuned to each workload.
//
// A VU's first wait is drawn at random over one whole period instead, so the
// VUs' cycles spread out over it rather than keep in step with their spawn.
package density

import (
	"context"
	"log/slog"
	"math/rand/v2"
	"os"
	"time"

	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/harness"
	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/runworkload"
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

	// DutyCycle is the share of its time each actor spends in a cycle, above
	// 0 and at most 1.
	DutyCycle float64
}

// New returns a boomer-compatible task function running c, plus a shutdown
// hook to run before exit.
func New(c Config) (task func(), shutdown func(context.Context)) {
	if !(c.DutyCycle > 0 && c.DutyCycle <= 1) {
		slog.Error("fatal: --duty-cycle must be above 0 and at most 1", slog.Float64("duty_cycle", c.DutyCycle))
		os.Exit(1)
	}
	if c.Cycle.Config.Tracer == nil {
		c.Cycle.Config.Tracer = otel.Tracer("substrate-boomer/density")
	}
	return harness.New(&densityScenario{
		cycle:     runworkload.NewScenario(c.Cycle, UserClass),
		dutyCycle: c.DutyCycle,
	}, harness.Options{
		Name:      Name,
		UserClass: UserClass,
		MinWait:   c.Cycle.MinWait,
		MaxWait:   c.Cycle.MaxWait,
	})
}

// densityScenario starts runworkload's sessions, paced.
type densityScenario struct {
	cycle     harness.Scenario
	dutyCycle float64
}

func (s *densityScenario) Start(ctx context.Context) (harness.Session, error) {
	session, err := s.cycle.Start(ctx)
	if err != nil {
		return nil, err
	}
	return &pacedSession{Session: session, dutyCycle: s.dutyCycle, random: rand.Float64}, nil
}

// pacedSession is one VU's cycles, paced by its duty cycle.
type pacedSession struct {
	harness.Session
	dutyCycle float64

	// random is the share of a period the first wait takes, in [0, 1).
	random func() float64

	staggered bool
	wait      time.Duration
}

// Iterate runs one cycle and sets the wait after it from how long it took.
func (s *pacedSession) Iterate(ctx context.Context) (string, error) {
	start := time.Now()
	call, err := s.Session.Iterate(ctx)
	s.wait = s.next(time.Since(start))
	return call, err
}

// next is the wait after a cycle that took busy: the rest of its period,
// busy / dutyCycle, or after the VU's first cycle a random share of a whole
// one.
func (s *pacedSession) next(busy time.Duration) time.Duration {
	period := time.Duration(float64(busy) / s.dutyCycle)
	if !s.staggered {
		s.staggered = true
		return time.Duration(s.random() * float64(period))
	}
	return period - busy
}

// Wait is the wait after the last cycle, and none before the first.
func (s *pacedSession) Wait() time.Duration { return s.wait }
