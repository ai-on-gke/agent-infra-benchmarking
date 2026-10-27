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
	"time"
)

// A retried call waits RetryInitialDelay, doubling up to RetryMaxDelay, until
// RetryBudget is spent, or a call waiting for room until its timeout is. The
// refusals worth retrying clear once another operation on the same actor
// lands, which can take seconds when that operation takes a snapshot.
// Exported variables so that tests of the packages using this one can shorten
// them.
var (
	RetryInitialDelay = 100 * time.Millisecond
	RetryMaxDelay     = time.Second
	RetryBudget       = 5 * time.Second
)

// retry calls do until it returns an error retryable does not accept, nil,
// or budget runs out, and returns how many calls it made and the last error.
// A budget of zero is unbounded. A cancelled ctx ends the wait between calls.
func retry(ctx context.Context, budget time.Duration, retryable func(error) bool, do func() error) (int, error) {
	start := time.Now()
	delay := RetryInitialDelay
	for attempt := 1; ; attempt++ {
		err := do()
		if err == nil || !retryable(err) || (budget > 0 && time.Since(start)+delay > budget) {
			return attempt, err
		}
		select {
		case <-ctx.Done():
			return attempt, err
		case <-time.After(delay):
		}
		delay = min(2*delay, RetryMaxDelay)
	}
}
