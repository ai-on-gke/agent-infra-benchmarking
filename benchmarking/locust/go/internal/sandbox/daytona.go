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
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"time"

	"github.com/google/uuid"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/measure"
	utilnet "k8s.io/apimachinery/pkg/util/net"
)

// DaytonaDefaultAPIURL is Daytona's hosted API.
const DaytonaDefaultAPIURL = "https://app.daytona.io/api"

// The header a private sandbox's preview token goes in.
const daytonaPreviewTokenHeader = "X-Daytona-Preview-Token"

// DaytonaPoll is how often a call waiting for a sandbox to reach a state looks
// at it again. Daytona's start and pause return once the transition is under
// way, so the wait is part of the latency they report.
var DaytonaPoll = 100 * time.Millisecond

// Daytona creates each sandbox on Daytona through its REST API, and cycles it
// with start (which resumes a paused sandbox) and pause. Pause keeps memory
// only for VM sandboxes, so the snapshot has to be a linux-vm one.
type Daytona struct {
	Rec measure.Recorder

	// APIURL is the Daytona API, APIKey its key, and API the client the
	// calls to it go over.
	APIURL string
	APIKey string
	API    *http.Client

	// ActorClient is the client a workload reaches a sandbox over, and Port
	// the one the workload listens on inside the sandbox.
	ActorClient *http.Client
	Port        int

	// Workload labels the actor gauges, Snapshot is the Daytona snapshot the
	// sandboxes are created from, and Target the region they run in; empty
	// leaves the organization's default.
	Workload string
	Snapshot string
	Target   string

	// CPU and MemoryGB size the sandboxes; zero leaves the snapshot's.
	CPU      int
	MemoryGB int

	// TTL destroys a sandbox that long after its creation, whatever its
	// state, so the ones a crashed worker leaves are cleaned up. Zero leaves
	// Daytona's default.
	TTL time.Duration

	// Labels label every sandbox, so the ones a run leaves can be found.
	Labels map[string]string

	// SuspendDisabled keeps the sandboxes running after their first Wake.
	SuspendDisabled bool
}

// Create makes the sandbox, waits for it to start, and pauses it, so the
// first Wake resumes it like every other.
func (p *Daytona) Create(ctx context.Context) (Sandbox, error) {
	s := &daytonaSandbox{p: p}
	// Daytona takes no idempotency key, so a retried create first looks for
	// the sandbox an earlier attempt may have made, by its name.
	name := "benchmark-" + uuid.NewString()
	body := map[string]any{
		"name":     name,
		"snapshot": p.Snapshot,
		"labels":   p.Labels,
		"public":   false,
	}
	if p.Target != "" {
		body["target"] = p.Target
	}
	if p.CPU > 0 {
		body["cpu"] = p.CPU
	}
	if p.MemoryGB > 0 {
		body["memory"] = p.MemoryGB
	}
	if p.TTL > 0 {
		body["ttlMinutes"] = max(1, int((p.TTL+time.Minute-1)/time.Minute))
	}

	attempted := false
	err := p.Rec.HTTP(ctx, "CreateSandbox", retryableDaytona, func(ctx context.Context) error {
		var info daytonaSandboxInfo
		found := false
		if attempted {
			err := p.call(ctx, http.MethodGet, "/sandbox/"+url.PathEscape(name), nil, &info)
			if err != nil && daytonaStatus(err) != http.StatusNotFound {
				return err
			}
			found = err == nil
		}
		attempted = true
		if !found {
			if err := p.call(ctx, http.MethodPost, "/sandbox", body, &info); err != nil {
				return err
			}
		}
		s.id = info.ID
		return s.waitFor(ctx, "started", info.State)
	})
	if err != nil {
		s.cleanUp(ctx)
		return nil, err
	}

	var preview daytonaPreview
	err = p.Rec.HTTP(ctx, "GetPreviewURL", retryableDaytona, func(ctx context.Context) error {
		return p.call(ctx, http.MethodGet, s.path(fmt.Sprintf("/ports/%d/preview-url", p.Port)), nil, &preview)
	})
	if err == nil {
		s.previewURL, s.previewToken = preview.URL, preview.Token
		err = s.pause(ctx, "PauseSandbox")
	}
	if err != nil {
		s.cleanUp(ctx)
		return nil, err
	}
	s.isCreated = true
	suspendedActors.WithLabelValues(p.Workload).Inc()
	return s, nil
}

