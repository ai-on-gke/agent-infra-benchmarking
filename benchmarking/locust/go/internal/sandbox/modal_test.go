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
	"errors"
	"fmt"
	"testing"
	"time"

	modal "github.com/modal-labs/modal-client/go"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/measure"
	"go.opentelemetry.io/otel"
)

// fakeModal stands in for Modal: it keeps which sandboxes run, and which
// sandbox each snapshot was taken of.
type fakeModal struct {
	n         int
	running   map[string]bool
	snapshots map[string]string
	restores  []string

	// failSnapshot and failTerminate fail that many calls of theirs;
	// failRestore every restore.
	failSnapshot  int
	failTerminate int
	failRestore   bool
}

func newFakeModal() *fakeModal {
	return &fakeModal{running: map[string]bool{}, snapshots: map[string]string{}}
}

func (f *fakeModal) start() ModalSandbox {
	f.n++
	id := fmt.Sprintf("sb-%d", f.n)
	f.running[id] = true
	return &fakeModalSandbox{f: f, id: id}
}

func (f *fakeModal) Create(context.Context) (ModalSandbox, error) { return f.start(), nil }

func (f *fakeModal) Restore(_ context.Context, snapshotID string) (ModalSandbox, error) {
	if f.failRestore {
		return nil, errors.New("restore refused")
	}
	if _, ok := f.snapshots[snapshotID]; !ok {
		return nil, fmt.Errorf("no snapshot %q", snapshotID)
	}
	f.restores = append(f.restores, snapshotID)
	return f.start(), nil
}

func (f *fakeModal) runningCount() int {
	n := 0
	for _, r := range f.running {
		if r {
			n++
		}
	}
	return n
}

type fakeModalSandbox struct {
	f  *fakeModal
	id string
}

func (s *fakeModalSandbox) ID() string { return s.id }

func (s *fakeModalSandbox) Snapshot(context.Context) (string, error) {
	if s.f.failSnapshot > 0 {
		s.f.failSnapshot--
		return "", errors.New("snapshot failed")
	}
	if !s.f.running[s.id] {
		return "", errors.New("sandbox not running")
	}
	id := fmt.Sprintf("snap-%d", len(s.f.snapshots)+1)
	s.f.snapshots[id] = s.id
	return id, nil
}

func (s *fakeModalSandbox) Terminate(context.Context) error {
	if s.f.failTerminate > 0 {
		s.f.failTerminate--
		return errors.New("terminate failed")
	}
	if _, ok := s.f.running[s.id]; !ok {
		return modal.NotFoundError{Exception: "no sandbox " + s.id}
	}
	s.f.running[s.id] = false
	return nil
}

func (s *fakeModalSandbox) Exited(context.Context) (bool, error) { return !s.f.running[s.id], nil }

func (s *fakeModalSandbox) Connect(_ context.Context, port int) (string, string, error) {
	return fmt.Sprintf("https://%s-%d.modal.test", s.id, port), "tok-" + s.id, nil
}

func newTestModal(f *fakeModal) *Modal {
	return &Modal{
		Rec:      measure.Recorder{Tracer: otel.Tracer("test"), UserClass: "ModalUser", Timeout: 5 * time.Second},
		API:      f,
		Port:     8080,
		Workload: "glutton",
	}
}

func fastSettle(t *testing.T) {
	old := SettlePoll
	SettlePoll = time.Millisecond
	t.Cleanup(func() { SettlePoll = old })
}

func TestModalCycle(t *testing.T) {
	f := newFakeModal()
	ctx := context.Background()
	sb, err := newTestModal(f).Create(ctx)
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if f.runningCount() != 0 || len(f.snapshots) != 1 {
		t.Fatalf("after Create: %d running, %d snapshots; want 0 and 1", f.runningCount(), len(f.snapshots))
	}

	for i, want := range []string{"snap-1", "snap-2"} {
		if err := sb.Wake(ctx); err != nil {
			t.Fatalf("Wake %d: %v", i, err)
		}
		if got := f.restores[i]; got != want {
			t.Errorf("Wake %d restored %q, want the latest snapshot %q", i, got, want)
		}
		a := sb.Actor()
		if a.RouterURL != "https://"+a.Name+"-8080.modal.test" || a.Header.Get("Authorization") != "Bearer tok-"+a.Name {
			t.Errorf("Wake %d actor: got url %q, auth %q for %s", i, a.RouterURL, a.Header.Get("Authorization"), a.Name)
		}
		if err := sb.Sleep(ctx); err != nil {
			t.Fatalf("Sleep %d: %v", i, err)
		}
		if f.runningCount() != 0 {
			t.Fatalf("after Sleep %d: %d running, want 0", i, f.runningCount())
		}
	}

	if err := sb.Wake(ctx); err != nil {
		t.Fatalf("Wake: %v", err)
	}
	if err := sb.Remove(ctx); err != nil {
		t.Fatalf("Remove: %v", err)
	}
	if f.runningCount() != 0 {
		t.Fatalf("after Remove: %d running, want 0", f.runningCount())
	}
	if err := sb.Remove(ctx); err != nil {
		t.Fatalf("second Remove: %v", err)
	}
}

