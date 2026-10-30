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

	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
)

func TestRLRolloutWorkload(t *testing.T) {
	t.Run("resolves registered rl-rollout", func(t *testing.T) {
		w, err := Resolve(rlRolloutWorkloadName)
		if err != nil {
			t.Fatalf("Resolve(%q) failed: %v", rlRolloutWorkloadName, err)
		}
		if w.Name() != rlRolloutWorkloadName {
			t.Errorf("got %q, want %q", w.Name(), rlRolloutWorkloadName)
		}
		if w.DefaultTemplate() != rlRolloutTemplate {
			t.Errorf("got template %q, want %q", w.DefaultTemplate(), rlRolloutTemplate)
		}
	})

	t.Run("successful instruction with nonce", func(t *testing.T) {
		ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != rlRolloutInvokePath {
				http.NotFound(w, r)
				return
			}
			body, _ := io.ReadAll(r.Body)
			bodyStr := string(body)
			// Extract request nonce
			idx := strings.Index(bodyStr, "(request ")
			nonce := "test-nonce"
			if idx != -1 {
				nonce = bodyStr[idx:]
			}
			w.Header().Set("Content-Type", "text/plain")
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("RL training step 1 completed: loss=0.4501, reward=1.200, accuracy=100.0% " + nonce))
		}))
		defer ts.Close()

		w, err := Resolve(rlRolloutWorkloadName)
		if err != nil {
			t.Fatalf("Resolve failed: %v", err)
		}

		actor := sandbox.Actor{
			RouterURL:  ts.URL,
			HTTPClient: ts.Client(),
		}

		retry, err := w.SendInstruction(context.Background(), actor)
		if err != nil {
			t.Fatalf("SendInstruction failed: %v", err)
		}
		if retry {
			t.Errorf("got retry true, want false")
		}
	})

	t.Run("fails on missing expected text", func(t *testing.T) {
		ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/plain")
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("something else entirely"))
		}))
		defer ts.Close()

		w, err := Resolve(rlRolloutWorkloadName)
		if err != nil {
			t.Fatalf("Resolve failed: %v", err)
		}

		actor := sandbox.Actor{
			RouterURL:  ts.URL,
			HTTPClient: ts.Client(),
		}

		_, err = w.SendInstruction(context.Background(), actor)
		if err == nil {
			t.Fatal("SendInstruction succeeded unexpectedly")
		}
	})
}
