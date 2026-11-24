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
	"log/slog"
	"net/http"
	"time"

	modal "github.com/modal-labs/modal-client/go"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/measure"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// Modal has no pause that keeps a sandbox in place, so this provider cycles
// each one through memory snapshots: Sleep snapshots its memory and
// filesystem and terminates it, and Wake restores the snapshot into a new
// sandbox, reached on a new URL. The SDK cannot delete snapshots, so every
// Sleep leaves one behind on Modal.
type Modal struct {
	Rec measure.Recorder
	API ModalAPI

	// ActorClient is the client a workload reaches a sandbox over, and Port
	// the one the workload listens on inside the sandbox.
	ActorClient *http.Client
	Port        int

	// Workload labels the actor gauges.
	Workload string

	// SuspendDisabled keeps the sandboxes running after their first Wake.
	SuspendDisabled bool
}

// ModalAPI is the part of Modal the provider calls.
type ModalAPI interface {
	// Create starts a sandbox that can be snapshotted, once it is scheduled.
	Create(ctx context.Context) (ModalSandbox, error)
	// Restore starts a new sandbox from the memory snapshot snapshotID.
	Restore(ctx context.Context, snapshotID string) (ModalSandbox, error)
}

// ModalSandbox is one running Modal sandbox.
type ModalSandbox interface {
	ID() string
	// Snapshot snapshots its memory and filesystem, which it keeps running.
	Snapshot(ctx context.Context) (snapshotID string, err error)
	Terminate(ctx context.Context) error
	// Exited reports whether it stopped running.
	Exited(ctx context.Context) (bool, error)
	// Connect returns the URL and bearer token that reach port in it.
	Connect(ctx context.Context, port int) (url, token string, err error)
}

// Create makes the sandbox and snapshots it, so the first Wake restores it
// like every other.
func (p *Modal) Create(ctx context.Context) (Sandbox, error) {
	s := &modalSandbox{p: p}
	err := p.Rec.GRPC(ctx, "CreateSandbox", func(ctx context.Context, _ *metadata.MD) error {
		sb, err := p.API.Create(ctx)
		s.running = sb
		return err
	})
	if err == nil {
		err = s.sleep(ctx, "PauseSandbox")
	}
	if err != nil {
		s.cleanUp(ctx)
		return nil, err
	}
	s.isCreated = true
	suspendedActors.WithLabelValues(p.Workload).Inc()
	return s, nil
}

type modalSandbox struct {
	p *Modal

	// running is the sandbox while one runs, and snapshot the one the next
	// Wake restores, taken of the sandbox snapshotOf.
	running    ModalSandbox
	snapshot   string
	snapshotOf string

	// url and token reach running, and change with every Wake.
	url   string
	token string

	isActive  bool
	isCreated bool
}

func (s *modalSandbox) Actor() Actor {
	h := http.Header{}
	h.Set("Authorization", "Bearer "+s.token)
	name := ""
	if s.running != nil {
		name = s.running.ID()
	}
	return Actor{
		HTTPClient: s.p.ActorClient,
		RouterURL:  s.url,
		Name:       name,
		Header:     h,
	}
}

// Wake restores the latest snapshot into a new sandbox and gets the URL that
// reaches it, both under the row, as the sandbox serves nothing before.
func (s *modalSandbox) Wake(ctx context.Context) error {
	if s.p.SuspendDisabled && s.isActive {
		return nil
	}
	err := s.p.Rec.GRPC(ctx, ResumeMetric, func(ctx context.Context, _ *metadata.MD) error {
		if s.running == nil {
			sb, err := s.p.API.Restore(ctx, s.snapshot)
			if err != nil {
				return err
			}
			s.running = sb
		}
		url, token, err := s.running.Connect(ctx, s.p.Port)
		if err != nil {
			return err
		}
		s.url, s.token = url, token
		return nil
	})
	if err != nil {
		return err
	}
	s.markActive()
	return nil
}

func (s *modalSandbox) Sleep(ctx context.Context) error {
	if s.p.SuspendDisabled {
		return nil
	}
	if err := s.sleep(ctx, SuspendMetric); err != nil {
		slog.Warn("suspend failed; the sandbox may be left running",
			slog.String("sandbox", s.running.ID()),
			slog.String("err", err.Error()))
		return err
	}
	s.markSuspended()
	return nil
}

