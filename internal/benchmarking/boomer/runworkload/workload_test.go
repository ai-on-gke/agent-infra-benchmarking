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
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"

	"github.com/ai-on-gke/agent-infra-benchmarking/internal/benchmarking/boomer/dynconfig"
	"github.com/ai-on-gke/agent-infra-benchmarking/internal/benchmarking/boomer/userclass"
)

// pingServer is a stand-in for the probe's /ping route — the one instruction
// this benchmark's default workload times.
type pingServer struct {
	// Status fails every request with this HTTP status code.
	Status int
	// PingFailures makes the first N requests fail with PingFailStatus before
	// the route starts answering, standing in for an application that is
	// still starting up.
	PingFailures int
	// PingFailStatus is the status those first PingFailures requests return.
	// Defaults to 503.
	PingFailStatus int
	// PingEcho replaces the echoed nonce, so a caller that verifies the echo
	// sees a mismatch.
	PingEcho string

	mu        sync.Mutex
	pingCount int
}

func (s *pingServer) Start(t *testing.T) *httptest.Server {
	t.Helper()
	ts := httptest.NewServer(http.HandlerFunc(s.serve))
	t.Cleanup(ts.Close)
	return ts
}

// RecordedPings returns how many requests the route has seen, including the
// ones PingFailures rejected — a measurement window counts every attempt a
// client would really have made.
func (s *pingServer) RecordedPings() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.pingCount
}

func (s *pingServer) serve(w http.ResponseWriter, r *http.Request) {
	if s.Status != 0 {
		http.Error(w, http.StatusText(s.Status), s.Status)
		return
	}
	if r.URL.Path != pingPath {
		http.NotFound(w, r)
		return
	}

	s.mu.Lock()
	s.pingCount++
	activating := s.pingCount <= s.PingFailures
	s.mu.Unlock()
	if activating {
		status := s.PingFailStatus
		if status == 0 {
			status = http.StatusServiceUnavailable
		}
		http.Error(w, http.StatusText(status), status)
		return
	}

	var req pingRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	echo := req.Nonce
	if s.PingEcho != "" {
		echo = s.PingEcho
	}
	w.Header().Set("Content-Type", pingContentType)
	_ = json.NewEncoder(w).Encode(pingResponse{Nonce: echo})
}

// targetFor points a sandboxTarget at ts. Real agent-sandbox targets are
// addressed by a per-pod Service DNS name and a fixed port; a test server
// binds to 127.0.0.1 on a random port, so the loopback address and the
// server's actual port stand in for those without needing DNS or a fixed
// port 8888 listener.
func targetFor(t *testing.T, ts *httptest.Server) sandboxTarget {
	t.Helper()
	u, err := url.Parse(ts.URL)
	if err != nil {
		t.Fatalf("parse %q: %v", ts.URL, err)
	}
	port, err := strconv.Atoi(u.Port())
	if err != nil {
		t.Fatalf("parse port from %q: %v", ts.URL, err)
	}
	return sandboxTarget{HTTPClient: ts.Client(), ServiceFQDN: u.Hostname(), Port: int32(port)}
}

func TestPingWorkloadSendInstruction(t *testing.T) {
	tests := []struct {
		name      string
		srv       *pingServer
		wantErr   bool
		wantRetry bool
	}{
		{name: "success"},
		{
			name:      "echo mismatch is unretryable",
			srv:       &pingServer{PingEcho: "not-what-was-sent"},
			wantErr:   true,
			wantRetry: false,
		},
		{
			name:      "client error is unretryable",
			srv:       &pingServer{Status: http.StatusNotFound},
			wantErr:   true,
			wantRetry: false,
		},
		{
			name:      "still activating is retryable",
			srv:       &pingServer{PingFailures: 1},
			wantErr:   true,
			wantRetry: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			srv := tc.srv
			if srv == nil {
				srv = &pingServer{}
			}
			ts := srv.Start(t)
			target := targetFor(t, ts)

			retry, err := pingWorkload{}.SendInstruction(context.Background(), target)
			if tc.wantErr && err == nil {
				t.Fatal("SendInstruction succeeded, want failure")
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("SendInstruction failed: %v", err)
			}
			if retry != tc.wantRetry {
				t.Errorf("retry: got %v, want %v", retry, tc.wantRetry)
			}
		})
	}
}

var (
	errStillActivating = errors.New("sandbox is still activating")
	errWorkloadFault   = errors.New("instruction rejected")
)

// instructionResult is one scripted answer from a fakeWorkload.
type instructionResult struct {
	retry bool
	err   error
}

// fakeWorkload is a workload that speaks no protocol at all. That is the
// point: a benchmark that still measures correctly while driving it cannot
// be reaching into ping's /ping, which is what the workload interface exists
// to prevent.
type fakeWorkload struct {
	name     string
	template string
	// results answers the first len(results) instructions. Every instruction
	// after them succeeds, standing in for a sandbox that has finished
	// starting up.
	results []instructionResult

	mu    sync.Mutex
	calls []sandboxTarget
}

