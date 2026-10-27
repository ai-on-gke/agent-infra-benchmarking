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

package measure

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestRetryBacksOffAndStopsAtTheBudget(t *testing.T) {
	initial, maxDelay, budget := RetryInitialDelay, RetryMaxDelay, RetryBudget
	RetryInitialDelay, RetryMaxDelay, RetryBudget = time.Millisecond, 4*time.Millisecond, 50*time.Millisecond
	t.Cleanup(func() {
		RetryInitialDelay, RetryMaxDelay, RetryBudget = initial, maxDelay, budget
	})

	var calls []time.Time
	busy := errors.New("busy")
	attempts, err := retry(context.Background(), RetryBudget, func(error) bool { return true }, func() error {
		calls = append(calls, time.Now())
		return busy
	})
	if !errors.Is(err, busy) || attempts != len(calls) {
		t.Fatalf("retry: got (%d, %v) after %d calls", attempts, err, len(calls))
	}
	if attempts < 4 {
		t.Fatalf("calls: got %d, want several within the budget", attempts)
	}
	if total := calls[len(calls)-1].Sub(calls[0]); total > RetryBudget+RetryMaxDelay {
		t.Errorf("retried for %v, past the %v budget", total, RetryBudget)
	}
	if first, later := calls[1].Sub(calls[0]), calls[3].Sub(calls[2]); later < first {
		t.Errorf("waits did not grow: first %v, third %v", first, later)
	}
}
