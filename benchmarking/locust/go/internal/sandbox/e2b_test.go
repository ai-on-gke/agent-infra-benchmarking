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

package sandbox

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/measure"
	"go.opentelemetry.io/otel"
)

// fakeE2B serves the part of the E2B API the provider calls, keeping each
// sandbox's state.
type fakeE2B struct {
	t *testing.T

	mu    sync.Mutex
	state map[string]string
	calls []string
	// createBodies are the create requests' bodies; idempotencyKeys their keys.
	createBodies    []map[string]any
	idempotencyKeys []string
	// failNext answers the next call with this status, then serves again.
	failNext int
}

func newFakeE2B(t *testing.T) (*fakeE2B, *httptest.Server) {
	f := &fakeE2B{t: t, state: map[string]string{}}
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	return f, srv
}

func (f *fakeE2B) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, r.Method+" "+r.URL.Path)
	if r.Header.Get("X-API-Key") != "key" {
		f.fail(w, http.StatusUnauthorized, "bad key")
		return
	}
	if f.failNext != 0 {
		status := f.failNext
		f.failNext = 0
		f.fail(w, status, "try again")
		return
	}

	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/"), "/")
	if parts[0] == "v2" {
		parts = parts[1:]
	}
	switch {
	case r.Method == http.MethodPost && len(parts) == 1:
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		f.createBodies = append(f.createBodies, body)
		f.idempotencyKeys = append(f.idempotencyKeys, r.Header.Get("Idempotency-Key"))
		id := "sbx" + string(rune('a'+len(f.state)))
		f.state[id] = "running"
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{"sandboxID": id, "trafficAccessToken": "tok-" + id})
		return
	case len(parts) < 2:
		f.fail(w, http.StatusNotFound, "no such path")
		return
	}

	id := parts[1]
	state, ok := f.state[id]
	if !ok {
		f.fail(w, http.StatusNotFound, "sandbox not found")
		return
	}
	switch {
	case r.Method == http.MethodGet && len(parts) == 2:
		_ = json.NewEncoder(w).Encode(map[string]any{"sandboxID": id, "state": state})
	case r.Method == http.MethodDelete && len(parts) == 2:
		delete(f.state, id)
		w.WriteHeader(http.StatusNoContent)
	case r.Method == http.MethodPost && parts[2] == "pause":
		if state == "paused" {
			f.fail(w, http.StatusConflict, "already paused")
			return
		}
		f.state[id] = "paused"
		w.WriteHeader(http.StatusNoContent)
	case r.Method == http.MethodPost && parts[2] == "connect":
		status := http.StatusOK
		if state == "paused" {
			status = http.StatusCreated
		}
		f.state[id] = "running"
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(map[string]any{"sandboxID": id, "trafficAccessToken": "tok-" + id})
	default:
		f.fail(w, http.StatusNotFound, "no such path")
	}
}

func (f *fakeE2B) fail(w http.ResponseWriter, status int, msg string) {
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"code": status, "message": msg})
}

func (f *fakeE2B) stateOf(id string) (string, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	s, ok := f.state[id]
	return s, ok
}

func newTestE2B(srv *httptest.Server) *E2B {
	return &E2B{
		Rec:      measure.Recorder{Tracer: otel.Tracer("test"), UserClass: "E2BUser", Timeout: 5 * time.Second},
		APIURL:   srv.URL,
		APIKey:   "key",
		API:      srv.Client(),
		Domain:   "e2b.app",
		Port:     8080,
		Workload: "glutton",
		Template: "glutton",
		CPUCount: 2,
		Lifetime: 90 * time.Second,
		Metadata: map[string]string{"atespace": "benchmark"},
	}
}

