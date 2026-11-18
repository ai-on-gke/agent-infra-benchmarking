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
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
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

// conversationActor stands in for claude_code_actor.py replaying a recorded
// conversation: it answers /invoke?turn=K as turn K, or as sessionTurn when
// that is set.
type conversationActor struct {
	status      int
	sessionTurn int
	paths       []string
	bodies      []string
}

func (a *conversationActor) serve(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	a.paths = append(a.paths, r.URL.RequestURI())
	a.bodies = append(a.bodies, string(body))
	if a.status != 0 {
		http.Error(w, http.StatusText(a.status), a.status)
		return
	}
	turn := a.sessionTurn
	if turn == 0 {
		turn, _ = strconv.Atoi(r.URL.Query().Get("turn"))
	}
	_, nonce, _ := strings.Cut(string(body), " (request ")
	_ = json.NewEncoder(w).Encode(claudeCodeReply{
		Result: fmt.Sprintf("Done. (turn %d) (request %s", turn, nonce),
		Tasks:  5,
	})
}

func newConversationActor(t *testing.T, a *conversationActor) sandbox.Actor {
	t.Helper()
	ts := httptest.NewServer(http.HandlerFunc(a.serve))
	t.Cleanup(ts.Close)
	return sandbox.Actor{HTTPClient: ts.Client(), RouterURL: ts.URL, Name: "conversationactor"}
}

func conversationWorkload(prompts ...string) Workload {
	return (&claudeCodeWorkload{conversation{turns: prompts}}).ForActor()
}

// The conversation's prompts go in order, each saying its turn, and from the
// first again after the last.
func TestClaudeCodeSendsTheConversation(t *testing.T) {
	a := &conversationActor{}
	actor := newConversationActor(t, a)
	w := conversationWorkload("Look at the repo.", "Add a test.")
	for i := range 3 {
		if _, err := send(t, w, actor); err != nil {
			t.Fatalf("instruction %d: %v", i+1, err)
		}
	}
	wantPaths := []string{"/invoke?turn=1", "/invoke?turn=2", "/invoke?turn=1"}
	if !slices.Equal(a.paths, wantPaths) {
		t.Errorf("paths: got %q, want %q", a.paths, wantPaths)
	}
	for i, prefix := range []string{"Look at the repo. (request ", "Add a test. (request ", "Look at the repo. (request "} {
		if !strings.HasPrefix(a.bodies[i], prefix) {
			t.Errorf("prompt %d: got %q, want it to start with %q", i+1, a.bodies[i], prefix)
		}
	}
}

// A session that lost its turns since the last answers the next prompt as
// its first.
func TestClaudeCodeRejectsALostConversation(t *testing.T) {
	a := &conversationActor{}
	actor := newConversationActor(t, a)
	w := conversationWorkload("one", "two")
	if _, err := send(t, w, actor); err != nil {
		t.Fatalf("turn 1: %v", err)
	}
	a.sessionTurn = 1
	retry, err := send(t, w, actor)
	if err == nil || !strings.Contains(err.Error(), "want turn 2") || retry {
		t.Fatalf("got retry %v, err %v; want turn 1 for turn 2 to fail without a retry", retry, err)
	}
}

// After a failed turn the conversation starts over.
func TestClaudeCodeStartsTheConversationOverAfterAFailure(t *testing.T) {
	a := &conversationActor{}
	actor := newConversationActor(t, a)
	w := conversationWorkload("one", "two", "three")
	if _, err := send(t, w, actor); err != nil {
		t.Fatalf("turn 1: %v", err)
	}
	a.status = http.StatusGatewayTimeout
	if _, err := send(t, w, actor); err == nil {
		t.Fatal("504: got success, want failure")
	}
	a.status = 0
	if _, err := send(t, w, actor); err != nil {
		t.Fatalf("after a failure: %v", err)
	}
	if got := a.paths[len(a.paths)-1]; got != "/invoke?turn=1" {
		t.Errorf("after a failure: got %s, want turn 1", got)
	}
}

func TestClaudeCodeFetchesItsTurns(t *testing.T) {
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

	t.Run("sends the served prompts", func(t *testing.T) {
		srv, _ := serve(http.StatusOK)
		w := &claudeCodeWorkload{conversation{turnsURL: srv.URL}}
		if err := w.Configure(context.Background()); err != nil {
			t.Fatalf("Configure failed: %v", err)
		}
		if want := []string{"Look at the repo.", "Add a test."}; !slices.Equal(w.turns, want) {
			t.Errorf("turns: got %q, want %q", w.turns, want)
		}
	})

	t.Run("tries a 5xx again", func(t *testing.T) {
		srv, calls := serve(http.StatusServiceUnavailable, http.StatusOK)
		if err := (&claudeCodeWorkload{conversation{turnsURL: srv.URL}}).Configure(context.Background()); err != nil {
			t.Fatalf("Configure failed: %v", err)
		}
		if got := calls.Load(); got != 2 {
			t.Errorf("calls: got %d, want 2", got)
		}
	})

	t.Run("does not try another recording again", func(t *testing.T) {
		srv, calls := serve(http.StatusConflict)
		if err := (&claudeCodeWorkload{conversation{turnsURL: srv.URL}}).Configure(context.Background()); err == nil || !strings.Contains(err.Error(), "HTTP 409") {
			t.Errorf("err: got %v, want an HTTP 409", err)
		}
		if got := calls.Load(); got != 1 {
			t.Errorf("calls: got %d, want 1", got)
		}
	})
}
