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

// boomer-worker is the Go re-implementation of the GluttonUser locust test.
// It speaks the locust worker protocol via myzhan/boomer, so it appears as a
// regular worker to the Python locust master while sidestepping gevent's
// scheduling tax.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/boomerutil"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/dynconfig"
	bmetrics "github.com/agent-substrate/substrate/internal/benchmarking/boomer/metrics"
	btrace "github.com/agent-substrate/substrate/internal/benchmarking/boomer/trace"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/userclass"
	"github.com/myzhan/boomer"

	"github.com/agent-substrate/substrate/agent-infra-benchmarking/internal/benchmarking/boomer/agentsandboxutil"
	// Imported for the user class each registers via init(). runworkload is
	// named rather than blank because its settings are declared here, as
	// flags, and handed over with runworkload.SetOptions/SetAgentSandboxDeps.
	"github.com/agent-substrate/substrate/agent-infra-benchmarking/internal/benchmarking/boomer/runworkload"
	_ "github.com/agent-substrate/substrate/internal/benchmarking/boomer/glutton"
)

func main() {
	var (
		backend = flag.String("backend", string(runworkload.DefaultBackend),
			fmt.Sprintf("runworkload only: which sandbox control plane to drive, %q or %q.", runworkload.BackendSubstrate, runworkload.BackendAgentSandbox))

		// substrate backend settings.
		apiEndpoint = flag.String("api-endpoint", "k8s:///api.ate-system.svc.cluster.local:443", "substrate backend only: ateapi gRPC dial target.")
		routerURL   = flag.String("router-url", "http://atenet-router.ate-system.svc.cluster.local", "substrate backend only: atenet HTTP router base URL (no trailing slash).")
		atespace    = flag.String("atespace", "benchmark", "substrate backend only: atespace every actor this worker creates lives in. Ensured (CreateAtespace, AlreadyExists is ok) at startup.")

		// agent-sandbox backend settings.
		namespace = flag.String("namespace", "benchmarking", "agent-sandbox backend only: namespace every Sandbox/SandboxClaim this worker creates lives in.")

		promAddr  = flag.String("prometheus-addr", ":8001", "Address for the Prometheus /metrics endpoint.")
		userClass = flag.String("user-class", "glutton", fmt.Sprintf("Locust user class to run, lowercase; one of %s.", strings.Join(userclass.Names(), "|")))

		// Settings a run holds from start to end. They were fetched from the
		// locust master's /boomer-config while the web UI could change them
		// mid-run; nothing in this repo does, so they are flags.
		minWait          = flag.Duration("min-wait", 0, "Lower bound on the think time between iterations.")
		maxWait          = flag.Duration("max-wait", 500*time.Millisecond, "Upper bound on the think time between iterations. At or below --min-wait waits exactly that.")
		resumeMode       = flag.String("resume-mode", runworkload.ResumeModeExplicit, fmt.Sprintf("substrate backend only: how an actor is woken: %s issues ResumeActor before the instruction, %s leaves the wake to the router.", runworkload.ResumeModeExplicit, runworkload.ResumeModeImplicit))
		traceProbability = flag.Float64("trace-probability", 0, "Fraction of iterations to sample a trace for, 0 to 1.")

		// runworkload settings. Declared here with every other flag, so
		// --help is the whole command line rather than one list per user
		// class, and read only when --user-class selects runworkload. The
		// defaults are left empty so that the package resolves them: the
		// template's follows from the workload, which is the only place that
		// knows what it can address.
		workloadName = flag.String("workload-name", "",
			"runworkload only: the workload a RunWorkloadUser drives. Unset selects the chosen --backend's own default (glutton for substrate, ping for agent-sandbox).")
		workloadTemplate = flag.String("workload-template", "",
			"runworkload only: the ActorTemplate (substrate) or SandboxTemplate (agent-sandbox) the workload is measured on. Unset selects the workload's own.")
		workloadWarmPool = flag.String("workload-warmpool", "",
			"runworkload, --backend agent-sandbox only: the SandboxWarmPool the pool-claim window adopts from. Unset selects \"<template>-warmpool\".")
		workloadTimeout = flag.Duration("workload-timeout", 0,
			fmt.Sprintf("runworkload only: ceiling on the instruction retry loop. Unset selects %s.", runworkload.DefaultTimeout))
		workloadProvisionTimeout = flag.Duration("workload-provision-timeout", 0,
			fmt.Sprintf("runworkload, --backend agent-sandbox only: ceiling on waiting for a Sandbox/SandboxClaim's Ready condition. Unset selects %s.", runworkload.DefaultProvisionTimeout))
	)
	// boomer.Run will call flag.Parse() if we haven't yet; calling here so
	// our flag-derived values are usable before that.
	flag.Parse()

	class := strings.ToLower(*userClass)

	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))

	sandboxBackend := runworkload.Backend(*backend)
	switch sandboxBackend {
	case runworkload.BackendSubstrate, runworkload.BackendAgentSandbox:
	default:
		slog.Error("fatal: unknown --backend value",
			slog.String("backend", *backend),
			slog.String("known", fmt.Sprintf("%s,%s", runworkload.BackendSubstrate, runworkload.BackendAgentSandbox)))
		os.Exit(1)
	}

	// runworkload is the only user class that supports --backend agent-sandbox.
	// The other registered classes (glutton, durdir) are substrate-only and
	// dereference cfg.APIStub unconditionally, which is only built for --backend
	// substrate below. Reject the combination here rather than letting a
	// substrate class panic on a nil APIStub deep in a measured iteration.
	if sandboxBackend == runworkload.BackendAgentSandbox && class != "runworkload" {
		slog.Error("fatal: --backend agent-sandbox is only supported with --user-class runworkload",
			slog.String("user-class", class),
			slog.String("backend", string(sandboxBackend)))
		os.Exit(1)
	}

	switch *resumeMode {
	case runworkload.ResumeModeExplicit, runworkload.ResumeModeImplicit:
	default:
		slog.Error("fatal: unknown --resume-mode value",
			slog.String("resume_mode", *resumeMode),
			slog.String("known", runworkload.ResumeModeExplicit+","+runworkload.ResumeModeImplicit))
		os.Exit(1)
	}

	ctx := context.Background()
	sampler := btrace.NewUpdatableSampler(*traceProbability)
	tp, err := btrace.Init(ctx, "substrate-boomer", sampler)
	if err != nil {
		slog.Error("failed to initialize tracing", slog.String("err", err.Error()))
		os.Exit(1)
	}
	defer func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = tp.Shutdown(shutdownCtx)
	}()

	httpClient := &http.Client{Timeout: 5 * time.Minute}

	// cfg is substrate's own userclass.Config type (this module cannot add
	// fields to it). It is built and passed to every user class, but only the
	// substrate backend's dependencies (APIStub, RouterURL, Atespace) are
	// populated, and only when that backend is selected; runworkload's
	// agent-sandbox dependencies travel separately via SetAgentSandboxDeps.
	cfg := &userclass.Config{
		HTTPClient: httpClient,
		// runworkload reads its flags through SetOptions; the Dyn holder is
		// for the upstream user classes, which take the same values off the
		// holder that userclass.Config requires.
		Dyn: dynconfig.NewHolder(dynconfig.Config{
			MinWait:          *minWait,
			MaxWait:          *maxWait,
			ResumeMode:       *resumeMode,
			TraceProbability: *traceProbability,
		}),
	}

	if sandboxBackend == runworkload.BackendSubstrate {
		conn, apiStub, err := boomerutil.DialControl(*apiEndpoint)
		if err != nil {
			slog.Error("failed to dial ateapi", slog.String("err", err.Error()))
			os.Exit(1)
		}
		defer conn.Close()
		cfg.APIStub = apiStub
		cfg.RouterURL = *routerURL
		cfg.Atespace = *atespace
	} else {
		sandboxes, extensions, err := agentsandboxutil.Dial()
		if err != nil {
			slog.Error("failed to build agent-sandbox clients", slog.String("err", err.Error()))
			os.Exit(1)
		}
		runworkload.SetAgentSandboxDeps(runworkload.AgentSandboxDeps{
			Sandboxes:  sandboxes,
			Extensions: extensions,
			Namespace:  *namespace,
		})
	}

	if *workloadTimeout < 0 {
		slog.Error("fatal: --workload-timeout cannot be negative",
			slog.Duration("workload_timeout", *workloadTimeout))
		os.Exit(1)
	}
	if *workloadProvisionTimeout < 0 {
		slog.Error("fatal: --workload-provision-timeout cannot be negative",
			slog.Duration("workload_provision_timeout", *workloadProvisionTimeout))
		os.Exit(1)
	}
	// Unconditional, and inert unless the lookup below selects runworkload:
	// only that entry's Init reads these, so a run of another user class
	// carries them without ever looking at them.
	runworkload.SetOptions(runworkload.Options{
		Backend:          sandboxBackend,
		Workload:         *workloadName,
		Template:         *workloadTemplate,
		WarmPool:         *workloadWarmPool,
		Timeout:          *workloadTimeout,
		ProvisionTimeout: *workloadProvisionTimeout,
		MinWait:          *minWait,
		MaxWait:          *maxWait,
		ResumeMode:       *resumeMode,
	})

	entry, ok := userclass.Lookup(class)
	if !ok {
		slog.Error("fatal: unknown --user-class value",
			slog.String("user_class", *userClass),
			slog.String("known", strings.Join(userclass.Names(), ",")))
		os.Exit(1)
	}
	taskFn, shutdownFn := entry.Init(cfg)

	slog.Info("registered boomer task",
		slog.String("user_class", entry.UserClass),
		slog.String("user_class_flag", class),
		slog.String("backend", string(sandboxBackend)),
	)

	metricsCtx, metricsCancel := context.WithCancel(context.Background())
	defer metricsCancel()
	go func() {
		if err := bmetrics.Serve(metricsCtx, *promAddr); err != nil {
			slog.Error("prometheus server stopped", slog.String("err", err.Error()))
		}
	}()

	boomer.Events.Subscribe("boomer:stop", func() {
		slog.Info("boomer:stop event received; tearing down the run's sandboxes")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()
		shutdownFn(shutdownCtx)

		slog.Info("forcing container restart to ensure clean state")

		os.Exit(0)
	})

	// Blocks until SIGINT/SIGTERM or master quit. Boomer registers its own
	// signal handlers; we do cleanup after it returns.
	boomer.Run(&boomer.Task{
		Name:   entry.UserClass,
		Weight: 1,
		Fn:     taskFn,
	})

	slog.Info("boomer exited")
}
