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

// Package harness is the VU runtime a user class runs on, whatever its VUs do
// per iteration: each VU starts a session on its first iteration and runs one
// iteration of it per call after that, with think time in between. A session
// whose iteration failed is kept if it recovers, and otherwise discarded for
// the next iteration to start a fresh one; at teardown every session the run
// still holds is closed.
package harness

import (
	"context"
	"log/slog"
	"math/rand/v2"
	"slices"
	"sync"
	"sync/atomic"
	"time"

	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/measure"
	"go.ate.dev/substrate/internal/benchmarking/boomer/boomerutil"
	bmetrics "go.ate.dev/substrate/internal/benchmarking/boomer/metrics"
)

const (
	// Caps the teardown's concurrent closes. Every VU's session is closed at
	// once, so an unbounded run would open one close per session against a
	// control plane still draining the run's own traffic.
	teardownConcurrency = 16

	// drainTimeout bounds the wait for in-flight iterations at the start of
	// the teardown. Well under the budget the caller allows for the whole
	// teardown, so that the sessions already started are still closed when an
	// iteration is wedged rather than merely slow.
	drainTimeout = 35 * time.Second

	// How long a VU refused by the teardown waits before boomer calls it again.
	// Boomer keeps the VUs looping until the teardown returns, and without a
	// wait they spin against the teardown's own calls.
	refusedWait = 100 * time.Millisecond
)

// Scenario is what a user class's VUs do.
type Scenario interface {
	// Start is the VU's on_start, called on its first iteration.
	Start(ctx context.Context) (Session, error)
}

// Session is one VU's state between iterations.
type Session interface {
	// Iterate runs one task iteration; a non-nil error names the call that
	// failed, and the session is kept only if Recover then succeeds.
	Iterate(ctx context.Context) (call string, err error)

	// Recover brings the session back to where the next iteration can start
	// after a failed one. An error discards it.
	Recover(ctx context.Context) error

	// Close removes what the session holds; called on discard and at teardown.
	Close(ctx context.Context) error

	// LogAttrs name the session on every log line about it.
	LogAttrs() []slog.Attr

	// LeftBehindAttrs follow them on a line saying Close failed, for whoever
	// has to remove by hand what the session held.
	LeftBehindAttrs() []slog.Attr
}

// Pacer is a Session that sets its own think time, in place of Options'
// MinWait and MaxWait.
type Pacer interface {
	// Wait is the think time before the session's next iteration.
	Wait() time.Duration
}

// Options are the runtime's settings.
type Options struct {
	// Name is the user class's registered name, which log lines give it by.
	Name string

	// UserClass is the locust class name the user count is reported under.
	UserClass string

	// MinWait and MaxWait bound the think time between iterations. An upper
	// bound at or below the lower one waits MinWait exactly.
	MinWait time.Duration
	MaxWait time.Duration
}

// New returns a boomer-compatible task function running s, plus a shutdown
// hook to run before exit.
func New(s Scenario, opts Options) (task func(), shutdown func(context.Context)) {
	r := newRuntime(s, opts)
	return r.iterate, r.shutdown
}

type runtime struct {
	scenario Scenario
	opts     Options
	sessions sync.Map // goroutineID -> Session

	// Gates the task function so the teardown can wait for the iterations
	// boomer leaves running when it returns. See inflight.go.
	inflight inflight

	// The context every iteration's calls run under, cancelled with
	// measure.ErrRunEnded when the teardown starts.
	run    context.Context
	endRun context.CancelCauseFunc
}

func newRuntime(s Scenario, opts Options) *runtime {
	r := &runtime{scenario: s, opts: opts}
	r.run, r.endRun = context.WithCancelCause(context.Background())
	return r
}

func (r *runtime) dynamicWait() time.Duration {
	if r.opts.MaxWait <= r.opts.MinWait {
		return r.opts.MinWait
	}
	jitter := r.opts.MaxWait - r.opts.MinWait
	return r.opts.MinWait + time.Duration(rand.Float64()*float64(jitter))
}

// wait is the think time before s's next iteration: its own if it paces
// itself, the dynamic one otherwise.
func (r *runtime) wait(s Session) time.Duration {
	if p, ok := s.(Pacer); ok {
		return p.Wait()
	}
	return r.dynamicWait()
}

// iterate is the task function boomer calls in a loop on each VU goroutine.
// The first call on a goroutine starts the session; every later one runs one
// iteration of it.
//
// The gate is taken for the whole iteration, so the teardown can tell an
// iteration that is still starting a session from one that has finished. A
// refused entry means the run is being torn down, and the only correct thing
// left to do is return: a session started past this point would not be in the
// map the teardown has already walked.
func (r *runtime) iterate() {
	if !r.inflight.enter() {
		time.Sleep(refusedWait)
		return
	}
	defer r.inflight.leave()

	ctx := r.run
	gid := boomerutil.GoroutineID()
	val, loaded := r.sessions.Load(gid)
	if !loaded {
		s, err := r.start(ctx)
		if err != nil {
			slog.Warn(r.opts.Name+" on_start failed; goroutine will retry next iter",
				slog.String("err", err.Error()))
			time.Sleep(r.dynamicWait())
			return
		}
		if _, loaded := r.sessions.LoadOrStore(gid, s); loaded {
			// Another session already holds this goroutine id, so the one just
			// started would never be reached again. Close it now rather than
			// leak it for the rest of the run.
			_ = s.Close(ctx)
			bmetrics.UpdateUsers(r.opts.UserClass, -1)
		}
		time.Sleep(r.wait(s))
		return
	}
	s := val.(Session)

	if call, err := s.Iterate(ctx); err != nil {
		// The teardown, which cut it off, closes the session itself.
		if measure.CutOff(ctx, err) {
			return
		}
		if !r.keepOrDiscard(ctx, gid, s, call, err) {
			time.Sleep(r.dynamicWait())
			return
		}
	}

	time.Sleep(r.wait(s))
}

