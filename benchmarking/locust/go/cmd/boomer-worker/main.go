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
	"sync"
	"time"

	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/boomerutil"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/dynconfig"
	bmetrics "github.com/agent-substrate/substrate/internal/benchmarking/boomer/metrics"
	btrace "github.com/agent-substrate/substrate/internal/benchmarking/boomer/trace"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/userclass"
	"github.com/myzhan/boomer"

	// Imported for the user class each registers via init(). runworkload is
	// named rather than blank because its settings are declared here, as
	// flags, and handed over with runworkload.SetOptions.
	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/runworkload"
	_ "github.com/agent-substrate/substrate/internal/benchmarking/boomer/glutton"
)

const (
	// The stop handler blocks boomer's message loop, and boomer shuts itself
	// down after 60s without a master heartbeat.
	teardownTimeout = 45 * time.Second

	// Boomer sends stats to the master every 3s.
	statsReportWait = 4 * time.Second

	// One monitoring/prometheus.yaml scrape interval (10s) plus margin, so the
	// counters the teardown left are scraped before the pod goes.
	finalScrapeWait = 15 * time.Second
)

func main() {
	var (
		apiEndpoint = flag.String("api-endpoint", "k8s:///api.ate-system.svc.cluster.local:443", "ateapi gRPC dial target.")
		routerURL   = flag.String("router-url", "http://atenet-router.ate-system.svc.cluster.local", "atenet HTTP router base URL (no trailing slash).")
		atespace    = flag.String("atespace", "benchmark", "Atespace every actor this worker creates lives in. Ensured (CreateAtespace, AlreadyExists is ok) at startup.")
		promAddr    = flag.String("prometheus-addr", ":8001", "Address for the Prometheus /metrics endpoint.")
		userClass   = flag.String("user-class", "glutton", fmt.Sprintf("Locust user class to run, lowercase; one of %s.", strings.Join(userclass.Names(), "|")))

		// Settings a run holds from start to end. They were fetched from the
		// locust master's /boomer-config while the web UI could change them
		// mid-run; nothing in this repo does, so they are flags.
		minWait          = flag.Duration("min-wait", 0, "Lower bound on the think time between iterations.")
		maxWait          = flag.Duration("max-wait", 500*time.Millisecond, "Upper bound on the think time between iterations. At or below --min-wait waits exactly that.")
		resumeMode       = flag.String("resume-mode", runworkload.ResumeModeExplicit, fmt.Sprintf("How an actor is woken: %s issues ResumeActor before the instruction, %s leaves the wake to the router.", runworkload.ResumeModeExplicit, runworkload.ResumeModeImplicit))
		traceProbability = flag.Float64("trace-probability", 0, "Fraction of iterations to sample a trace for, 0 to 1.")

		httpMaxIdleConnsPerHost = flag.Int("http-max-idle-conns-per-host", 10000, "Idle HTTP connections the router client keeps per host. Set it to at least the number of users this worker runs, so each VU reuses its connection to the router across wakes instead of opening a new one per request.")

		// runworkload settings. Declared here with every other flag, so
		// --help is the whole command line rather than one list per user
		// class, and read only when --user-class selects runworkload. The
		// defaults are left empty so that the package resolves them: the
		// template's follows from the workload, which is the only place that
		// knows what it can address.
		workloadName = flag.String("workload-name", "",
			fmt.Sprintf("runworkload only: the workload a RunWorkloadUser drives. Unset selects %s.", runworkload.DefaultWorkload))
		workloadTemplate = flag.String("workload-template", "",
			"runworkload only: the ActorTemplate the workload is measured on. Unset selects the workload's own.")
		workloadTimeout = flag.Duration("workload-timeout", 0,
			fmt.Sprintf("runworkload only: deadline on each control-plane call and instruction; one that runs past it counts as a failure. Unset selects %s.", runworkload.DefaultTimeout))
		actorEgressAllow = flag.String("actor-egress-allow", "",
			"runworkload only: comma-separated hosts or IPv4 addresses every actor may connect to, resolved once at startup. Unset gives actors no egress policy, so they can reach nothing.")
	)
	// boomer.Run will call flag.Parse() if we haven't yet; calling here so
	// our flag-derived values are usable before that.
	flag.Parse()

	class := strings.ToLower(*userClass)

	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))

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

	conn, apiStub, err := boomerutil.DialControl(*apiEndpoint)
	if err != nil {
		slog.Error("failed to dial ateapi", slog.String("err", err.Error()))
		os.Exit(1)
	}
	defer conn.Close()

	// Upstream's tuning. Every VU calls the one router host, and the default two
	// idle connections per host would put a TCP connect in most ExecutionTime rows.
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConns = 0 // no total cap; the per-host cap governs
	transport.MaxIdleConnsPerHost = *httpMaxIdleConnsPerHost
	transport.IdleConnTimeout = 5 * time.Minute
	httpClient := &http.Client{Timeout: 5 * time.Minute, Transport: transport}

	cfg := &userclass.Config{
		APIStub:    apiStub,
		HTTPClient: httpClient,
		RouterURL:  *routerURL,
		Atespace:   *atespace,
		// runworkload reads the flags above through SetOptions; this is for
		// the upstream user classes, which take the same values off the
		// holder that userclass.Config requires.
		Dyn: dynconfig.NewHolder(dynconfig.Config{
			MinWait:          *minWait,
			MaxWait:          *maxWait,
			ResumeMode:       *resumeMode,
			TraceProbability: *traceProbability,
		}),
	}

	if *workloadTimeout < 0 {
		slog.Error("fatal: --workload-timeout cannot be negative",
			slog.Duration("workload_timeout", *workloadTimeout))
		os.Exit(1)
	}
	// Unconditional, and inert unless the lookup below selects runworkload:
	// only that entry's Init reads these, so a run of another user class
	// carries them without ever looking at them.
	runworkload.SetOptions(runworkload.Options{
		Workload:    *workloadName,
		Template:    *workloadTemplate,
		Timeout:     *workloadTimeout,
		MinWait:     *minWait,
		MaxWait:     *maxWait,
		ResumeMode:  *resumeMode,
		EgressAllow: splitList(*actorEgressAllow),
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
	)

	metricsCtx, metricsCancel := context.WithCancel(context.Background())
	defer metricsCancel()
	go func() {
		if err := bmetrics.Serve(metricsCtx, *promAddr); err != nil {
			slog.Error("prometheus server stopped", slog.String("err", err.Error()))
		}
	}()

	teardown := sync.OnceFunc(func() {
		ctx, cancel := context.WithTimeout(context.Background(), teardownTimeout)
		defer cancel()
		shutdownFn(ctx)
	})

	// The master sends this stop before it quits (master-image/tests/runworkload.py) and
	// waits for it. Boomer reports stats only until the handler returns, so it
	// holds on until the teardown's rows have gone out.
	boomer.Events.Subscribe("boomer:stop", func() {
		slog.Info("boomer:stop event received; suspending+deleting actors")
		teardown()
		time.Sleep(statsReportWait)
	})

	// Blocks until SIGINT/SIGTERM or master quit. Boomer registers its own
	// signal handlers; we do cleanup after it returns.
	boomer.Run(&boomer.Task{
		Name:   entry.UserClass,
		Weight: 1,
		Fn:     taskFn,
	})

	// A worker stopped by a signal rather than by the master got no stop.
	teardown()
	slog.Info("boomer exited; waiting for the final scrape", slog.Duration("wait", finalScrapeWait))
	time.Sleep(finalScrapeWait)
}

// splitList splits a comma-separated flag value, dropping empty entries.
func splitList(v string) []string {
	var out []string
	for _, item := range strings.Split(v, ",") {
		if item = strings.TrimSpace(item); item != "" {
			out = append(out, item)
		}
	}
	return out
}
