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