// keepOrDiscard keeps a session whose iteration failed if it recovers, and
// discards it otherwise, reporting whether it was kept. Recovering keeps the
// run on the sessions it started, as a client would retry its own sandbox, so
// a failure that sticks to one shows as that rather than as a fresh session
// each time.
func (r *runtime) keepOrDiscard(ctx context.Context, gid int64, s Session, call string, cause error) bool {
	err := s.Recover(ctx)
	// The teardown, which cut it off, closes the session itself.
	if measure.CutOff(ctx, err) {
		return false
	}
	attrs := slices.Concat(s.LogAttrs(), []slog.Attr{slog.String("call", call), slog.String("err", cause.Error())})
	if err == nil {
		slog.LogAttrs(ctx, slog.LevelInfo, "kept the session after a failed call", attrs...)
		return true
	}
	r.discard(ctx, gid, s, append(attrs, slog.String("recover_err", err.Error())))
	return false
}

func (r *runtime) start(ctx context.Context) (Session, error) {
	bmetrics.UpdateUsers(r.opts.UserClass, 1)
	s, err := r.scenario.Start(ctx)
	if err != nil {
		bmetrics.UpdateUsers(r.opts.UserClass, -1)
		return nil, err
	}
	return s, nil
}

// discard throws away a session that did not recover: it is closed and the
// goroutine's slot freed, and the next iteration starts a fresh one rather
// than working against a broken one for the rest of the run.
//
// The slot is freed whether or not the close worked, because a VU that cannot
// get rid of its session still has to stop using it. A close that failed is
// named, since what the session held stays until someone removes it.
func (r *runtime) discard(ctx context.Context, gid int64, s Session, attrs []slog.Attr) {
	slog.LogAttrs(ctx, slog.LevelWarn, "discarding the session after a failed call", attrs...)

	if err := s.Close(ctx); err != nil {
		slog.LogAttrs(ctx, slog.LevelWarn, "discarded session was left behind", leftBehind(s, err)...)
	}
	r.sessions.Delete(gid)
	bmetrics.UpdateUsers(r.opts.UserClass, -1)
}

// leftBehind is what a line saying s's Close failed with err carries.
func leftBehind(s Session, err error) []slog.Attr {
	return slices.Concat(s.LogAttrs(), s.LeftBehindAttrs(), []slog.Attr{slog.String("err", err.Error())})
}

// shutdown closes every session the run holds, and says per session whether
// what it held went. What outlives the run, such as an actor holding a worker,
// stays until someone deletes it by hand, so the one place that knows it was
// left behind names it.
//
// The sessions are closed in parallel, capped at teardownConcurrency: a run's
// worth of them closed one at a time would outlast the budget the caller
// allows.
//
// It begins by draining the task function, because boomer.Run returns while
// iterations are still in flight (see inflight.go) and the walk below has to
// happen after the last session has been recorded, not during it.
func (r *runtime) shutdown(ctx context.Context) {
	// In-flight calls are cut off first, so the drain waits seconds rather than
	// the rest of an iteration: an openclaw turn takes far longer than the
	// teardown has. What they were doing is not recorded; see measure.CutOff.
	// Control-plane calls are let finish instead, so the actors are not left
	// mid transition; see measure.ControlContext.
	r.endRun(measure.ErrRunEnded)

	// The drain takes a slice of the caller's budget rather than all of it, so
	// that one wedged iteration cannot spend every second the suspend and
	// delete calls need — turning a drain meant to prevent leaks into the
	// cause of them.
	drainCtx, cancel := context.WithTimeout(ctx, drainTimeout)
	defer cancel()
	if !r.inflight.drain(drainCtx) {
		// Not fatal, and deliberately still followed by the walk: the sessions
		// already recorded are worth closing even though the map may still
		// grow behind us. What it costs is the guarantee, so name it.
		slog.Warn("teardown drain timed out; tearing down with iterations still in flight",
			slog.Duration("drain_timeout", drainTimeout))
	}

	// Snapshotted before the teardown so nothing is walked while goroutines are
	// being launched against it.
	var sessions []Session
	r.sessions.Range(func(_, val any) bool {
		sessions = append(sessions, val.(Session))
		return true
	})

	var removed, left atomic.Int64
	var wg sync.WaitGroup
	sem := make(chan struct{}, teardownConcurrency)
	for _, s := range sessions {
		sem <- struct{}{}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-sem }()

			err := s.Close(ctx)
			bmetrics.UpdateUsers(r.opts.UserClass, -1)
			if err != nil {
				left.Add(1)
				slog.LogAttrs(ctx, slog.LevelWarn, "teardown left a session behind", leftBehind(s, err)...)
				return
			}
			removed.Add(1)
			slog.LogAttrs(ctx, slog.LevelInfo, "teardown closed a session", s.LogAttrs()...)
		}()
	}
	wg.Wait()

	slog.Info("teardown finished",
		slog.Int64("removed", removed.Load()),
		slog.Int64("left", left.Load()))
}
