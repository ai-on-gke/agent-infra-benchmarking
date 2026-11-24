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

// E2B defaults: its hosted API, and the domain its sandboxes are reached on.
const (
	E2BDefaultAPIURL = "https://api.e2b.app"
	E2BDefaultDomain = "e2b.app"
)

// The header a sandbox's traffic token goes in, for a sandbox that does not
// take public traffic.
const e2bTrafficTokenHeader = "E2B-Traffic-Access-Token"

// E2B creates each sandbox on E2B through its REST API, and cycles it with
// connect (which resumes a paused sandbox) and pause.
type E2B struct {
	Rec measure.Recorder

	// APIURL is the E2B API, APIKey its key, and API the client the calls
	// to it go over.
	APIURL string
	APIKey string
	API    *http.Client

	// ActorClient is the client a workload reaches a sandbox over, Domain
	// the domain sandboxes are served on, and Port the one the workload
	// listens on inside the sandbox.
	ActorClient *http.Client
	Domain      string
	Port        int

	// Workload labels the actor gauges, and Template is the E2B template ID
	// or alias the sandboxes are created from.
	Workload string
	Template string

	// CPUCount and MemoryMB size the sandboxes; zero leaves the template's.
	CPUCount int
	MemoryMB int

	// Lifetime is the TTL each create and connect gives a running sandbox,
	// so one a crashed worker leaves running is killed. A paused sandbox
	// keeps none.
	Lifetime time.Duration

	// Metadata labels every sandbox, so the ones a run leaves can be found.
	Metadata map[string]string

	// SuspendDisabled keeps the sandboxes running after their first Wake.
	SuspendDisabled bool
}

// Create makes the sandbox, which E2B starts running, and pauses it, so the
// first Wake resumes it like every other.
func (p *E2B) Create(ctx context.Context) (Sandbox, error) {
	s := &e2bSandbox{p: p}
	body := map[string]any{
		"templateID": p.Template,
		"timeout":    p.lifetimeSeconds(),
		"metadata":   p.Metadata,
	}
	if p.CPUCount > 0 {
		body["cpuCount"] = p.CPUCount
	}
	if p.MemoryMB > 0 {
		body["memoryMB"] = p.MemoryMB
	}
	// The same key on every retry, so a create that landed is not made twice.
	header := http.Header{"Idempotency-Key": {uuid.NewString()}}

	var created e2bSandboxInfo
	err := p.Rec.HTTP(ctx, "CreateSandbox", retryableE2B, func(ctx context.Context) error {
		return p.call(ctx, http.MethodPost, "/v2/sandboxes", header, body, &created)
	})
	if err != nil {
		return nil, err
	}
	s.id, s.trafficToken = created.SandboxID, created.TrafficAccessToken

	if err := s.pause(ctx, "PauseSandbox"); err != nil {
		if delErr := s.kill(ctx); delErr != nil {
			slog.Warn("sandbox that did not pause was left behind",
				slog.String("sandbox", s.id),
				slog.String("err", delErr.Error()))
		}
		return nil, err
	}
	s.isCreated = true
	suspendedActors.WithLabelValues(p.Workload).Inc()
	return s, nil
}

func (p *E2B) lifetimeSeconds() int { return max(1, int(p.Lifetime/time.Second)) }

type e2bSandbox struct {
	p            *E2B
	id           string
	trafficToken string

	isActive  bool
	isCreated bool
}

// e2bSandboxInfo is the part of E2B's Sandbox and SandboxDetail the benchmark
// reads.
type e2bSandboxInfo struct {
	SandboxID          string `json:"sandboxID"`
	TrafficAccessToken string `json:"trafficAccessToken"`
	State              string `json:"state"`
}

func (s *e2bSandbox) Actor() Actor {
	h := http.Header{}
	if s.trafficToken != "" {
		h.Set(e2bTrafficTokenHeader, s.trafficToken)
	}
	return Actor{
		HTTPClient: s.p.ActorClient,
		RouterURL:  fmt.Sprintf("https://%d-%s.%s", s.p.Port, s.id, s.p.Domain),
		Name:       s.id,
		Header:     h,
	}
}

// Wake connects to the sandbox, which resumes it when paused and renews its
// TTL either way.
func (s *e2bSandbox) Wake(ctx context.Context) error {
	if s.p.SuspendDisabled && s.isActive {
		return nil
	}
	var info e2bSandboxInfo
	err := s.p.Rec.HTTP(ctx, ResumeMetric, retryableE2B, func(ctx context.Context) error {
		return s.p.call(ctx, http.MethodPost, s.path("/connect", true), nil,
			map[string]any{"timeout": s.p.lifetimeSeconds()}, &info)
	})
	if err != nil {
		return err
	}
	if info.TrafficAccessToken != "" {
		s.trafficToken = info.TrafficAccessToken
	}
	s.markActive()
	return nil
}