type daytonaSandbox struct {
	p  *Daytona
	id string

	// A paused sandbox keeps its preview URL and token when resumed.
	previewURL   string
	previewToken string

	isActive  bool
	isCreated bool
}

// daytonaSandboxInfo is the part of Daytona's Sandbox the benchmark reads.
type daytonaSandboxInfo struct {
	ID          string `json:"id"`
	State       string `json:"state"`
	ErrorReason string `json:"errorReason"`
}

type daytonaPreview struct {
	URL   string `json:"url"`
	Token string `json:"token"`
}

func (s *daytonaSandbox) Actor() Actor {
	h := http.Header{}
	h.Set(daytonaPreviewTokenHeader, s.previewToken)
	return Actor{
		HTTPClient: s.p.ActorClient,
		RouterURL:  s.previewURL,
		Name:       s.id,
		Header:     h,
	}
}

// Wake starts the sandbox, which resumes it from paused, and waits for it to
// run.
func (s *daytonaSandbox) Wake(ctx context.Context) error {
	if s.p.SuspendDisabled && s.isActive {
		return nil
	}
	err := s.p.Rec.HTTP(ctx, ResumeMetric, retryableDaytona, func(ctx context.Context) error {
		return s.transition(ctx, "/start", "started")
	})
	if err != nil {
		return err
	}
	s.markActive()
	return nil
}

func (s *daytonaSandbox) Sleep(ctx context.Context) error {
	if s.p.SuspendDisabled {
		return nil
	}
	if err := s.pause(ctx, SuspendMetric); err != nil {
		slog.Warn("pause failed; the sandbox may be left mid transition",
			slog.String("sandbox", s.id),
			slog.String("err", err.Error()))
		return err
	}
	s.markSuspended()
	return nil
}

func (s *daytonaSandbox) pause(ctx context.Context, row string) error {
	return s.p.Rec.HTTP(ctx, row, retryableDaytona, func(ctx context.Context) error {
		return s.transition(ctx, "/pause", "paused")
	})
}

// transition asks for the sandbox's next state with the action at suffix and
// waits for it to reach want.
func (s *daytonaSandbox) transition(ctx context.Context, suffix, want string) error {
	var info daytonaSandboxInfo
	if err := s.p.call(ctx, http.MethodPost, s.path(suffix), nil, &info); err != nil {
		return err
	}
	return s.waitFor(ctx, want, info.State)
}

// waitFor polls the sandbox until it is in state want, starting from the
// state it was last seen in. Its errors are never retryable: the action that
// led to the wait is not to be asked for again.
func (s *daytonaSandbox) waitFor(ctx context.Context, want, state string) error {
	ctx, cancel := context.WithTimeout(ctx, SettleTimeout)
	defer cancel()
	var lastErr error
	for state != want {
		if daytonaBroken(state) {
			return fmt.Errorf("daytona: sandbox %s is %s, want %s", s.id, state, want)
		}
		select {
		case <-ctx.Done():
			if lastErr != nil {
				return fmt.Errorf("daytona: sandbox %s not %s after %v: %v", s.id, want, SettleTimeout, lastErr)
			}
			return fmt.Errorf("daytona: sandbox %s still %s after %v", s.id, state, SettleTimeout)
		case <-time.After(DaytonaPoll):
		}
		var info daytonaSandboxInfo
		lastErr = s.p.call(ctx, http.MethodGet, s.path(""), nil, &info)
		switch {
		case daytonaStatus(lastErr) == http.StatusNotFound:
			return fmt.Errorf("daytona: sandbox %s is gone", s.id)
		case lastErr == nil:
			state = info.State
		}
	}
	return nil
}

// daytonaBroken is a state the sandbox does not leave for started or paused
// on its own.
func daytonaBroken(state string) bool {
	switch state {
	case "error", "build_failed", "destroying", "destroyed":
		return true
	}
	return false
}