func (s *modalSandbox) sleep(ctx context.Context, row string) error {
	return s.p.Rec.GRPC(ctx, row, func(ctx context.Context, _ *metadata.MD) error {
		return s.snapshotAndTerminate(ctx)
	})
}

// snapshotAndTerminate snapshots the running sandbox and terminates it. A
// failed terminate keeps the snapshot it took, so a retry only terminates.
func (s *modalSandbox) snapshotAndTerminate(ctx context.Context) error {
	if s.running == nil {
		return nil
	}
	if s.snapshot == "" || s.snapshotOf != s.running.ID() {
		id, err := s.running.Snapshot(ctx)
		if err != nil {
			return err
		}
		s.snapshot, s.snapshotOf = id, s.running.ID()
	}
	if err := terminateModal(ctx, s.running); err != nil {
		return err
	}
	s.running = nil
	return nil
}

// Settle snapshots and terminates the sandbox, unmetered, while one runs, or
// leaves it running when suspends are disabled. One that stopped on its own
// is dropped for the last snapshot. A sandbox with no snapshot to restore,
// or one still running after SettleTimeout, is not cycled again.
func (s *modalSandbox) Settle(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, SettleTimeout)
	defer cancel()
	for {
		if s.running == nil {
			if s.snapshot == "" {
				return errors.New("sandbox has no snapshot to restore")
			}
			s.markSuspended()
			return nil
		}
		if s.p.SuspendDisabled {
			s.markActive()
			return nil
		}
		err := s.snapshotAndTerminate(ctx)
		if err == nil {
			continue
		}
		if exited, exitErr := s.running.Exited(ctx); exitErr == nil && exited {
			s.running = nil
			continue
		}

		select {
		case <-ctx.Done():
			return fmt.Errorf("sandbox still running after %v: %w", SettleTimeout, err)
		case <-time.After(SettlePoll):
		}
	}
}

// Remove terminates the sandbox if one runs.
func (s *modalSandbox) Remove(ctx context.Context) error {
	if err := s.terminate(ctx); err != nil {
		return fmt.Errorf("terminate: %w", err)
	}
	return nil
}

// cleanUp terminates a sandbox a failed Create left running.
func (s *modalSandbox) cleanUp(ctx context.Context) {
	if s.running == nil {
		return
	}
	id := s.running.ID()
	if err := s.terminate(ctx); err != nil {
		slog.Warn("sandbox a failed create made was left behind",
			slog.String("sandbox", id),
			slog.String("err", err.Error()))
	}
}

func (s *modalSandbox) terminate(ctx context.Context) error {
	var err error
	if s.running != nil {
		err = s.p.Rec.GRPC(ctx, "TerminateSandbox", func(ctx context.Context, _ *metadata.MD) error {
			return terminateModal(ctx, s.running)
		})
		if err == nil {
			s.running = nil
		}
	}
	// The gauges drop whether or not the terminate worked, since the sandbox
	// is not used again either way.
	if s.isCreated {
		if s.isActive {
			activeActors.WithLabelValues(s.p.Workload).Dec()
		} else {
			suspendedActors.WithLabelValues(s.p.Workload).Dec()
		}
		s.isCreated = false
		s.isActive = false
	}
	return err
}

// terminateModal terminates sb, which is done when it is already gone.
func terminateModal(ctx context.Context, sb ModalSandbox) error {
	err := sb.Terminate(ctx)
	var notFound modal.NotFoundError
	if errors.As(err, &notFound) || status.Code(err) == codes.NotFound {
		return nil
	}
	return err
}

func (s *modalSandbox) markActive() {
	if !s.isActive {
		suspendedActors.WithLabelValues(s.p.Workload).Dec()
		activeActors.WithLabelValues(s.p.Workload).Inc()
		s.isActive = true
	}
}

func (s *modalSandbox) markSuspended() {
	if s.isActive {
		activeActors.WithLabelValues(s.p.Workload).Dec()
		suspendedActors.WithLabelValues(s.p.Workload).Inc()
		s.isActive = false
	}
}

