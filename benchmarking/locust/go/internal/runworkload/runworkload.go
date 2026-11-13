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

// Package runworkload drives a workload actor through substrate's actor
// lifecycle: each VU creates one actor and then cycles it resume/suspend for
// the rest of the run, removing it at teardown.
//
// Every control-plane call reports its own row and span through measure.
// On top of them each cycle reports TTFI: the wait from having a suspended
// actor to that actor serving an instruction. See measureTTFI for what the
// window includes. It also reports CycleTime, the whole cycle from the resume
// to the suspend returning; see cycle.
//
// The workload the actor runs is a parameter: --workload-name selects an
// implementation of workload.Workload (see internal/workload) and
// --workload-template overrides the ActorTemplate it names. Both flags are
// declared by benchmarking/locust/go/cmd/boomer-worker and arrive through New,
// resolved by scenario.Setup.
package runworkload

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"sync"
	"time"

	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/harness"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/measure"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/scenario"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/workload"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
)

const (
	// Name is the --user-class value that selects this user class.
	Name = "runworkload"

	// UserClass is the Locust class name from master-image/tests/runworkload.py;
	// must match boomer.Task.Name.
	UserClass = "RunWorkloadUser"

	ttfiMetric          = "TTFI"
	cycleTimeMetric     = "CycleTime"
	executionTimeMetric = "ExecutionTime"
	writeRAMMetric      = "WriteRAM"
	writeDiskMetric     = "WriteDisk"
	recoverMetric       = "Recover"

	// Transport label for TTFI, CycleTime and Recover. Their windows span
	// several calls, so no one transport label describes them.
	runWorkloadMethod = "runworkload"
)

// Config are the settings of a run: the ones every scenario shares, and the
// cycle's own.
type Config struct {
	scenario.Common

	// SuspendDisabled controls whether the suspend phase is skipped during the benchmark.
	SuspendDisabled bool

	// WaitForWorker has a resume that finds no free worker wait for one, the
	// wait part of TTFI and reported as SlotWait, rather than fail the cycle.
	WaitForWorker bool

	// InstructionsPerCycle is how many instructions each cycle sends between
	// its resume and its suspend, the first one closing TTFI; below 1 is 1.
	// More model a session of several turns per wake.
	InstructionsPerCycle int

	// ConcurrentInstructions is how many instructions are sent to the actor
	// at once at each of those steps; below 1 is 1. TTFI closes once all of
	// the first step's are served.
	ConcurrentInstructions int

	// GluttonWriteRAM, a size such as "64Mi", has every glutton cycle write
	// that much of the actor's memory after its instructions, so the suspend
	// snapshots dirtied memory. GluttonWriteDisk does the same with that many
	// bytes of a file. Unset for none.
	GluttonWriteRAM  string
	GluttonWriteDisk int
}

// New returns a boomer-compatible task function running c, plus a shutdown
// hook to run before exit.
func New(c Config) (task func(), shutdown func(context.Context)) {
	if c.Config.Tracer == nil {
		c.Config.Tracer = otel.Tracer("substrate-boomer/runworkload")
	}
	return harness.New(NewScenario(c, UserClass), harness.Options{
		Name:      Name,
		UserClass: UserClass,
		MinWait:   c.MinWait,
		MaxWait:   c.MaxWait,
	})
}

// NewScenario is the scenario New runs, its rows reported under userClass,
// for a user class that runs the same cycle on a pacing of its own. c's
// Tracer has to be set.
func NewScenario(c Config, userClass string) harness.Scenario {
	c.InstructionsPerCycle = max(c.InstructionsPerCycle, 1)
	c.ConcurrentInstructions = max(c.ConcurrentInstructions, 1)
	if _, ok := c.Workload.(workload.Loader); !ok && (c.GluttonWriteRAM != "" || c.GluttonWriteDisk > 0) {
		slog.Error("fatal: --glutton-write-ram and --glutton-write-disk need a workload that implements workload.Loader",
			slog.String("workload", c.Workload.Name()))
		os.Exit(1)
	}
	if _, ok := c.Workload.(workload.PerActor); ok && c.ConcurrentInstructions > 1 {
		slog.Error("fatal: --concurrent-instructions needs a workload that keeps no state per actor",
			slog.String("workload", c.Workload.Name()))
		os.Exit(1)
	}
	provider, err := c.NewProvider(recorder(c, userClass), c.SuspendDisabled, c.WaitForWorker)
	if err != nil {
		slog.Error("fatal: cannot initialize k8s client for agent-sandbox", slog.String("err", err.Error()))
		os.Exit(1)
	}
	return &runWorkloadScenario{c: c, userClass: userClass, provider: provider}
}

