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
	"errors"
	"sync"
	"testing"

	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/scenario"
	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/workload"
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
	calls []sandbox.Actor
}

func (w *fakeWorkload) Name() string { return w.name }

func (w *fakeWorkload) DefaultTemplate() string { return w.template }

func (w *fakeWorkload) SendInstruction(_ context.Context, actor sandbox.Actor) (bool, error) {
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
func (w *fakeWorkload) recordedCalls() []sandbox.Actor {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]sandbox.Actor(nil), w.calls...)
}

// newWorkloadTestUser builds a user around wl with no HTTP surface behind it,
// so anything the benchmark learns has to have come through the interface.
func newWorkloadTestUser(t *testing.T, wl workload.Workload) *runWorkloadUser {
	t.Helper()
	cfg := &userclass.Config{
		APIStub:  &fakeControlClient{},
		Tracer:   otel.Tracer("workload-test"),
		Atespace: "benchmark",
	}
	return newTestUser(t, newTestScenarioRunning(cfg, scenario.Options{}, wl))
}

// The router resolves the actor from the headers its sandbox gives, so every
// instruction has to carry them.
func TestTheInstructionCarriesTheActorsRoute(t *testing.T) {
	srv := &pingServer{}
	u := newTestRunWorkloadUser(t, srv, nil)

	if err := u.executeInstruction(context.Background()); err != nil {
		t.Fatalf("executeInstruction failed: %v", err)
	}
	if got, want := srv.RecordedTargetActor(), "benchmark/"+u.sandbox.Actor().Name; got != want {
		t.Errorf("target actor: got %q, want %q", got, want)
	}
}

// --workload-template overrides the workload's own default; unset, the
// workload decides. The benchmark must not hold a template default of its own,
// or the two could name a template the selected workload cannot address.
func TestTemplateDefaultsToTheWorkloads(t *testing.T) {
	tests := []struct {
		name         string
		opts         scenario.Options
		wantTemplate string
	}{
		{
			name:         "unset uses the workload default",
			wantTemplate: "fake-template",
		},
		{
			name:         "--workload-template overrides it",
			opts:         scenario.Options{Template: "some-other-template"},
			wantTemplate: "some-other-template",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			ctrl := &fakeControlClient{}
			cfg := &userclass.Config{
				APIStub:  ctrl,
				Tracer:   otel.Tracer("workload-test"),
				Atespace: "benchmark",
			}
			wl := &fakeWorkload{name: "fake", template: "fake-template"}
			u := newTestUser(t, newTestScenarioRunning(cfg, tc.opts, wl))

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
