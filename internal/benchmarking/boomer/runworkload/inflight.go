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
	"sync"
)

// inflight admits task iterations until it is closed, and lets a caller wait
// for the admitted ones to finish.
//
// This is the guarantee boomer does not give. runner.stop cancels each VU
// goroutine's context and returns without joining it, and the goroutine reads
// that cancellation only at the top of its loop — before the task function
// rather than during it. An iteration already inside the task therefore runs
// to completion after boomer.Run has returned, with nothing tracking it. The
// teardown that follows would otherwise walk the user map while a goroutine is
// still adding to it, and an actor created after that walk is never suspended
// or deleted: it holds a worker until someone removes it by hand.
//
// A bare WaitGroup cannot express this. The VU goroutines are still looping,
// so one can call Add after Wait has begun and after the counter has
// momentarily reached zero, which is precisely the case WaitGroup does not
// allow. The gate is what makes the wait sound: it stops iterations starting
// before any waiting begins.
//
// The zero value is ready to use.
type inflight struct {
	mu     sync.RWMutex
	closed bool
	wg     sync.WaitGroup
}

// enter admits one iteration, which must call leave when it finishes. It
// reports false once the gate is closed, which is the caller's signal to
// return without doing any work.
//
// The read lock is held across the check and the Add together, so a
// concurrent drain either sees this iteration in its wait or refuses it
// outright, and never lands between the two.
func (g *inflight) enter() bool {
	g.mu.RLock()
	defer g.mu.RUnlock()
	if g.closed {
		return false
	}
	g.wg.Add(1)
	return true
}

// leave releases an iteration admitted by enter.
func (g *inflight) leave() { g.wg.Done() }

// drain closes the gate and waits for the admitted iterations, reporting
// whether they all finished before ctx ended. It is safe to call more than
// once; a second call waits on whatever is still in flight.
//
// A false result leaves the iterations running rather than killing them —
// there is no way to do the latter — so a caller that proceeds anyway is
// racing the stragglers and should say so.
func (g *inflight) drain(ctx context.Context) bool {
	g.mu.Lock()
	g.closed = true
	g.mu.Unlock()

	done := make(chan struct{})
	go func() {
		g.wg.Wait()
		close(done)
	}()

	select {
	case <-done:
		return true
	case <-ctx.Done():
		return false
	}
}
