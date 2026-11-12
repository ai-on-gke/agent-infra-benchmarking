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
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
)

// newHermesActor stands in for hermes_actor.py with handler.
func newHermesActor(t *testing.T, handler http.HandlerFunc) sandbox.Actor {
	t.Helper()
	ts := httptest.NewServer(handler)
	t.Cleanup(ts.Close)
	return sandbox.Actor{HTTPClient: ts.Client(), RouterURL: ts.URL, Name: "hermesactor"}
}

// echoMockLLM answers /invoke as the actor does once mock-llm's loop ends.
func echoMockLLM(t *testing.T) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if r.Method != http.MethodPost || r.URL.Path != hermesInvokePath {
			t.Errorf("request: got %s %s, want POST %s", r.Method, r.URL.Path, hermesInvokePath)
		}
		nonce, ok := strings.CutPrefix(string(body), mockLLMPrompt+" (request ")
		if !ok {
			t.Errorf("body: got %q, want the prompt %q with a nonce", body, mockLLMPrompt)
		}
		_, _ = io.WriteString(w, mockLLMReplyFor(strings.TrimSuffix(nonce, ")")))
	}
}

func TestHermesSucceedsOnItsTurnsReply(t *testing.T) {
	retry, err := hermesWorkload{}.SendInstruction(context.Background(), newHermesActor(t, echoMockLLM(t)))
	if err != nil {
		t.Fatalf("SendInstruction failed: %v", err)
	}
	if retry {
		t.Error("retry: got true on success, want false")
	}
}

func TestHermesRejectsAnotherTurnsReply(t *testing.T) {
	actor := newHermesActor(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, mockLLMReplyFor("00000000-0000-4000-8000-000000000000"))
	})
	retry, err := hermesWorkload{}.SendInstruction(context.Background(), actor)
	if err == nil {
		t.Fatal("SendInstruction succeeded on another turn's reply, want failure")
	}
	if retry {
		t.Error("retry: got true for a wrong reply, want false")
	}
}

func TestHermesRetriesWhileActivating(t *testing.T) {
	actor := newHermesActor(t, func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "activating", http.StatusServiceUnavailable)
	})
	retry, err := hermesWorkload{}.SendInstruction(context.Background(), actor)
	if err == nil {
		t.Fatal("SendInstruction succeeded against a 503, want failure")
	}
	if !retry {
		t.Error("retry: got false for a 503, want true")
	}
}

func TestHermesIsSelectable(t *testing.T) {
	w, err := Resolve(hermesWorkloadName)
	if err != nil {
		t.Fatalf("Resolve(%q) failed: %v", hermesWorkloadName, err)
	}
	if _, ok := w.(hermesWorkload); !ok {
		t.Fatalf("workload: got %T, want hermesWorkload", w)
	}
	if got := w.DefaultTemplate(); got != hermesTemplate {
		t.Errorf("DefaultTemplate: got %q, want %q", got, hermesTemplate)
	}
}
