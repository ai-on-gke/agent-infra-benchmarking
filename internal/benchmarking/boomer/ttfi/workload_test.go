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

package ttfi

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/dynconfig"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/userclass"
	"go.opentelemetry.io/otel"
)

var (
	errStillActivating = errors.New("actor is still activating")
	errWorkloadFault   = errors.New("instruction rejected")
)

// instructionResult is one scripted answer from a fakeWorkload.
type instructionResult struct {
	retry bool
	err   error
}

// fakeWorkload is a workload that speaks no protocol at all. That is the
// point: a benchmark that still measures correctly while driving it cannot be
// reaching into glutton's /ping, which is what the workload interface exists
// to prevent.
type fakeWorkload struct {
	name     string
	template string
	// results answers the first len(results) instructions. Every instruction
	// after them succeeds, standing in for an actor that has finished
	// activating.
	results []instructionResult

	mu    sync.Mutex
	calls []actorRef
}

func (w *fakeWorkload) Name() string { return w.name }

func (w *fakeWorkload) DefaultTemplate() string { return w.template }

func (w *fakeWorkload) SendInstruction(_ context.Context, actor actorRef) (bool, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.calls = append(w.calls, actor)
	if len(w.calls) > len(w.results) {
		return false, nil
	}
	r := w.results[len(w.calls)-1]
	return r.retry, r.err
}

// recordedCalls returns the actor each instruction was addressed to, in order.
func (w *fakeWorkload) recordedCalls() []actorRef {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]actorRef(nil), w.calls...)
}

// newWorkloadTestUser builds a user around wl with no HTTP surface behind it,
// so anything the benchmark learns has to have come through the interface.
func newWorkloadTestUser(t *testing.T, wl workload) *ttfiUser {
	t.Helper()
	return &ttfiUser{
		cfg: &userclass.Config{
			APIStub:  &fakeControlClient{},
			Tracer:   otel.Tracer("workload-test"),
			Atespace: "benchmark",
			Dyn:      dynconfig.NewHolder(dynconfig.Config{}),
		},
		workload:     wl,
		actorName:    "fakeactor",
		templateName: wl.DefaultTemplate(),
		userClass:    ttfiUserClass,
	}
}

// The retry loop is the benchmark's, but the verdict on each answer is the
// workload's. Driving a workload that is not glutton is what shows the loop
// reads that verdict rather than re-deriving it from an HTTP status.
func TestTTFIDrivesTheWorkloadInterface(t *testing.T) {
	tests := []struct {
		name         string
		results      []instructionResult
		wantAttempts int
		wantErr      bool
	}{
		{
			name:         "ready on the first instruction",
			wantAttempts: 1,
		},
		{
			name: "retries while the workload says the actor is activating",
			results: []instructionResult{
				{retry: true, err: errStillActivating},
				{retry: true, err: errStillActivating},
			},
			wantAttempts: 3,
		},
		{
			name: "stops when the workload calls a failure unretryable",
			results: []instructionResult{
				{retry: false, err: errWorkloadFault},
			},
			wantAttempts: 1,
			wantErr:      true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			wl := &fakeWorkload{name: "fake", template: "fake-template", results: tc.results}
			u := newWorkloadTestUser(t, wl)

			attempts, _, err := u.firstInstruction(context.Background(), 5*time.Second)

			if tc.wantErr && err == nil {
				t.Fatal("firstInstruction succeeded, want failure")
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("firstInstruction failed: %v", err)
			}
			if attempts != tc.wantAttempts {
				t.Errorf("attempts: got %d, want %d", attempts, tc.wantAttempts)
			}

			calls := wl.recordedCalls()
			if len(calls) != tc.wantAttempts {
				t.Fatalf("instructions sent: got %d, want %d", len(calls), tc.wantAttempts)
			}
			// The workload is handed the actor to address rather than
			// discovering it, so every attempt has to name the same one.
			for i, got := range calls {
				if got.Atespace != u.cfg.Atespace || got.Name != u.actorName {
					t.Errorf("instruction %d addressed %s/%s, want %s/%s",
						i, got.Atespace, got.Name, u.cfg.Atespace, u.actorName)
				}
			}
		})
	}
}

// The target string is the only thing atenet resolves an actor from, so its
// shape is part of the contract between the benchmark and every workload.
func TestActorRefTarget(t *testing.T) {
	a := actorRef{Atespace: "benchmark", Name: "sb-1"}
	if got := a.target(); got != "benchmark/sb-1" {
		t.Errorf("target: got %q, want %q", got, "benchmark/sb-1")
	}
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
		w, err := resolveWorkload(gluttonWorkloadName)
		if err != nil {
			t.Fatalf("resolveWorkload(%q) failed: %v", gluttonWorkloadName, err)
		}
		if _, ok := w.(gluttonWorkload); !ok {
			t.Errorf("workload: got %T, want gluttonWorkload", w)
		}
	})

	// A typo that fell back to the default would produce rows that look valid
	// while measuring a workload nobody asked for, so it has to be an error —
	// and the message has to say what could have been asked for instead.
	t.Run("unknown name is an error listing the known ones", func(t *testing.T) {
		_, err := resolveWorkload("not-a-workload")
		if err == nil {
			t.Fatal("resolveWorkload succeeded for an unregistered name, want error")
		}
		if !strings.Contains(err.Error(), gluttonWorkloadName) {
			t.Errorf("error does not list the known workloads: %v", err)
		}
	})
}

// --ttfi-workload-template overrides the workload's own default; unset, the
// workload decides. The benchmark must not hold a template default of its own,
// or the two could name a template the selected workload cannot address.
func TestTemplateDefaultsToTheWorkloads(t *testing.T) {
	tests := []struct {
		name         string
		opts         Options
		wantTemplate string
	}{
		{
			name:         "unset uses the workload default",
			wantTemplate: "fake-template",
		},
		{
			name:         "--ttfi-workload-template overrides it",
			opts:         Options{Template: "some-other-template"},
			wantTemplate: "some-other-template",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			ctrl := &fakeControlClient{}
			rt := &ttfiRuntime{
				cfg: &userclass.Config{
					APIStub:  ctrl,
					Tracer:   otel.Tracer("workload-test"),
					Atespace: "benchmark",
					Dyn:      dynconfig.NewHolder(dynconfig.Config{}),
				},
				workload: &fakeWorkload{name: "fake", template: "fake-template"},
			}

			// Implicit resume keeps the control plane out of the window, so
			// the user is built without needing a resume to succeed.
			dyn := dynconfig.Config{ResumeMode: dynconfig.ResumeModeImplicit}
			u, err := rt.startUser(context.Background(), dyn, tc.opts)
			if err != nil {
				t.Fatalf("startUser failed: %v", err)
			}

			if u.templateName != tc.wantTemplate {
				t.Errorf("templateName: got %q, want %q", u.templateName, tc.wantTemplate)
			}
			// The name only matters if it reaches the actor that gets created.
			if got := ctrl.recordedTemplate(); got != tc.wantTemplate {
				t.Errorf("CreateActor template: got %q, want %q", got, tc.wantTemplate)
			}
		})
	}
}
