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

package workload

import (
	"context"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
)

func TestResolve(t *testing.T) {
	t.Run("unset selects the default", func(t *testing.T) {
		w, err := Resolve("")
		if err != nil {
			t.Fatalf("Resolve(\"\") failed: %v", err)
		}
		if w.Name() != Default {
			t.Errorf("workload: got %q, want %q", w.Name(), Default)
		}
	})

	t.Run("names the registered workload", func(t *testing.T) {
		w, err := Resolve(GluttonName)
		if err != nil {
			t.Fatalf("Resolve(%q) failed: %v", GluttonName, err)
		}
		if _, ok := w.(gluttonWorkload); !ok {
			t.Errorf("workload: got %T, want gluttonWorkload", w)
		}
	})

	// A typo that fell back to the default would produce rows that look valid
	// while measuring a workload nobody asked for, so it has to be an error —
	// and the message has to say what could have been asked for instead.
	t.Run("unknown name is an error listing the known ones", func(t *testing.T) {
		_, err := Resolve("not-a-workload")
		if err == nil {
			t.Fatal("Resolve succeeded for an unregistered name, want error")
		}
		if !strings.Contains(err.Error(), GluttonName) {
			t.Errorf("error does not list the known workloads: %v", err)
		}
	})
}

func TestFetchTurns(t *testing.T) {
	// serve answers each call with the next of statuses, the last for the rest.
	serve := func(statuses ...int) (*httptest.Server, *atomic.Int32) {
		var calls atomic.Int32
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			status := statuses[min(int(calls.Add(1)), len(statuses))-1]
			w.WriteHeader(status)
			if status == http.StatusOK {
				w.Write([]byte(`["Look at the repo.", "Add a test."]`))
			}
		}))
		t.Cleanup(srv.Close)
		return srv, &calls
	}

	t.Run("returns the served prompts", func(t *testing.T) {
		srv, _ := serve(http.StatusOK)
		turns, err := FetchTurns(context.Background(), srv.URL)
		if err != nil {
			t.Fatalf("FetchTurns failed: %v", err)
		}
		if want := []string{"Look at the repo.", "Add a test."}; !slices.Equal(turns, want) {
			t.Errorf("turns: got %q, want %q", turns, want)
		}
	})

	t.Run("tries a 5xx again", func(t *testing.T) {
		srv, calls := serve(http.StatusServiceUnavailable, http.StatusOK)
		if _, err := FetchTurns(context.Background(), srv.URL); err != nil {
			t.Fatalf("FetchTurns failed: %v", err)
		}
		if got := calls.Load(); got != 2 {
			t.Errorf("calls: got %d, want 2", got)
		}
	})

	t.Run("does not try another recording again", func(t *testing.T) {
		srv, calls := serve(http.StatusConflict)
		if _, err := FetchTurns(context.Background(), srv.URL); err == nil || !strings.Contains(err.Error(), "HTTP 409") {
			t.Errorf("err: got %v, want an HTTP 409", err)
		}
		if got := calls.Load(); got != 1 {
			t.Errorf("calls: got %d, want 1", got)
		}
	})
}
