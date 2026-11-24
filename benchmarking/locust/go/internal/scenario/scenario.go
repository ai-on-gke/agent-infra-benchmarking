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

// Package scenario holds the settings every scenario run on harness shares,
// whatever its VUs do per iteration: where the actors live, which workload
// they run, on which provider, and the timing around them. Setup resolves
// them once at startup, and a scenario adds its own settings beside them. The
// worker's flags fill them in, in benchmarking/locust/go/cmd/boomer-worker.
package scenario

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"

	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/measure"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/workload"
	"go.ate.dev/substrate/api/ateapi/v1"
	"go.ate.dev/substrate/internal/benchmarking/boomer/userclass"
)

// DefaultTimeout is the per-call deadline when Options sets none. Exported
// for the worker's --workload-timeout usage string.
const DefaultTimeout = 30 * time.Second

// Resume modes. Explicit issues a ResumeActor before the instruction;
// implicit leaves the wake to the router.
const (
	ResumeModeExplicit = "explicit"
	ResumeModeImplicit = "implicit"
)

// Suspend modes. Async has SuspendActor return once the snapshot is on the
// node and upload it in the background; sync once it is uploaded.
const (
	SuspendModeAsync = "async"
	SuspendModeSync  = "sync"
)

var suspendModes = map[string]ateapi.DurabilityMode{
	SuspendModeAsync: ateapi.DurabilityMode_DURABILITY_MODE_ASYNCHRONOUS,
	SuspendModeSync:  ateapi.DurabilityMode_DURABILITY_MODE_SYNCHRONOUS,
}

// Options are the settings every scenario takes. The zero value asks for the
// defaults.
type Options struct {
	// Config gives the run's atespace, its router and the client to reach
	// it, the tracer, and the connection to substrate's API used when
	// APIStubs is empty.
	Config *userclass.Config

	// Provider names the infrastructure provider: agent-substrate,
	// agent-sandbox or e2b.
	Provider string

	// E2B is the e2b provider's settings.
	E2B E2BOptions

	// WarmPoolNames are the agent-sandbox SandboxWarmPools the users are
	// spread over.
	WarmPoolNames []string

	// APIStubs are the connections to substrate's API the users are spread
	// over, one each in turn. Empty uses the Config's one.
	APIStubs []ateapi.ControlClient

	// WorkloadName names a registered workload (see internal/workload). Empty
	// selects workload.Default.
	WorkloadName string

	// Template overrides the ActorTemplate the workload would run on. Empty
	// leaves that choice to the workload.
	Template string

	// Timeout is the deadline on each control-plane call and each instruction;
	// one that runs past it fails. Zero selects DefaultTimeout.
	Timeout time.Duration

	// MinWait and MaxWait bound the think time between iterations. An upper
	// bound at or below the lower one waits MinWait exactly.
	MinWait time.Duration
	MaxWait time.Duration

	// ResumeMode is ResumeModeExplicit or ResumeModeImplicit; empty selects
	// explicit.
	ResumeMode string

	// SuspendMode is SuspendModeAsync or SuspendModeSync; empty leaves it to
	// substrate, which suspends asynchronously.
	SuspendMode string

	// EgressAllow names the hosts every actor may connect to over cleartext
	// HTTP, and EgressAllowTLS the ones it may reach over TLS, such as an
	// https:// endpoint, which a cleartext rule does not let through. Both
	// empty gives the actors no egress policy, which substrate's egress
	// gateway treats as deny-all. Setup checks them and leaves them as
	// sandbox.EgressHosts returns them.
	EgressAllow    []string
	EgressAllowTLS []string

	// Flags are the worker's, holding the workloads' own (see
	// workload.RegisterFlags). Nil skips the check that none of another
	// workload's is set.
	Flags *flag.FlagSet
}

// E2BOptions are the e2b provider's settings; its API key is read from
// E2B_API_KEY. The zero value asks for the defaults.
type E2BOptions struct {
	// APIURL is the E2B API, and Domain the one sandboxes are served on.
	APIURL string
	Domain string

	// Port is the one the workload listens on inside the sandbox.
	Port int

	// CPUCount and MemoryMB size the sandboxes; zero leaves the template's.
	CPUCount int
	MemoryMB int

	// Lifetime is the TTL a running sandbox gets on each create and wake.
	Lifetime time.Duration
}

// Common is Options as Setup resolved them.
type Common struct {
	Options

	// Workload is the workload WorkloadName selects.
	Workload workload.Workload
}

