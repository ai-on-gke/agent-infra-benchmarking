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
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
)

// claudeCodeActor stands in for claude_code_actor.py with mock-llm behind it:
// each prompt is answered as turn turn with commits commits, by default the
// next turn of a session of five.
type claudeCodeActor struct {
	status        int
	turn, commits int
	tasks         int
	wrongNonce    bool
	bodies        []string
}

func (a *claudeCodeActor) next() {
	a.turn = a.turn%5 + 1
	a.commits = a.turn
}

func (a *claudeCodeActor) serve(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	a.bodies = append(a.bodies, string(body))
	if a.status != 0 {
		http.Error(w, http.StatusText(a.status), a.status)
		return
	}
	if r.Method != http.MethodPost || r.URL.Path != claudeCodeInvokePath {
		http.NotFound(w, r)
		return
	}
	nonce := claudeCodeNonce(string(body))
	if a.wrongNonce {
		nonce = "00000000-0000-4000-8000-000000000000"
	}
	tasks := a.tasks
	if tasks == 0 {
		tasks = 5
	}
	_ = json.NewEncoder(w).Encode(claudeCodeReply{
		Result:  fmt.Sprintf("Fixed it. (turn %d) (request %s)", a.turn, nonce),
		Commits: a.commits,
		Tasks:   tasks,
	})
}

func claudeCodeNonce(prompt string) string {
	nonce, _ := strings.CutPrefix(prompt, claudeCodePrompt+" (request ")
	return strings.TrimSuffix(nonce, ")")
}

func newClaudeCodeActor(t *testing.T, a *claudeCodeActor) sandbox.Actor {
	t.Helper()
	ts := httptest.NewServer(http.HandlerFunc(a.serve))
	t.Cleanup(ts.Close)
	return sandbox.Actor{HTTPClient: ts.Client(), RouterURL: ts.URL, Name: "claudecodeactor"}
}

func send(t *testing.T, w Workload, actor sandbox.Actor) (bool, error) {
	t.Helper()
	return w.SendInstruction(context.Background(), actor)
}

// A session runs its five turns, then starts over at turn 1.
func TestClaudeCodeFollowsTheSession(t *testing.T) {
	a := &claudeCodeActor{}
	actor := newClaudeCodeActor(t, a)
	w := (&claudeCodeWorkload{}).ForActor()
	for i := range 7 {
		a.next()
		if retry, err := send(t, w, actor); err != nil || retry {
			t.Fatalf("turn %d: got retry %v, err %v; want success", i+1, retry, err)
		}
	}
	if len(a.bodies) != 7 || !strings.HasPrefix(a.bodies[0], claudeCodePrompt+" (request ") {
		t.Errorf("prompts: got %q", a.bodies)
	}
}

// A turn that repeats the last one is what a suspend that lost the session
// and the repo looks like.
func TestClaudeCodeRejectsALostSession(t *testing.T) {
	a := &claudeCodeActor{}
	actor := newClaudeCodeActor(t, a)
	w := (&claudeCodeWorkload{}).ForActor()
	a.next()
	if _, err := send(t, w, actor); err != nil {
		t.Fatalf("turn 1: %v", err)
	}
	retry, err := send(t, w, actor)
	if err == nil || !strings.Contains(err.Error(), "want turn 2") {
		t.Fatalf("got %v, want a turn 1 after turn 1 to fail", err)
	}
	if retry {
		t.Error("retry: got true for a wrong reply, want false")
	}
}

// A fresh actor's first turn has to be its session's first.
func TestClaudeCodeStartsAtTurnOne(t *testing.T) {
	a := &claudeCodeActor{turn: 3, commits: 3}
	if _, err := send(t, (&claudeCodeWorkload{}).ForActor(), newClaudeCodeActor(t, a)); err == nil {
		t.Fatal("a fresh actor's turn 3 succeeded, want failure")
	}
}

// A session that kept its turns while the repo lost its commits, or the
// other way round.
func TestClaudeCodeRejectsASessionOutOfStepWithTheRepo(t *testing.T) {
	a := &claudeCodeActor{turn: 1, commits: 0}
	if _, err := send(t, (&claudeCodeWorkload{}).ForActor(), newClaudeCodeActor(t, a)); err == nil {
		t.Fatal("turn 1 with no commit succeeded, want failure")
	}
}

func TestClaudeCodeRejectsAnotherTurnsReply(t *testing.T) {
	a := &claudeCodeActor{turn: 1, commits: 1, wrongNonce: true}
	if _, err := send(t, (&claudeCodeWorkload{}).ForActor(), newClaudeCodeActor(t, a)); err == nil {
		t.Fatal("another turn's reply succeeded, want failure")
	}
}

// After a failed turn the actor may have committed or not, and may have
// started over, so any turn is taken; the ones after it are checked again.
func TestClaudeCodeResyncsAfterAFailure(t *testing.T) {
	a := &claudeCodeActor{status: http.StatusGatewayTimeout}
	actor := newClaudeCodeActor(t, a)
	w := (&claudeCodeWorkload{}).ForActor()
	if retry, err := send(t, w, actor); err == nil || !retry {
		t.Fatalf("504: got retry %v, err %v; want a retryable failure", retry, err)
	}
	a.status, a.turn, a.commits = 0, 4, 4
	if _, err := send(t, w, actor); err != nil {
		t.Fatalf("turn 4 after a failure: %v", err)
	}
	if _, err := send(t, w, actor); err == nil {
		t.Fatal("turn 4 after turn 4 succeeded, want failure")
	}
}

func TestClaudeCodeIsSelectableAndPerActor(t *testing.T) {
	w, err := Resolve(claudeCodeWorkloadName)
	if err != nil {
		t.Fatalf("Resolve(%q): %v", claudeCodeWorkloadName, err)
	}
	if w.DefaultTemplate() != claudeCodeTemplate {
		t.Errorf("template: got %q, want %q", w.DefaultTemplate(), claudeCodeTemplate)
	}
	p, ok := w.(PerActor)
	if !ok {
		t.Fatalf("%T is not PerActor", w)
	}
	if p.ForActor() == p.ForActor() {
		t.Error("ForActor returned one Workload twice, want one per actor")
	}
}
