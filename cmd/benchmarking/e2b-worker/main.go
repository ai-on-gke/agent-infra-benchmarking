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

// e2b-worker is the E2B counterpart of boomer-worker: it drives the E2B
// sandbox lifecycle under load and reports through the boomer/locust worker
// protocol, so it appears to the Python locust master as a regular worker and
// its rows sit alongside the substrate rows for comparison. The lifecycle logic
// lives in internal/benchmarking/e2b/e2bworkload; this binary only parses flags,
// wires the clients, and hands boomer a task.
//
// Auth is read from the E2B_API_KEY environment variable; everything else is a
// flag with an E2B Cloud default.
package main

import (
	"context"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"time"

	bmetrics "github.com/agent-substrate/substrate/internal/benchmarking/boomer/metrics"
	btrace "github.com/agent-substrate/substrate/internal/benchmarking/boomer/trace"
	"github.com/myzhan/boomer"

	"github.com/agent-substrate/substrate/agent-infra-benchmarking/internal/benchmarking/e2b/control"
	"github.com/agent-substrate/substrate/agent-infra-benchmarking/internal/benchmarking/e2b/e2bworkload"
)

// teardownBudget caps the boomer:stop shutdown hook: the in-flight drain plus
// the kill of every sandbox the run created. Kept under the locust master's
// stop timeout so the master does not kill this process mid-teardown and leave
// sandboxes behind. Mirrors boomer-worker's budget.
const teardownBudget = 240 * time.Second

func main() {
	var (
		domain     = flag.String("domain", "e2b.app", "E2B base domain. API host is https://api.<domain>; sandboxes serve at <port>-<id>.<domain>. Use your self-hosted domain for a self-hosted E2B.")
		template   = flag.String("template", "base", "E2B template ID every sandbox is created from.")
		sandboxTTL = flag.Int("sandbox-timeout", 300, "Sandbox time-to-live in seconds, applied on create and resume.")
		command    = flag.String("command", "echo hello-from-e2b", "Instruction run inside each sandbox per cycle via /bin/sh -c; the measured Exec round-trip.")
		opTimeout  = flag.Duration("op-timeout", 120*time.Second, "Ceiling on any single control-plane or envd call.")
		promAddr   = flag.String("prometheus-addr", ":8001", "Address for the Prometheus /metrics endpoint.")

		minWait          = flag.Duration("min-wait", 0, "Lower bound on the think time between iterations.")
		maxWait          = flag.Duration("max-wait", 500*time.Millisecond, "Upper bound on the think time between iterations. At or below --min-wait waits exactly that.")
		traceProbability = flag.Float64("trace-probability", 0, "Fraction of iterations to sample a trace for, 0 to 1.")
	)
	// boomer.Run calls flag.Parse() if we have not; calling here so our
	// flag-derived values are usable before that, as boomer-worker does.
	flag.Parse()

	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))

	apiKey := os.Getenv("E2B_API_KEY")
	if apiKey == "" {
		slog.Error("fatal: E2B_API_KEY is required")
		os.Exit(1)
	}
	if *opTimeout <= 0 {
		slog.Error("fatal: --op-timeout must be positive", slog.Duration("op_timeout", *opTimeout))
		os.Exit(1)
	}

	ctx := context.Background()
	sampler := btrace.NewUpdatableSampler(*traceProbability)
	tp, err := btrace.Init(ctx, "e2b-boomer", sampler)
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
	ctrl := control.NewClient(*domain, apiKey, httpClient)

	rt := e2bworkload.New(
		e2bworkload.Config{
			Control:    ctrl,
			HTTPClient: httpClient,
			Domain:     *domain,
			Template:   *template,
			TTL:        *sandboxTTL,
		},
		e2bworkload.Options{
			MinWait:   *minWait,
			MaxWait:   *maxWait,
			OpTimeout: *opTimeout,
			Command:   *command,
		},
	)

	metricsCtx, metricsCancel := context.WithCancel(context.Background())
	defer metricsCancel()
	go func() {
		if err := bmetrics.Serve(metricsCtx, *promAddr); err != nil {
			slog.Error("prometheus server stopped", slog.String("err", err.Error()))
		}
	}()

	boomer.Events.Subscribe("boomer:stop", func() {
		slog.Info("boomer:stop event received; killing sandboxes")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), teardownBudget)
		defer cancel()
		rt.Shutdown(shutdownCtx)
		// os.Exit skips the deferred tracer shutdown, so flush spans here or
		// the last run's traces are lost.
		flushCtx, flushCancel := context.WithTimeout(context.Background(), 5*time.Second)
		_ = tp.Shutdown(flushCtx)
		flushCancel()
		os.Exit(0)
	})

	// Blocks until SIGINT/SIGTERM or master quit. Boomer registers its own
	// signal handlers; cleanup runs in the boomer:stop handler above.
	boomer.Run(&boomer.Task{
		Name:   e2bworkload.E2BUserClass,
		Weight: 1,
		Fn:     rt.Iterate,
	})

	slog.Info("boomer exited")
}