// recorder reports the run's calls, the sandboxes' and the users' alike.
func recorder(c Config, userClass string) measure.Recorder {
	return measure.Recorder{
		Tracer:       c.Config.Tracer,
		UserClass:    userClass,
		Timeout:      c.Timeout,
		WindowMethod: runWorkloadMethod,
	}
}

// runWorkloadScenario starts each VU's user on an actor of its own.
type runWorkloadScenario struct {
	c         Config
	userClass string
	provider  sandbox.Provider
}

func (s *runWorkloadScenario) Start(ctx context.Context) (harness.Session, error) {
	sb, err := s.provider.Create(ctx)
	if err != nil {
		return nil, err
	}
	w := s.c.Workload
	if p, ok := w.(workload.PerActor); ok {
		w = p.ForActor()
	}
	return &runWorkloadUser{
		rec:          recorder(s.c, s.userClass),
		sandbox:      sb,
		workload:     w,
		templateName: s.c.ActorTemplate(),
		resumeMode:   s.c.ResumeMode,
		steps:        max(s.c.InstructionsPerCycle, 1),
		concurrent:   max(s.c.ConcurrentInstructions, 1),
		writeRAM:     s.c.GluttonWriteRAM,
		writeDisk:    s.c.GluttonWriteDisk,
	}, nil
}

type runWorkloadUser struct {
	// rec reports the user's calls.
	rec          measure.Recorder
	sandbox      sandbox.Sandbox
	workload     workload.Workload
	templateName string
	resumeMode   string

	// steps is how many instruction steps a cycle has, and concurrent how
	// many instructions each step sends at once; zero is one.
	steps      int
	concurrent int

	// What load writes each cycle, for a workload that is a loader, and
	// whether this actor's memory has been written yet.
	writeRAM   string
	writeDisk  int
	ramWritten bool
}

// Iterate runs one cycle. The first iteration on a VU is its Start, which
// leaves the actor SUSPENDED, and every cycle leaves it that way again, so the
// actor sits suspended through the think time between iterations.
func (u *runWorkloadUser) Iterate(ctx context.Context) (string, error) {
	return u.cycle(ctx, u.resumeMode)
}

// Recover waits for the actor to settle after a failed cycle and reports the
// wait as the Recover row: a success keeps the actor, and a failure, which
// says why, has it replaced. The row counts the two apart.
func (u *runWorkloadUser) Recover(ctx context.Context) error {
	ctx, span := u.rec.Tracer.Start(ctx, recoverMetric)
	defer span.End()

	start := time.Now()
	err := u.sandbox.Settle(ctx)
	u.rec.Window(ctx, span, recoverMetric, time.Since(start), err)
	return err
}

// Close removes the actor.
func (u *runWorkloadUser) Close(ctx context.Context) error { return u.sandbox.Remove(ctx) }

func (u *runWorkloadUser) LogAttrs() []slog.Attr {
	return []slog.Attr{slog.String("actor", u.sandbox.Actor().Name)}
}

func (u *runWorkloadUser) LeftBehindAttrs() []slog.Attr {
	return []slog.Attr{slog.String("template", u.templateName)}
}

// cycle runs one resume/instruction/suspend cycle and reports its whole
// duration as CycleTime: the window opens where measureTTFI's does and closes
// when the suspend returns, so it holds the resume, the instruction's service
// time and the suspend together. Under implicit resume there is no ResumeActor,
// so it opens on the instruction that has the router wake the actor.
//
// Its span parents the TTFI and Suspend spans, so a trace shows the cycle as
// one unit. A failed call reports the window as a failure over the time spent
// and names that call, for the actor to be discarded.
func (u *runWorkloadUser) cycle(ctx context.Context, mode string) (call string, err error) {
	ctx, span := u.rec.Tracer.Start(ctx, cycleTimeMetric)
	defer span.End()

	start := time.Now()
	defer func() { u.rec.Window(ctx, span, cycleTimeMetric, time.Since(start), err) }()

	if err := u.measureTTFI(ctx, mode); err != nil {
		return ttfiMetric, err
	}
	for step := 1; step < u.steps; step++ {
		if err := u.executeStep(ctx); err != nil {
			return executionTimeMetric, err
		}
	}
	if call, err := u.load(ctx); err != nil {
		return call, err
	}
	if err := u.sandbox.Sleep(ctx); err != nil {
		return sandbox.SuspendMetric, err
	}
	return "", nil
}

