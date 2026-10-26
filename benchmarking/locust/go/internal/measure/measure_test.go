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

// A control-plane call outlives the end of the run, which would otherwise
// abort the server's workflow on the actor, but not its own timeout or any
// other end of its context, such as the teardown's budget.
func TestControlContextOutlivesOnlyTheEndOfTheRun(t *testing.T) {
	errBudget := errors.New("teardown budget spent")
	for _, tc := range []struct {
		name     string
		cause    error
		wantDone bool
	}{
		{name: "the run ended", cause: ErrRunEnded},
		{name: "another end", cause: errBudget, wantDone: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			parent, end := context.WithCancelCause(context.Background())
			callCtx, cancel := Recorder{Timeout: time.Minute}.ControlContext(parent)
			defer cancel()

			end(tc.cause)

			select {
			case <-callCtx.Done():
				if !tc.wantDone {
					t.Fatalf("the call ended with the run: %v", context.Cause(callCtx))
				}
				if got := context.Cause(callCtx); !errors.Is(got, tc.cause) {
					t.Errorf("cause: got %v, want %v", got, tc.cause)
				}
			case <-time.After(50 * time.Millisecond):
				if tc.wantDone {
					t.Fatal("the call outlived its context's end")
				}
			}
			if _, ok := callCtx.Deadline(); !ok {
				t.Error("the call has no deadline; want the recorder's timeout")
			}
		})
	}
}