func TestE2BCycle(t *testing.T) {
	f, srv := newFakeE2B(t)
	p := newTestE2B(srv)
	ctx := context.Background()

	sb, err := p.Create(ctx)
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	id := sb.Actor().Name
	if st, _ := f.stateOf(id); st != "paused" {
		t.Fatalf("after Create: state %q, want paused", st)
	}
	body := f.createBodies[0]
	if body["templateID"] != "glutton" || body["timeout"] != float64(90) || body["cpuCount"] != float64(2) {
		t.Errorf("create body: got %v", body)
	}
	if _, ok := body["memoryMB"]; ok {
		t.Error("create body: memoryMB sent though unset")
	}
	if f.idempotencyKeys[0] == "" {
		t.Error("create: no Idempotency-Key")
	}

	if err := sb.Wake(ctx); err != nil {
		t.Fatalf("Wake: %v", err)
	}
	if st, _ := f.stateOf(id); st != "running" {
		t.Fatalf("after Wake: state %q, want running", st)
	}
	a := sb.Actor()
	if want := "https://8080-" + id + ".e2b.app"; a.RouterURL != want {
		t.Errorf("RouterURL: got %q, want %q", a.RouterURL, want)
	}
	if got := a.Header.Get(e2bTrafficTokenHeader); got != "tok-"+id {
		t.Errorf("traffic token: got %q", got)
	}

	if err := sb.Sleep(ctx); err != nil {
		t.Fatalf("Sleep: %v", err)
	}
	if st, _ := f.stateOf(id); st != "paused" {
		t.Fatalf("after Sleep: state %q, want paused", st)
	}

	if err := sb.Remove(ctx); err != nil {
		t.Fatalf("Remove: %v", err)
	}
	if _, ok := f.stateOf(id); ok {
		t.Fatal("after Remove: sandbox still there")
	}
	// A second Remove finds it gone, which is what it is for.
	if err := sb.Remove(ctx); err != nil {
		t.Fatalf("second Remove: %v", err)
	}
}

func TestE2BRetriesUnavailable(t *testing.T) {
	f, srv := newFakeE2B(t)
	p := newTestE2B(srv)
	f.failNext = http.StatusServiceUnavailable

	if _, err := p.Create(context.Background()); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if len(f.calls) < 2 || !strings.HasPrefix(f.calls[1], "POST /v2/sandboxes") {
		t.Errorf("calls: got %v, want the create made again", f.calls)
	}
}

func TestE2BCreateFailsOnAuth(t *testing.T) {
	_, srv := newFakeE2B(t)
	p := newTestE2B(srv)
	p.APIKey = "wrong"

	_, err := p.Create(context.Background())
	if e2bStatus(err) != http.StatusUnauthorized {
		t.Fatalf("Create: got %v, want a 401", err)
	}
}

func TestE2BSettle(t *testing.T) {
	old := SettlePoll
	SettlePoll = 10 * time.Millisecond
	t.Cleanup(func() { SettlePoll = old })

	f, srv := newFakeE2B(t)
	p := newTestE2B(srv)
	ctx := context.Background()
	sb, err := p.Create(ctx)
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	id := sb.Actor().Name

	// A running sandbox, as a Sleep that failed on the client may leave it,
	// is paused.
	f.mu.Lock()
	f.state[id] = "running"
	f.mu.Unlock()
	if err := sb.Settle(ctx); err != nil {
		t.Fatalf("Settle running: %v", err)
	}
	if st, _ := f.stateOf(id); st != "paused" {
		t.Errorf("after Settle: state %q, want paused", st)
	}

	// A paused one is left as it is.
	if err := sb.Settle(ctx); err != nil {
		t.Fatalf("Settle paused: %v", err)
	}

	// A gone one cannot be cycled again.
	f.mu.Lock()
	delete(f.state, id)
	f.mu.Unlock()
	if err := sb.Settle(ctx); err == nil {
		t.Fatal("Settle gone: got nil, want an error")
	}
}

func TestE2BSuspendDisabled(t *testing.T) {
	f, srv := newFakeE2B(t)
	p := newTestE2B(srv)
	p.SuspendDisabled = true
	ctx := context.Background()
	sb, err := p.Create(ctx)
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	id := sb.Actor().Name
	for range 2 {
		if err := sb.Wake(ctx); err != nil {
			t.Fatalf("Wake: %v", err)
		}
		if err := sb.Sleep(ctx); err != nil {
			t.Fatalf("Sleep: %v", err)
		}
	}
	if st, _ := f.stateOf(id); st != "running" {
		t.Errorf("state: got %q, want running", st)
	}
	connects := 0
	for _, c := range f.calls {
		if strings.HasSuffix(c, "/connect") {
			connects++
		}
	}
	if connects != 1 {
		t.Errorf("connects: got %d, want 1", connects)
	}
}
