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
	"cmp"
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

// fakeDaytona serves the part of the Daytona API the provider calls. Like
// Daytona, it answers create, start and pause mid transition, and a sandbox
// reaches its next state after a few reads.
type fakeDaytona struct {
	t *testing.T

	mu    sync.Mutex
	state map[string]string
	// next is the state a sandbox mid transition reaches, and reads how many
	// reads it takes.
	next  map[string]string
	reads map[string]int
	calls []string
	// createBodies are the create requests' bodies, by sandbox name.
	createBodies []map[string]any
	names        map[string]string
	// failNext answers the next call with this status, then serves again;
	// landFailed makes a failed create land all the same.
	failNext   int
	landFailed bool
	// startTo is the state a start ends in, when not started.
	startTo string
}

func newFakeDaytona(t *testing.T) (*fakeDaytona, *httptest.Server) {
	old := DaytonaPoll
	DaytonaPoll = time.Millisecond
	t.Cleanup(func() { DaytonaPoll = old })

	f := &fakeDaytona{t: t, state: map[string]string{}, next: map[string]string{}, reads: map[string]int{}, names: map[string]string{}}
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	return f, srv
}

func (f *fakeDaytona) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, r.Method+" "+r.URL.Path)
	if r.Header.Get("Authorization") != "Bearer key" {
		f.fail(w, http.StatusUnauthorized, "bad key")
		return
	}
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/"), "/")

	if r.Method == http.MethodPost && len(parts) == 1 {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		f.createBodies = append(f.createBodies, body)
		id := "sbx" + string(rune('a'+len(f.state)))
		land := f.failNext == 0 || f.landFailed
		if land {
			f.names[body["name"].(string)] = id
			f.move(id, "creating", "started")
		}
		if f.failNext != 0 {
			status := f.failNext
			f.failNext = 0
			f.fail(w, status, "try again")
			return
		}
		f.answer(w, id)
		return
	}
	if f.failNext != 0 {
		status := f.failNext
		f.failNext = 0
		f.fail(w, status, "try again")
		return
	}
	if len(parts) < 2 {
		f.fail(w, http.StatusNotFound, "no such path")
		return
	}

	id := parts[1]
	if byName, ok := f.names[id]; ok {
		id = byName
	}
	state, ok := f.state[id]
	if !ok {
		f.fail(w, http.StatusNotFound, "sandbox not found")
		return
	}
	switch {
	case r.Method == http.MethodGet && len(parts) == 2:
		if f.reads[id]--; f.reads[id] <= 0 && f.next[id] != "" {
			f.state[id], f.next[id] = f.next[id], ""
		}
		f.answer(w, id)
	case r.Method == http.MethodDelete && len(parts) == 2:
		delete(f.state, id)
		f.answer(w, id)
	case r.Method == http.MethodPost && parts[2] == "pause":
		if state != "started" {
			f.fail(w, http.StatusBadRequest, "sandbox is not started")
			return
		}
		f.move(id, "pausing", "paused")
		f.answer(w, id)
	case r.Method == http.MethodPost && parts[2] == "start":
		if state != "paused" {
			f.fail(w, http.StatusBadRequest, "sandbox is not paused")
			return
		}
		f.move(id, "resuming", cmp.Or(f.startTo, "started"))
		f.answer(w, id)
	case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/preview-url"):
		_ = json.NewEncoder(w).Encode(map[string]any{
			"sandboxId": id, "url": "https://" + parts[3] + "-" + id + ".proxy.daytona.works", "token": "tok-" + id,
		})
	default:
		f.fail(w, http.StatusNotFound, "no such path")
	}
}

// move puts the sandbox mid transition to next.
func (f *fakeDaytona) move(id, now, next string) {
	f.state[id], f.next[id], f.reads[id] = now, next, 2
}

func (f *fakeDaytona) answer(w http.ResponseWriter, id string) {
	_ = json.NewEncoder(w).Encode(map[string]any{"id": id, "state": f.state[id]})
}

func (f *fakeDaytona) fail(w http.ResponseWriter, status int, msg string) {
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"statusCode": status, "message": msg})
}

func (f *fakeDaytona) stateOf(id string) (string, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	s, ok := f.state[id]
	return s, ok
}

func (f *fakeDaytona) count(method, suffix string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, c := range f.calls {
		if strings.HasPrefix(c, method+" ") && strings.HasSuffix(c, suffix) {
			n++
		}
	}
	return n
}

func newTestDaytona(srv *httptest.Server) *Daytona {
	return &Daytona{
		Rec:      measure.Recorder{Tracer: otel.Tracer("test"), UserClass: "DaytonaUser", Timeout: 5 * time.Second},
		APIURL:   srv.URL,
		APIKey:   "key",
		API:      srv.Client(),
		Port:     8080,
		Workload: "glutton",
		Snapshot: "glutton-vm",
		CPU:      2,
		TTL:      90 * time.Second,
		Labels:   map[string]string{"atespace": "benchmark"},
	}
}