// executeStep sends the actor u.concurrent instructions at once, each its own
// ExecutionTime row, and returns once all of them are done, with their
// errors joined.
func (u *runWorkloadUser) executeStep(ctx context.Context) error {
	if u.concurrent <= 1 {
		return u.executeInstruction(ctx)
	}
	errs := make([]error, u.concurrent)
	var wg sync.WaitGroup
	for i := range errs {
		wg.Go(func() { errs[i] = u.executeInstruction(ctx) })
	}
	wg.Wait()
	return errors.Join(errs...)
}

// load writes u.writeRAM of the actor's memory and u.writeDisk bytes of its
// disk, each its own row, so the suspend after it snapshots that much changed
// state. It returns the name of the call that failed, if one did.
func (u *runWorkloadUser) load(ctx context.Context) (string, error) {
	loader, ok := u.workload.(workload.Loader)
	if !ok {
		return "", nil
	}
	if u.writeRAM != "" {
		if err := u.rec.HTTP(ctx, writeRAMMetric, nil, func(ctx context.Context) error {
			return loader.WriteRAM(ctx, u.sandbox.Actor(), u.writeRAM, !u.ramWritten)
		}); err != nil {
			return writeRAMMetric, err
		}
		u.ramWritten = true
	}
	if u.writeDisk > 0 {
		if err := u.rec.HTTP(ctx, writeDiskMetric, nil, func(ctx context.Context) error {
			return loader.WriteDisk(ctx, u.sandbox.Actor(), u.writeDisk)
		}); err != nil {
			return writeDiskMetric, err
		}
	}
	return "", nil
}

// measureTTFI brings the actor back up, sends it one instruction, and reports
// the wait until that instruction is served as TTFI. It must be called with the
// actor suspended, which is how Iterate leaves it between iterations.
//
// Only explicit mode issues ResumeActor; implicit leaves the wake to the
// router. Either way the window closes on the served instruction, so it
// includes the instruction's service time, which ExecutionTime reports alone.
//
// A failed call reports the window as a failure over the wait it spent and
// hands the error back for the actor to be discarded.
func (u *runWorkloadUser) measureTTFI(ctx context.Context, mode string) error {
	if mode == "" {
		mode = scenario.ResumeModeExplicit
	}

	ctx, span := u.rec.Tracer.Start(ctx, ttfiMetric)
	defer span.End()
	span.SetAttributes(
		attribute.String("workload", u.workload.Name()),
		attribute.String("template", u.templateName),
		attribute.String("resume_mode", mode),
	)

	start := time.Now()
	if err := u.sandbox.Wake(ctx); err != nil {
		u.rec.Window(ctx, span, ttfiMetric, time.Since(start), err)
		return err
	}

	err := u.executeStep(ctx)
	u.rec.Window(ctx, span, ttfiMetric, time.Since(start), err)
	return err
}

// executeInstruction sends one instruction and reports it as the ExecutionTime
// row. There are no retries: the actor is resumed before this runs, so a
// command it will not serve is a fault to surface rather than a state to wait
// out. The workload's retryable verdict is therefore ignored, with one
// exception: a 421 says the router used a worker assignment that was stale
// or not live yet, and is retried as measure describes.
//
// A non-nil error leaves the actor in a state the run cannot reason about: it
// is the signal to discard the actor, not merely a failed sample.
func (u *runWorkloadUser) executeInstruction(ctx context.Context) error {
	stale := func(err error) bool { return errors.Is(err, workload.ErrStaleAssignment) }
	return u.rec.HTTP(ctx, executionTimeMetric, stale, func(ctx context.Context) error {
		_, err := u.workload.SendInstruction(ctx, u.sandbox.Actor())
		return err
	})
}