// ModalOptions are what the SDK creates every sandbox with; its credentials
// and environment are the SDK's own, read from MODAL_TOKEN_ID,
// MODAL_TOKEN_SECRET and MODAL_ENVIRONMENT or ~/.modal.toml.
type ModalOptions struct {
	// App is the Modal App the sandboxes run under, made if missing, and
	// Image the registry image they run, built once by NewModalAPI.
	App   string
	Image string

	// Command starts the workload in the sandbox.
	Command []string

	// Regions are the ones sandboxes may run in; empty lets Modal pick.
	Regions []string

	// CPU and MemoryMiB size the sandboxes; zero leaves Modal's default.
	CPU       float64
	MemoryMiB int

	// Timeout ends a sandbox that long after its start, so the ones a crashed
	// worker leaves are cleaned up. Zero leaves Modal's default.
	Timeout time.Duration

	// Tags tag every sandbox, so the ones a run leaves can be found.
	Tags map[string]string
}

// NewModalAPI connects to Modal, looks up the App and builds the image, so
// no Create waits for either.
func NewModalAPI(ctx context.Context, o ModalOptions) (ModalAPI, error) {
	client, err := modal.NewClient()
	if err != nil {
		return nil, fmt.Errorf("modal client: %w", err)
	}
	app, err := client.Apps.FromName(ctx, o.App, &modal.AppFromNameParams{CreateIfMissing: true})
	if err != nil {
		return nil, fmt.Errorf("modal app %s: %w", o.App, err)
	}
	image, err := client.Images.FromRegistry(o.Image, nil).Build(ctx, app, nil)
	if err != nil {
		return nil, fmt.Errorf("modal image %s: %w", o.Image, err)
	}
	return &modalSDK{
		client: client,
		app:    app,
		image:  image,
		params: modal.SandboxCreateParams{
			Command:                    o.Command,
			Regions:                    o.Regions,
			CPU:                        o.CPU,
			MemoryMiB:                  o.MemoryMiB,
			Timeout:                    o.Timeout,
			Tags:                       o.Tags,
			ExperimentalEnableSnapshot: true,
		},
	}, nil
}

// modalSDK is ModalAPI over Modal's Go SDK.
type modalSDK struct {
	client *modal.Client
	app    *modal.App
	image  *modal.Image
	params modal.SandboxCreateParams
}

func (m *modalSDK) Create(ctx context.Context) (ModalSandbox, error) {
	params := m.params
	// Only the V2 backend, which ExperimentalCreate always uses, snapshots
	// memory.
	sb, err := m.client.Sandboxes.ExperimentalCreate(ctx, m.app, m.image, &params)
	if err != nil {
		return nil, err
	}
	return modalSDKSandbox{sb}, nil
}

func (m *modalSDK) Restore(ctx context.Context, snapshotID string) (ModalSandbox, error) {
	snapshot, err := m.client.SandboxSnapshots.FromID(ctx, snapshotID, nil)
	if err != nil {
		return nil, err
	}
	sb, err := m.client.Sandboxes.ExperimentalFromSnapshot(ctx, snapshot, nil)
	if err != nil {
		return nil, err
	}
	return modalSDKSandbox{sb}, nil
}

type modalSDKSandbox struct{ sb *modal.Sandbox }

func (m modalSDKSandbox) ID() string { return m.sb.SandboxID }

func (m modalSDKSandbox) Snapshot(ctx context.Context) (string, error) {
	snapshot, err := m.sb.ExperimentalSnapshot(ctx, nil)
	if err != nil {
		return "", err
	}
	return snapshot.SnapshotID, nil
}

func (m modalSDKSandbox) Terminate(ctx context.Context) error {
	if _, err := m.sb.Terminate(ctx, nil); err != nil {
		return err
	}
	// Its connection is not used again.
	return m.sb.Detach()
}

func (m modalSDKSandbox) Exited(ctx context.Context) (bool, error) {
	code, err := m.sb.Poll(ctx, nil)
	return code != nil, err
}

func (m modalSDKSandbox) Connect(ctx context.Context, port int) (string, string, error) {
	creds, err := m.sb.CreateConnectToken(ctx, &modal.SandboxCreateConnectTokenParams{Port: port})
	if err != nil {
		return "", "", err
	}
	return creds.URL, creds.Token, nil
}