func (w *fakeWorkload) Name() string { return w.name }

func (w *fakeWorkload) DefaultTemplate() string { return w.template }

func (w *fakeWorkload) SendInstruction(_ context.Context, target sandboxTarget) (bool, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.calls = append(w.calls, target)
	if len(w.calls) > len(w.results) {
		return false, nil
	}
	r := w.results[len(w.calls)-1]
	return r.retry, r.err
}

func (w *fakeWorkload) recordedCalls() []sandboxTarget {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]sandboxTarget(nil), w.calls...)
}

// newFirstInstructionTestUser builds a user with just enough of Config for
// firstInstruction: it never touches Sandboxes/Extensions/Namespace.
func newFirstInstructionTestUser(wl workload) *runWorkloadUser {
	return &runWorkloadUser{
		cfg: &userclass.Config{
			Tracer: otel.Tracer("workload-test"),
			Dyn:    dynconfig.NewHolder(dynconfig.Config{}),
		},
		workload:  wl,
		userClass: runWorkloadUserClass,
	}
}

// The retry loop is the benchmark's, but the verdict on each answer is the
// workload's. Driving a workload that speaks no real protocol shows the loop
// reads that verdict rather than re-deriving it from an HTTP status itself.
func TestFirstInstructionDrivesTheWorkloadInterface(t *testing.T) {
	tests := []struct {
		name         string
		results      []instructionResult
		wantAttempts int
		wantErr      bool
	}{
		{name: "ready on the first instruction", wantAttempts: 1},
		{
			name: "retries while the workload says the sandbox is activating",
			results: []instructionResult{
				{retry: true, err: errStillActivating},
				{retry: true, err: errStillActivating},
			},
			wantAttempts: 3,
		},
		{
			name:         "stops when the workload calls a failure unretryable",
			results:      []instructionResult{{retry: false, err: errWorkloadFault}},
			wantAttempts: 1,
			wantErr:      true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			wl := &fakeWorkload{name: "fake", template: "fake-template", results: tc.results}
			u := newFirstInstructionTestUser(wl)

			attempts, _, err := u.firstInstruction(context.Background(), sandboxTarget{}, 5*time.Second)

			if tc.wantErr && err == nil {
				t.Fatal("firstInstruction succeeded, want failure")
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("firstInstruction failed: %v", err)
			}
			if attempts != tc.wantAttempts {
				t.Errorf("attempts: got %d, want %d", attempts, tc.wantAttempts)
			}
			if got := len(wl.recordedCalls()); got != tc.wantAttempts {
				t.Errorf("instructions sent: got %d, want %d", got, tc.wantAttempts)
			}
		})
	}
}

func TestFirstInstructionFailsWhenTheBudgetElapses(t *testing.T) {
	// Never succeeds, so the loop must be stopped by the budget rather than
	// run for the rest of the test.
	u := newFirstInstructionTestUser(&alwaysRetryWorkload{})

	start := time.Now()
	_, _, err := u.firstInstruction(context.Background(), sandboxTarget{}, 100*time.Millisecond)
	elapsed := time.Since(start)

	if err == nil {
		t.Fatal("firstInstruction succeeded, want budget failure")
	}
	if elapsed > 5*time.Second {
		t.Errorf("budget not enforced: loop ran for %v", elapsed)
	}
}

// alwaysRetryWorkload never succeeds, standing in for a sandbox that never
// finishes starting up.
type alwaysRetryWorkload struct{}

func (alwaysRetryWorkload) Name() string            { return "always-retry" }
func (alwaysRetryWorkload) DefaultTemplate() string { return "always-retry-template" }
func (alwaysRetryWorkload) SendInstruction(context.Context, sandboxTarget) (bool, error) {
	return true, errStillActivating
}

func TestResolveWorkload(t *testing.T) {
	t.Run("unset selects the default", func(t *testing.T) {
		w, err := resolveWorkload("")
		if err != nil {
			t.Fatalf("resolveWorkload(\"\") failed: %v", err)
		}
		if w.Name() != DefaultWorkload {
			t.Errorf("workload: got %q, want %q", w.Name(), DefaultWorkload)
		}
	})

	t.Run("names the registered workload", func(t *testing.T) {
		w, err := resolveWorkload(pingWorkloadName)
		if err != nil {
			t.Fatalf("resolveWorkload(%q) failed: %v", pingWorkloadName, err)
		}
		if _, ok := w.(pingWorkload); !ok {
			t.Errorf("workload: got %T, want pingWorkload", w)
		}
	})

	// A typo that fell back to the default would produce rows that look
	// valid while measuring a workload nobody asked for, so it has to be an
	// error — and the message has to say what could have been asked for
	// instead.
	t.Run("unknown name is an error listing the known ones", func(t *testing.T) {
		_, err := resolveWorkload("not-a-workload")
		if err == nil {
			t.Fatal("resolveWorkload succeeded for an unregistered name, want error")
		}
		if !strings.Contains(err.Error(), pingWorkloadName) {
			t.Errorf("error does not list the known workloads: %v", err)
		}
	})
}