// Settle polls the sandbox until it is paused, pausing it, unmetered, while
// it runs, or leaves it running when suspends are disabled. A sandbox that is
// gone, broken, stopped, or not paused after SettleTimeout is not cycled
// again.
func (s *daytonaSandbox) Settle(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, SettleTimeout)
	defer cancel()
	for {
		var info daytonaSandboxInfo
		err := s.p.call(ctx, http.MethodGet, s.path(""), nil, &info)
		switch {
		case daytonaStatus(err) == http.StatusNotFound:
			return fmt.Errorf("sandbox is gone: %w", err)
		case err != nil:
			// Tried again until the bound, as a refused read says nothing
			// about the sandbox.
		case info.State == "paused":
			s.markSuspended()
			return nil
		case info.State == "started" && s.p.SuspendDisabled:
			s.markActive()
			return nil
		case info.State == "started":
			// The pause is under way once accepted, and a refused one is
			// asked for again on the next poll.
			_ = s.p.call(ctx, http.MethodPost, s.path("/pause"), nil, nil)
		case daytonaBroken(info.State), info.State == "stopped", info.State == "archived":
			return fmt.Errorf("sandbox is %s %s", info.State, info.ErrorReason)
		}

		select {
		case <-ctx.Done():
			if err != nil {
				return fmt.Errorf("sandbox state unknown after %v: %w", SettleTimeout, err)
			}
			return fmt.Errorf("sandbox still %s after %v", info.State, SettleTimeout)
		case <-time.After(SettlePoll):
		}
	}
}

// Remove deletes the sandbox in whatever state it is.
func (s *daytonaSandbox) Remove(ctx context.Context) error {
	if err := s.delete(ctx); err != nil {
		return fmt.Errorf("delete: %w", err)
	}
	return nil
}

// cleanUp deletes a sandbox a failed Create made.
func (s *daytonaSandbox) cleanUp(ctx context.Context) {
	if s.id == "" {
		return
	}
	if err := s.delete(ctx); err != nil {
		slog.Warn("sandbox a failed create made was left behind",
			slog.String("sandbox", s.id),
			slog.String("err", err.Error()))
	}
}

func (s *daytonaSandbox) delete(ctx context.Context) error {
	err := s.p.Rec.HTTP(ctx, "DeleteSandbox", retryableDaytona, func(ctx context.Context) error {
		err := s.p.call(ctx, http.MethodDelete, s.path(""), nil, nil)
		// Already gone, which is what the call is for.
		if daytonaStatus(err) == http.StatusNotFound {
			return nil
		}
		return err
	})
	// The gauges drop whether or not the delete worked, since the sandbox is
	// not used again either way.
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

func (s *daytonaSandbox) markActive() {
	if !s.isActive {
		suspendedActors.WithLabelValues(s.p.Workload).Dec()
		activeActors.WithLabelValues(s.p.Workload).Inc()
		s.isActive = true
	}
}

func (s *daytonaSandbox) markSuspended() {
	if s.isActive {
		activeActors.WithLabelValues(s.p.Workload).Dec()
		suspendedActors.WithLabelValues(s.p.Workload).Inc()
		s.isActive = false
	}
}

func (s *daytonaSandbox) path(suffix string) string {
	return "/sandbox/" + url.PathEscape(s.id) + suffix
}

// DaytonaError is a call the Daytona API answered with an error status.
type DaytonaError struct {
	Status  int
	Message string
}

func (e *DaytonaError) Error() string {
	return fmt.Sprintf("daytona: %d: %s", e.Status, e.Message)
}

// daytonaStatus is the status a Daytona call was answered with, 0 for none.
func daytonaStatus(err error) int {
	var e *DaytonaError
	if errors.As(err, &e) {
		return e.Status
	}
	return 0
}

// retryableDaytona is a Daytona call worth making again: one rate limited,
// one the API could not serve right now, or one whose connection dropped.
func retryableDaytona(err error) bool {
	switch daytonaStatus(err) {
	case http.StatusTooManyRequests, http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		return true
	}
	return utilnet.IsConnectionReset(err) || utilnet.IsProbableEOF(err)
}

// call makes one request to the Daytona API, sending body as JSON when it is
// not nil and decoding a successful answer into out when that is not nil.
func (p *Daytona) call(ctx context.Context, method, path string, body, out any) error {
	var reader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, p.APIURL+path, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+p.APIKey)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := p.API.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		e := &DaytonaError{Status: resp.StatusCode}
		var answer struct {
			Message string `json:"message"`
		}
		if json.Unmarshal(data, &answer) == nil && answer.Message != "" {
			e.Message = answer.Message
		} else {
			e.Message = string(data)
		}
		return e
	}
	if out != nil && len(data) > 0 {
		return json.Unmarshal(data, out)
	}
	return nil
}