// A create whose first snapshot fails leaves no sandbox running.
func TestModalCreateCleansUp(t *testing.T) {
	f := newFakeModal()
	f.failSnapshot = 1
	if _, err := newTestModal(f).Create(context.Background()); err == nil {
		t.Fatal("Create: got nil, want the snapshot's error")
	}
	if f.runningCount() != 0 {
		t.Fatalf("after a failed Create: %d running, want 0", f.runningCount())
	}
}

// A sandbox already gone counts as terminated.
func TestModalTerminateGone(t *testing.T) {
	f := newFakeModal()
	ctx := context.Background()
	sb, err := newTestModal(f).Create(ctx)
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := sb.Wake(ctx); err != nil {
		t.Fatalf("Wake: %v", err)
	}
	delete(f.running, sb.Actor().Name)
	if err := sb.Remove(ctx); err != nil {
		t.Fatalf("Remove of a gone sandbox: %v", err)
	}
}

func TestModalSettle(t *testing.T) {
	fastSettle(t)
	f := newFakeModal()
	ctx := context.Background()
	sb, err := newTestModal(f).Create(ctx)
	if err != nil {
		t.Fatalf("Create: %v", err)
	}

	// A Sleep whose terminate failed keeps its snapshot, so Settle only
	// terminates.
	if err := sb.Wake(ctx); err != nil {
		t.Fatalf("Wake: %v", err)
	}
	f.failTerminate = 1
	if err := sb.Sleep(ctx); err == nil {
		t.Fatal("Sleep: got nil, want the terminate's error")
	}
	if err := sb.Settle(ctx); err != nil {
		t.Fatalf("Settle after a failed terminate: %v", err)
	}
	if f.runningCount() != 0 || len(f.snapshots) != 2 {
		t.Errorf("after Settle: %d running, %d snapshots; want 0 and 2", f.runningCount(), len(f.snapshots))
	}

	// A sandbox that stopped on its own is dropped for the last snapshot.
	if err := sb.Wake(ctx); err != nil {
		t.Fatalf("Wake: %v", err)
	}
	f.running[sb.Actor().Name] = false
	f.failSnapshot = 1
	if err := sb.Sleep(ctx); err == nil {
		t.Fatal("Sleep: got nil, want the snapshot's error")
	}
	if err := sb.Settle(ctx); err != nil {
		t.Fatalf("Settle after the sandbox stopped: %v", err)
	}
	if err := sb.Wake(ctx); err != nil {
		t.Fatalf("Wake after Settle: %v", err)
	}
	if got := f.restores[len(f.restores)-1]; got != "snap-2" {
		t.Errorf("Wake after Settle restored %q, want snap-2", got)
	}

	// A failed restore leaves the last snapshot for the next Wake.
	if err := sb.Sleep(ctx); err != nil {
		t.Fatalf("Sleep: %v", err)
	}
	f.failRestore = true
	if err := sb.Wake(ctx); err == nil {
		t.Fatal("Wake: got nil, want the restore's error")
	}
	if err := sb.Settle(ctx); err != nil {
		t.Fatalf("Settle after a failed restore: %v", err)
	}
}

func TestModalSuspendDisabled(t *testing.T) {
	f := newFakeModal()
	p := newTestModal(f)
	p.SuspendDisabled = true
	ctx := context.Background()
	sb, err := p.Create(ctx)
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	for range 2 {
		if err := sb.Wake(ctx); err != nil {
			t.Fatalf("Wake: %v", err)
		}
		if err := sb.Sleep(ctx); err != nil {
			t.Fatalf("Sleep: %v", err)
		}
	}
	if len(f.restores) != 1 || f.runningCount() != 1 {
		t.Errorf("got %d restores, %d running; want 1 and 1", len(f.restores), f.runningCount())
	}
}