func TestDaytonaCycle(t *testing.T) {
	f, srv := newFakeDaytona(t)
	p := newTestDaytona(srv)
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
	if body["snapshot"] != "glutton-vm" || body["ttlMinutes"] != float64(2) || body["cpu"] != float64(2) || body["public"] != false {
		t.Errorf("create body: got %v", body)
	}
	for _, k := range []string{"memory", "target"} {
		if _, ok := body[k]; ok {
			t.Errorf("create body: %s sent though unset", k)
		}
	}

	if err := sb.Wake(ctx); err != nil {
		t.Fatalf("Wake: %v", err)
	}
	if st, _ := f.stateOf(id); st != "started" {
		t.Fatalf("after Wake: state %q, want started", st)
	}
	a := sb.Actor()
	if want := "https://8080-" + id + ".proxy.daytona.works"; a.RouterURL != want {
		t.Errorf("RouterURL: got %q, want %q", a.RouterURL, want)
	}
	if got := a.Header.Get("x-daytona-preview-token"); got != "tok-"+id {
		t.Errorf("preview token: got %q", got)
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

// A create retried after one that landed though its answer was lost takes
// that sandbox rather than making a second.
func TestDaytonaCreateRetryFindsLanded(t *testing.T) {
	f, srv := newFakeDaytona(t)
	p := newTestDaytona(srv)
	f.failNext, f.landFailed = http.StatusBadGateway, true

	if _, err := p.Create(context.Background()); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if n := f.count(http.MethodPost, "/sandbox"); n != 1 {
		t.Errorf("creates: got %d, want 1", n)
	}
	if len(f.state) != 1 {
		t.Errorf("sandboxes: got %d, want 1", len(f.state))
	}
}

func TestDaytonaCreateRetriesUnavailable(t *testing.T) {
	f, srv := newFakeDaytona(t)
	p := newTestDaytona(srv)
	f.failNext = http.StatusServiceUnavailable

	if _, err := p.Create(context.Background()); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if n := f.count(http.MethodPost, "/sandbox"); n != 2 {
		t.Errorf("creates: got %d, want 2", n)
	}
}

func TestDaytonaCreateFailsOnAuth(t *testing.T) {
	_, srv := newFakeDaytona(t)
	p := newTestDaytona(srv)
	p.APIKey = "wrong"

	_, err := p.Create(context.Background())
	if daytonaStatus(err) != http.StatusUnauthorized {
		t.Fatalf("Create: got %v, want a 401", err)
	}
}

// A sandbox that breaks while it resumes fails the Wake rather than being
// waited on.
func TestDaytonaWakeFailsOnError(t *testing.T) {
	f, srv := newFakeDaytona(t)
	p := newTestDaytona(srv)
	ctx := context.Background()
	sb, err := p.Create(ctx)
	if err != nil {
		t.Fatalf("Create: %v", err)
	}

	f.mu.Lock()
	f.startTo = "error"
	f.mu.Unlock()
	if err := sb.Wake(ctx); err == nil || !strings.Contains(err.Error(), "is error") {
		t.Fatalf("Wake: got %v, want the sandbox in error", err)
	}
}

func TestDaytonaSettle(t *testing.T) {
	old := SettlePoll
	SettlePoll = time.Millisecond
	t.Cleanup(func() { SettlePoll = old })

	f, srv := newFakeDaytona(t)
	p := newTestDaytona(srv)
	ctx := context.Background()
	sb, err := p.Create(ctx)
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	id := sb.Actor().Name

	// A running sandbox, as a Sleep that failed on the client may leave it,
	// is paused.
	f.mu.Lock()
	f.state[id] = "started"
	f.mu.Unlock()
	if err := sb.Settle(ctx); err != nil {
		t.Fatalf("Settle started: %v", err)
	}
	if st, _ := f.stateOf(id); st != "paused" {
		t.Errorf("after Settle: state %q, want paused", st)
	}

	// A paused one is left as it is.
	if err := sb.Settle(ctx); err != nil {
		t.Fatalf("Settle paused: %v", err)
	}

	// A stopped one has lost its memory, so it cannot be cycled again.
	f.mu.Lock()
	f.state[id] = "stopped"
	f.mu.Unlock()
	if err := sb.Settle(ctx); err == nil {
		t.Fatal("Settle stopped: got nil, want an error")
	}

	// Nor can a gone one.
	f.mu.Lock()
	delete(f.state, id)
	f.mu.Unlock()
	if err := sb.Settle(ctx); err == nil {
		t.Fatal("Settle gone: got nil, want an error")
	}
}

func TestDaytonaSuspendDisabled(t *testing.T) {
	f, srv := newFakeDaytona(t)
	p := newTestDaytona(srv)
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
	if st, _ := f.stateOf(id); st != "started" {
		t.Errorf("state: got %q, want started", st)
	}
	if n := f.count(http.MethodPost, "/start"); n != 1 {
		t.Errorf("starts: got %d, want 1", n)
	}
}