func (s *e2bSandbox) Sleep(ctx context.Context) error {
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

func (s *e2bSandbox) pause(ctx context.Context, row string) error {
	return s.p.Rec.HTTP(ctx, row, retryableE2B, func(ctx context.Context) error {
		return s.p.call(ctx, http.MethodPost, s.path("/pause", false), nil, nil, nil)
	})
}

// Settle polls the sandbox until it is paused, pausing it, unmetered, while
// it runs, or leaves it running when suspends are disabled. A sandbox that is
// gone, or not paused after SettleTimeout, is not cycled again.
func (s *e2bSandbox) Settle(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, SettleTimeout)
	defer cancel()
	for {
		var info e2bSandboxInfo
		err := s.p.call(ctx, http.MethodGet, s.path("", false), nil, nil, &info)
		switch {
		case e2bStatus(err) == http.StatusNotFound:
			return fmt.Errorf("sandbox is gone: %w", err)
		case err != nil:
			// Tried again until the bound, as a refused read says nothing
			// about the sandbox.
		case info.State == "paused":
			s.markSuspended()
			return nil
		case info.State == "running" && s.p.SuspendDisabled:
			s.markActive()
			return nil
		case info.State == "running":
			// A pause refused while another is still under way is looked at
			// again on the next poll.
			if err = s.p.call(ctx, http.MethodPost, s.path("/pause", false), nil, nil, nil); err == nil {
				s.markSuspended()
				return nil
			}
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

// Remove kills the sandbox in whatever state it is.
func (s *e2bSandbox) Remove(ctx context.Context) error {
	if err := s.kill(ctx); err != nil {
		return fmt.Errorf("kill: %w", err)
	}
	return nil
}

func (s *e2bSandbox) kill(ctx context.Context) error {
	err := s.p.Rec.HTTP(ctx, "KillSandbox", retryableE2B, func(ctx context.Context) error {
		err := s.p.call(ctx, http.MethodDelete, s.path("", false), nil, nil, nil)
		// Already gone, which is what the call is for.
		if e2bStatus(err) == http.StatusNotFound {
			return nil
		}
		return err
	})
	// The gauges drop whether or not the kill worked, since the sandbox is
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

func (s *e2bSandbox) markActive() {
	if !s.isActive {
		suspendedActors.WithLabelValues(s.p.Workload).Dec()
		activeActors.WithLabelValues(s.p.Workload).Inc()
		s.isActive = true
	}
}

func (s *e2bSandbox) markSuspended() {
	if s.isActive {
		activeActors.WithLabelValues(s.p.Workload).Dec()
		suspendedActors.WithLabelValues(s.p.Workload).Inc()
		s.isActive = false
	}
}

// path is the sandbox's API path with suffix, under /v2 for the calls that
// have a v2.
func (s *e2bSandbox) path(suffix string, v2 bool) string {
	p := "/sandboxes/" + url.PathEscape(s.id) + suffix
	if v2 {
		p = "/v2" + p
	}
	return p
}

// E2BError is a call the E2B API answered with an error status.
type E2BError struct {
	Status    int
	ErrorCode string `json:"error_code"`
	Message   string `json:"message"`
}

func (e *E2BError) Error() string {
	if e.ErrorCode != "" {
		return fmt.Sprintf("e2b: %d %s: %s", e.Status, e.ErrorCode, e.Message)
	}
	return fmt.Sprintf("e2b: %d: %s", e.Status, e.Message)
}

// e2bStatus is the status an E2B call was answered with, 0 for none.
func e2bStatus(err error) int {
	var e *E2BError
	if errors.As(err, &e) {
		return e.Status
	}
	return 0
}

// retryableE2B is an E2B call worth making again: one rate limited, one the
// API could not serve right now, or one whose connection dropped.
func retryableE2B(err error) bool {
	switch e2bStatus(err) {
	case http.StatusTooManyRequests, http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		return true
	}
	return utilnet.IsConnectionReset(err) || utilnet.IsProbableEOF(err)
}

// call makes one request to the E2B API, sending body as JSON when it is not
// nil and decoding a successful answer into out when that is not nil.
func (p *E2B) call(ctx context.Context, method, path string, header http.Header, body, out any) error {
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
	for k, v := range header {
		req.Header[k] = v
	}
	req.Header.Set("X-API-Key", p.APIKey)
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
		e := &E2BError{Status: resp.StatusCode}
		if json.Unmarshal(data, e) != nil || e.Message == "" {
			e.Message = string(data)
		}
		return e
	}
	if out != nil && len(data) > 0 {
		return json.Unmarshal(data, out)
	}
	return nil
}