// Setup resolves o once at startup: an unselectable workload or an invalid
// host is a configuration mistake, and reporting it after producing rows has
// already wasted the run, so either ends the process.
func Setup(o Options) Common {
	wl, err := workload.Resolve(o.WorkloadName)
	if err != nil {
		slog.Error("fatal: cannot select the workload", slog.String("err", err.Error()))
		os.Exit(1)
	}
	if err := workload.Configure(context.Background(), wl, o.Flags); err != nil {
		slog.Error("fatal: cannot configure the workload", slog.String("workload", wl.Name()), slog.String("err", err.Error()))
		os.Exit(1)
	}
	if o.EgressAllow, err = sandbox.EgressHosts(o.EgressAllow); err != nil {
		slog.Error("fatal: invalid --actor-egress-allow", slog.String("err", err.Error()))
		os.Exit(1)
	}
	if o.EgressAllowTLS, err = sandbox.EgressHosts(o.EgressAllowTLS); err != nil {
		slog.Error("fatal: invalid --actor-egress-allow-tls", slog.String("err", err.Error()))
		os.Exit(1)
	}
	if len(o.EgressAllow)+len(o.EgressAllowTLS) > 0 {
		slog.Info("actors may connect to", slog.Any("hosts", o.EgressAllow), slog.Any("tls_hosts", o.EgressAllowTLS))
	}
	if o.Timeout == 0 {
		o.Timeout = DefaultTimeout
	}
	return Common{Options: o, Workload: wl}
}

// ActorTemplate is the ActorTemplate the run's actors are created from.
func (c Common) ActorTemplate() string {
	if c.Template != "" {
		return c.Template
	}
	return c.Workload.DefaultTemplate()
}

// NewProvider makes the run's sandboxes on the provider c names, their calls
// reported through rec. suspendDisabled has substrate skip the suspend phase,
// and waitForWorker wait for a free worker on a resume that finds none.
func (c Common) NewProvider(rec measure.Recorder, suspendDisabled, waitForWorker bool) (sandbox.Provider, error) {
	if c.Provider == "e2b" {
		return c.newE2B(rec, suspendDisabled)
	}
	if c.Provider == "agent-sandbox" {
		if len(c.WarmPoolNames) == 0 {
			return nil, errors.New("agent-sandbox needs a --warm-pool-name to claim from")
		}
		dc, err := sandbox.NewKubeDynamicClient()
		if err != nil {
			return nil, err
		}
		return &sandbox.AgentSandbox{
			Config:    c.Config,
			Client:    dc,
			Rec:       rec,
			Workload:  c.Workload.Name(),
			WarmPools: c.WarmPoolNames,
		}, nil
	}
	return &sandbox.Substrate{
		Config:          c.Config,
		Rec:             rec,
		APIStubs:        c.APIStubs,
		Workload:        c.Workload.Name(),
		Template:        c.ActorTemplate(),
		EgressHosts:     c.EgressAllow,
		EgressTLSHosts:  c.EgressAllowTLS,
		ImplicitResume:  c.ResumeMode == ResumeModeImplicit,
		SuspendDisabled: suspendDisabled,
		SuspendMode:     suspendModes[c.SuspendMode],
		WaitForWorker:   waitForWorker,
	}, nil
}

func (c Common) newE2B(rec measure.Recorder, suspendDisabled bool) (sandbox.Provider, error) {
	key := os.Getenv("E2B_API_KEY")
	if key == "" {
		return nil, errors.New("e2b needs its API key in E2B_API_KEY")
	}
	o := c.E2B
	if o.APIURL == "" {
		o.APIURL = sandbox.E2BDefaultAPIURL
	}
	if o.Domain == "" {
		o.Domain = sandbox.E2BDefaultDomain
	}
	if o.Port == 0 {
		o.Port = 80
	}
	if o.Lifetime == 0 {
		o.Lifetime = 10 * time.Minute
	}
	return &sandbox.E2B{
		Rec:         rec,
		APIURL:      strings.TrimSuffix(o.APIURL, "/"),
		APIKey:      key,
		API:         &http.Client{Transport: http.DefaultTransport.(*http.Transport).Clone()},
		ActorClient: c.Config.HTTPClient,
		Domain:      o.Domain,
		Port:        o.Port,
		Workload:    c.Workload.Name(),
		Template:    c.ActorTemplate(),
		CPUCount:    o.CPUCount,
		MemoryMB:    o.MemoryMB,
		Lifetime:    o.Lifetime,
		Metadata: map[string]string{
			"benchmark-user": rec.UserClass,
			"atespace":       c.Config.Atespace,
		},
		SuspendDisabled: suspendDisabled,
	}, nil
}
