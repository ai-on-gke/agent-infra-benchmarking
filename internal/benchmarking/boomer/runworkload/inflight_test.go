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
	"sync/atomic"
	"testing"
	"time"
)

func TestInflightDrainWaitsForAdmittedIterations(t *testing.T) {
	var g inflight
	if !g.enter() {
		t.Fatal("enter on a fresh gate: got false, want true")
	}

	var finished atomic.Bool
	drained := make(chan bool, 1)
	go func() { drained <- g.drain(context.Background()) }()

	// The drain must still be blocked while the iteration is in flight. The
	// sleep is what makes the negative meaningful: a drain that ignored the
	// WaitGroup would have answered by now.
	select {
	case <-drained:
		t.Fatal("drain returned while an admitted iteration was still running")
	case <-time.After(50 * time.Millisecond):
	}

	finished.Store(true)
	g.leave()

	select {
	case ok := <-drained:
		if !ok {
			t.Error("drain: got false, want true")
		}
		if !finished.Load() {
			t.Error("drain returned before the iteration finished")
		}
	case <-time.After(time.Second):
		t.Fatal("drain did not return after the iteration left")
	}
}

func TestInflightRefusesEntryOnceDrained(t *testing.T) {
	var g inflight
	if !g.drain(context.Background()) {
		t.Fatal("drain of an idle gate: got false, want true")
	}
	if g.enter() {
		t.Error("enter after drain: got true, want false")
	}
	// Draining twice is how a caller that already timed out asks again; it
	// must not panic or block on an idle gate.
	if !g.drain(context.Background()) {
		t.Error("second drain of an idle gate: got false, want true")
	}
}

func TestInflightDrainHonorsContext(t *testing.T) {
	var g inflight
	if !g.enter() {
		t.Fatal("enter on a fresh gate: got false, want true")
	}
	defer g.leave()

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if g.drain(ctx) {
		t.Error("drain with a stuck iteration: got true, want false")
	}
	// The gate is closed even though the wait gave up, so the stragglers
	// cannot be joined by new work.
	if g.enter() {
		t.Error("enter after a timed-out drain: got true, want false")
	}
}

// TestInflightNoWorkAdmittedAfterDrain is the property the teardown depends
// on: once drain reports success, nothing can still be inside the gate. It
// reproduces boomer's shape — goroutines looping on the task function with no
// idea the run is over — so the Add-after-Wait race a bare WaitGroup would
// have is actually exercised. Run with -race.
func TestInflightNoWorkAdmittedAfterDrain(t *testing.T) {
	var g inflight

	// Stands in for the user map: only an admitted iteration may touch it,
	// so a write after drain returns is the leak the gate exists to prevent.
	var mu sync.Mutex
	var drainReturned bool
	var lateWrites int

	var wg sync.WaitGroup
	stop := make(chan struct{})
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				if !g.enter() {
					// Boomer keeps calling the task after the run ends; a
					// refused entry is the only thing stopping it.
					continue
				}
				mu.Lock()
				if drainReturned {
					lateWrites++
				}
				mu.Unlock()
				g.leave()
			}
		}()
	}

	// Let the loops get going, so the drain lands mid-iteration rather than
	// before any goroutine has entered.
	time.Sleep(20 * time.Millisecond)

	ok := g.drain(context.Background())
	mu.Lock()
	drainReturned = true
	mu.Unlock()

	if !ok {
		t.Error("drain: got false, want true")
	}

	// Give the loops a chance to misbehave before checking.
	time.Sleep(20 * time.Millisecond)
	close(stop)
	wg.Wait()

	mu.Lock()
	defer mu.Unlock()
	if lateWrites != 0 {
		t.Errorf("iterations ran after drain returned: got %d, want 0", lateWrites)
	}
}
