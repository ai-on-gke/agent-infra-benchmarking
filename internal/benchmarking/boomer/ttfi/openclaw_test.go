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
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// openclawServer is a stand-in for demos/openclaw/openclaw_actor.py. It
// reproduces that script's routing decisions rather than its behavior: which
// paths exist, and which request bodies take the fast path. If the actor's
// contract moves, this fake is what has to move with it.
type openclawServer struct {
	// Status fails every request with this HTTP status code, standing in for
	// the router answering while the actor is still activating.
	Status int
	// Reply replaces the fast-path body.
	Reply string

	mu       sync.Mutex
	requests []openclawRequest
}

type openclawRequest struct {
	method string
	path   string
	body   string
}

func (s *openclawServer) Start(t *testing.T) *httptest.Server {
	t.Helper()
	ts := httptest.NewServer(http.HandlerFunc(s.serve))
	t.Cleanup(ts.Close)
	return ts
}

func (s *openclawServer) recordedRequests() []openclawRequest {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]openclawRequest(nil), s.requests...)
}

func (s *openclawServer) serve(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	s.mu.Lock()
	s.requests = append(s.requests, openclawRequest{method: r.Method, path: r.URL.Path, body: string(body)})
	s.mu.Unlock()

	if s.Status != 0 {
		http.Error(w, http.StatusText(s.Status), s.Status)
		return
	}
	// The actor matches the prefix, and answers 404 anywhere else.
	if r.Method != http.MethodPost || !strings.HasPrefix(r.URL.Path, openclawInvokePath) {
		http.NotFound(w, r)
		return
	}
	// Anything outside the fast path runs the agent, which this fake does not
	// model: a TTFI instruction that reached it would be a bug in the
	// workload, and the empty reply below fails the verification that follows.
	if strings.TrimSpace(string(body)) != openclawFastPathPrompt {
		w.WriteHeader(http.StatusOK)
		return
	}

	reply := s.Reply
	if reply == "" {
		reply = openclawFastPathReply
	}
	w.Header().Set("Content-Type", textContentType)
	_, _ = io.WriteString(w, reply)
}

// newOpenclawActor returns the actor handle addressing srv.
func newOpenclawActor(t *testing.T, srv *openclawServer) actorRef {
	t.Helper()
	ts := srv.Start(t)
	return actorRef{
		HTTPClient: ts.Client(),
		RouterURL:  ts.URL,
		Atespace:   "benchmark",
		Name:       "openclawactor",
	}
}

// The instruction has to reach the fast path, because that is the only route
// that answers without booting the agent underneath it.
func TestOpenclawSendsTheFastPathInstruction(t *testing.T) {
	srv := &openclawServer{}
	actor := newOpenclawActor(t, srv)

	retry, err := openclawWorkload{}.SendInstruction(context.Background(), actor)
	if err != nil {
		t.Fatalf("SendInstruction failed: %v", err)
	}
	if retry {
		t.Error("retry: got true on success, want false")
	}

	reqs := srv.recordedRequests()
	if len(reqs) != 1 {
		t.Fatalf("requests: got %d, want 1", len(reqs))
	}
	if reqs[0].method != http.MethodPost {
		t.Errorf("method: got %q, want POST", reqs[0].method)
	}
	if reqs[0].path != openclawInvokePath {
		t.Errorf("path: got %q, want %q", reqs[0].path, openclawInvokePath)
	}
	// A body the actor does not recognize would fall through to a real agent
	// turn, which measures the agent rather than the activation.
	if reqs[0].body != openclawFastPathPrompt {
		t.Errorf("body: got %q, want %q", reqs[0].body, openclawFastPathPrompt)
	}
}

func TestOpenclawRejectsAnUnexpectedReply(t *testing.T) {
	// A 200 carrying someone else's body is what an interposed proxy or an
	// error page looks like, and it must not close the window.
	srv := &openclawServer{Reply: "not the fast path reply"}
	actor := newOpenclawActor(t, srv)

	retry, err := openclawWorkload{}.SendInstruction(context.Background(), actor)
	if err == nil {
		t.Fatal("SendInstruction succeeded on a wrong reply, want failure")
	}
	// The actor answered, so waiting cannot change the answer.
	if retry {
		t.Error("retry: got true for a wrong reply, want false")
	}
}

// While the actor is activating the router answers 503, which is the one
// failure a TTFI window is supposed to sit through.
func TestOpenclawRetriesWhileActivating(t *testing.T) {
	srv := &openclawServer{Status: http.StatusServiceUnavailable}
	actor := newOpenclawActor(t, srv)

	retry, err := openclawWorkload{}.SendInstruction(context.Background(), actor)
	if err == nil {
		t.Fatal("SendInstruction succeeded against a 503, want failure")
	}
	if !retry {
		t.Error("retry: got false for a 503, want true")
	}
}

func TestOpenclawIsSelectable(t *testing.T) {
	w, err := resolveWorkload(openclawWorkloadName)
	if err != nil {
		t.Fatalf("resolveWorkload(%q) failed: %v", openclawWorkloadName, err)
	}
	if _, ok := w.(openclawWorkload); !ok {
		t.Fatalf("workload: got %T, want openclawWorkload", w)
	}
	// The template has to be the one the manifest creates, or the actor is
	// built from something that does not serve /invoke.
	if got := w.DefaultTemplate(); got != openclawTemplate {
		t.Errorf("DefaultTemplate: got %q, want %q", got, openclawTemplate)
	}
}
