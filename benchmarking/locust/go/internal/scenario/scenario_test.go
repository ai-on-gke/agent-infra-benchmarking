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

package scenario

import (
	"net/http"
	"slices"
	"testing"
	"time"

	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/measure"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/workload"
	"go.ate.dev/substrate/internal/benchmarking/boomer/userclass"
)

// The zero value asks for the defaults.
func TestSetupFillsInTheDefaults(t *testing.T) {
	c := Setup(Options{})

	if got := c.Workload.Name(); got != workload.Default {
		t.Errorf("workload: got %q, want %q", got, workload.Default)
	}
	if c.Timeout != DefaultTimeout {
		t.Errorf("timeout: got %s, want %s", c.Timeout, DefaultTimeout)
	}
	if got, want := c.ActorTemplate(), c.Workload.DefaultTemplate(); got != want {
		t.Errorf("template: got %q, want the workload's %q", got, want)
	}
}

func TestSetupKeepsWhatIsSet(t *testing.T) {
	c := Setup(Options{
		WorkloadName:   workload.GluttonName,
		Template:       "some-other-template",
		Timeout:        time.Second,
		EgressAllow:    []string{"B.svc", "a.svc", "a.svc"},
		EgressAllowTLS: []string{"api.example.com"},
	})

	if got := c.ActorTemplate(); got != "some-other-template" {
		t.Errorf("template: got %q, want %q", got, "some-other-template")
	}
	if c.Timeout != time.Second {
		t.Errorf("timeout: got %s, want %s", c.Timeout, time.Second)
	}
	// As sandbox.EgressHosts returns them.
	if want := []string{"a.svc", "b.svc"}; !slices.Equal(c.EgressAllow, want) {
		t.Errorf("egress hosts: got %v, want %v", c.EgressAllow, want)
	}
	if want := []string{"api.example.com"}; !slices.Equal(c.EgressAllowTLS, want) {
		t.Errorf("egress TLS hosts: got %v, want %v", c.EgressAllowTLS, want)
	}
}

// Anything but agent-sandbox runs on substrate, as the settings say.
func TestNewProviderDefaultsToSubstrate(t *testing.T) {
	cfg := &userclass.Config{Atespace: "benchmark"}
	c := Setup(Options{
		Config:      cfg,
		ResumeMode:  ResumeModeImplicit,
		EgressAllow: []string{"collector.svc"},
	})
	rec := measure.Recorder{UserClass: "SomeUser"}

	p, err := c.NewProvider(rec, true, true)
	if err != nil {
		t.Fatalf("NewProvider: %v", err)
	}
	s, ok := p.(*sandbox.Substrate)
	if !ok {
		t.Fatalf("provider: got %T, want *sandbox.Substrate", p)
	}
	if s.Config != cfg || s.Rec != rec {
		t.Error("the provider does not use the run's Config and the recorder given")
	}
	if s.Workload != c.Workload.Name() || s.Template != c.ActorTemplate() {
		t.Errorf("workload and template: got %q and %q, want %q and %q", s.Workload, s.Template, c.Workload.Name(), c.ActorTemplate())
	}
	if !slices.Equal(s.EgressHosts, c.EgressAllow) {
		t.Errorf("egress hosts: got %v, want %v", s.EgressHosts, c.EgressAllow)
	}
	if !s.ImplicitResume || !s.SuspendDisabled || !s.WaitForWorker {
		t.Errorf("ImplicitResume, SuspendDisabled and WaitForWorker: got %t, %t and %t, want all",
			s.ImplicitResume, s.SuspendDisabled, s.WaitForWorker)
	}
}

// e2b takes its key from the environment and fills in the defaults left unset.
func TestNewProviderE2B(t *testing.T) {
	cfg := &userclass.Config{Atespace: "benchmark", HTTPClient: &http.Client{}}
	c := Setup(Options{Config: cfg, Provider: "e2b", E2B: E2BOptions{CPUCount: 2}})
	rec := measure.Recorder{UserClass: "SomeUser"}

	t.Setenv("E2B_API_KEY", "")
	if _, err := c.NewProvider(rec, false, false); err == nil {
		t.Fatal("NewProvider without E2B_API_KEY: got nil, want an error")
	}

	t.Setenv("E2B_API_KEY", "key")
	p, err := c.NewProvider(rec, true, false)
	if err != nil {
		t.Fatalf("NewProvider: %v", err)
	}
	e, ok := p.(*sandbox.E2B)
	if !ok {
		t.Fatalf("provider: got %T, want *sandbox.E2B", p)
	}
	if e.APIKey != "key" || e.APIURL != sandbox.E2BDefaultAPIURL || e.Domain != sandbox.E2BDefaultDomain || e.Port != 80 {
		t.Errorf("API and routing: got key %q, url %q, domain %q, port %d", e.APIKey, e.APIURL, e.Domain, e.Port)
	}
	if e.Template != c.ActorTemplate() || e.CPUCount != 2 || !e.SuspendDisabled || e.ActorClient != cfg.HTTPClient {
		t.Errorf("sandbox settings: got template %q, cpu %d, suspend disabled %t", e.Template, e.CPUCount, e.SuspendDisabled)
	}
}

// daytona takes its key from the environment and fills in the defaults left
// unset.
func TestNewProviderDaytona(t *testing.T) {
	cfg := &userclass.Config{Atespace: "benchmark", HTTPClient: &http.Client{}}
	c := Setup(Options{Config: cfg, Provider: "daytona", Daytona: DaytonaOptions{MemoryGB: 4, Target: "us"}})
	rec := measure.Recorder{UserClass: "SomeUser"}

	t.Setenv("DAYTONA_API_KEY", "")
	if _, err := c.NewProvider(rec, false, false); err == nil {
		t.Fatal("NewProvider without DAYTONA_API_KEY: got nil, want an error")
	}

	t.Setenv("DAYTONA_API_KEY", "key")
	p, err := c.NewProvider(rec, false, false)
	if err != nil {
		t.Fatalf("NewProvider: %v", err)
	}
	d, ok := p.(*sandbox.Daytona)
	if !ok {
		t.Fatalf("provider: got %T, want *sandbox.Daytona", p)
	}
	if d.APIKey != "key" || d.APIURL != sandbox.DaytonaDefaultAPIURL || d.Port != 80 || d.Target != "us" {
		t.Errorf("API and routing: got key %q, url %q, port %d, target %q", d.APIKey, d.APIURL, d.Port, d.Target)
	}
	if d.Snapshot != c.ActorTemplate() || d.MemoryGB != 4 || d.TTL != 0 || d.ActorClient != cfg.HTTPClient {
		t.Errorf("sandbox settings: got snapshot %q, memory %d, ttl %v", d.Snapshot, d.MemoryGB, d.TTL)
	}
}
