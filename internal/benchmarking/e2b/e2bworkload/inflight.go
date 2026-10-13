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
	"sync"
)

// inflight gates the task function so teardown can wait for the iterations
// boomer leaves running when boomer.Run returns. An iteration enters for its
// whole duration and leaves when done; once drain closes the gate no new
// iteration may enter, so the sandbox set teardown walks cannot grow behind it.
//
// The zero value is ready to use.
type inflight struct {
	mu     sync.Mutex
	closed bool
	wg     sync.WaitGroup
}

// enter admits one iteration, reporting false once the gate is closed. A caller
// that is admitted must pair the call with exactly one leave.
func (i *inflight) enter() bool {
	i.mu.Lock()
	defer i.mu.Unlock()
	if i.closed {
		return false
	}
	i.wg.Add(1)
	return true
}

// leave marks an admitted iteration done.
func (i *inflight) leave() { i.wg.Done() }

// drain closes the gate to new iterations and waits for the admitted ones to
// leave, reporting false if ctx expires first. It is safe to call once.
func (i *inflight) drain(ctx context.Context) bool {
	i.mu.Lock()
	i.closed = true
	i.mu.Unlock()

	done := make(chan struct{})
	go func() {
		i.wg.Wait()
		close(done)
	}()
	select {
	case <-done:
		return true
	case <-ctx.Done():
		return false
	}
}
