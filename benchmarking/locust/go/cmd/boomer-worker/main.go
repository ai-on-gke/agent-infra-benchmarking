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

// boomer-worker runs this repo's locust user classes, and upstream's, in Go.
// It speaks the locust worker protocol via myzhan/boomer, so it appears as a
// regular worker to the Python locust master while sidestepping gevent's
// scheduling tax.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/myzhan/boomer"
	"go.ate.dev/substrate/api/ateapi/v1"
	"go.ate.dev/substrate/internal/benchmarking/boomer/boomerutil"
	"go.ate.dev/substrate/internal/benchmarking/boomer/dynconfig"
	bmetrics "go.ate.dev/substrate/internal/benchmarking/boomer/metrics"
	btrace "go.ate.dev/substrate/internal/benchmarking/boomer/trace"
	"go.ate.dev/substrate/internal/benchmarking/boomer/userclass"

	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/density"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/runworkload"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/scenario"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/workload"

	// Imported for the user classes it registers via init().
	_ "go.ate.dev/substrate/internal/benchmarking/boomer/glutton"
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
		routerURL   = flag.String("router-url", boomerutil.DefaultRouterURL, "Router base URL (no trailing slash): atenet-router's, which takes mutual TLS, or agent-sandbox's.")
		atespace    = flag.String("atespace", "benchmark", "Atespace every actor this worker creates lives in. Ensured (CreateAtespace, AlreadyExists is ok) at startup.")
		promAddr    = flag.String("prometheus-addr", ":8001", "Address for the Prometheus /metrics endpoint.")
		userClass   = flag.String("user-class", "glutton", fmt.Sprintf("Locust user class to run, lowercase; one of %s.", strings.Join(userClasses(), "|")))

		// Settings a run holds from start to end. They were fetched from the
		// locust master's /boomer-config while the web UI could change them
		// mid-run; nothing in this repo does, so they are flags.
		minWait          = flag.Duration("min-wait", 0, "Lower bound on the think time between iterations.")
		maxWait          = flag.Duration("max-wait", 500*time.Millisecond, "Upper bound on the think time between iterations. At or below --min-wait waits exactly that.")
		resumeMode       = flag.String("resume-mode", scenario.ResumeModeExplicit, fmt.Sprintf("How an actor is woken: %s issues ResumeActor before the instruction, %s leaves the wake to the router.", scenario.ResumeModeExplicit, scenario.ResumeModeImplicit))
		suspendMode      = flag.String("suspend-mode", scenario.SuspendModeAsync, fmt.Sprintf("agent-substrate only: when SuspendActor returns: %s once the snapshot is on the node, uploading it in the background; %s once it is uploaded.", scenario.SuspendModeAsync, scenario.SuspendModeSync))
		traceProbability = flag.Float64("trace-probability", 0, "Fraction of iterations to sample a trace for, 0 to 1.")

		httpMaxIdleConnsPerHost = flag.Int("http-max-idle-conns-per-host", 10000, "Idle HTTP connections the router client keeps per host. Set it to at least the number of users this worker runs, so each VU reuses its connection to the router across wakes instead of opening a new one per request.")

		// Settings of every user class this repo builds on harness, which
		// scenario.Setup resolves; upstream's user classes read none of them.
		// Declared here with every other flag, so --help is the whole command
		// line rather than one list per user class. The defaults are left
		// empty so that scenario.Setup resolves them: the template's follows
		// from the workload, which is the only place that knows what it can
		// address.
		workloadName = flag.String("workload-name", "",
			fmt.Sprintf("The workload the actors run. Unset selects %s.", workload.Default))
		workloadTemplate = flag.String("workload-template", "",
			"The ActorTemplate the workload is measured on. Unset selects the workload's own.")
		workloadTimeout = flag.Duration("workload-timeout", 0,
			fmt.Sprintf("Deadline on each control-plane call and instruction; one that runs past it counts as a failure. Unset selects %s.", scenario.DefaultTimeout))
		actorEgressAllow = flag.String("actor-egress-allow", "",
			"agent-substrate only: comma-separated hostnames every actor may reach over cleartext HTTP or gRPC, on any port, each as the actor dials it. Unset, with --actor-egress-allow-tls unset too, gives actors no egress policy, so they can reach nothing.")
		actorEgressAllowTLS = flag.String("actor-egress-allow-tls", "",
			"agent-substrate only: comma-separated hostnames every actor may reach over TLS, such as an https:// endpoint, on any port, passed through undecrypted. A cleartext rule does not let TLS through.")
		apiConnections = flag.Int("api-connections", 1,
			"Connections to ateapi this worker spreads its users over.")
		provider     = flag.String("provider", "agent-substrate", "Provider: agent-substrate or agent-sandbox.")
		warmPoolName = flag.String("warm-pool-name", "",
			"agent-sandbox only: the SandboxWarmPools to claim from, comma-separated; the users are spread over them.")

		// runworkload's own settings, read only when --user-class selects it.
		instructionsPerCycle = flag.Int("instructions-per-cycle", 1,
			"runworkload only: instructions each cycle sends between its resume and its suspend; the first closes TTFI.")
		concurrentInstructions = flag.Int("concurrent-instructions", 1,
			"runworkload only: instructions sent to an actor at once at each of a cycle's steps.")
		gluttonWriteRAM = flag.String("glutton-write-ram", "",
			"runworkload with glutton only: memory each cycle rewrites in the actor before its suspend, such as 64Mi. Unset writes none.")
		gluttonWriteDisk = flag.Int("glutton-write-disk", 0,
			"runworkload with glutton only: bytes of a file each cycle rewrites in the actor before its suspend. Zero writes none.")
		suspendDisabled = flag.Bool("suspend-disabled", false,
			"runworkload only: whether to skip the suspend phase during the benchmark.")

		// density runs runworkload's cycle, so it reads the flags above too.
		activeActors = flag.Int("active-actors", 0,
			"density only: how many of this worker's actors are in a cycle at once, at least 1. Each actor waits after a cycle so that it spends this over the worker's users of its time in one, in place of --min-wait and --max-wait, which then only follow a failed start.")
		skippedResumeSuspend = flag.Duration("skipped-resume-suspend", 0,
			"density with --suspend-disabled only: the resume and suspend each cycle skips, which the pacing counts as if it ran, so the actors send instructions at the rate of ones that suspend.")
	)
	// boomer.Run will call flag.Parse() if we haven't yet; calling here so
	// our flag-derived values are usable before that.
	flag.Parse()

	class := strings.ToLower(*userClass)

	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))

	switch *resumeMode {
	case scenario.ResumeModeExplicit, scenario.ResumeModeImplicit:
	default:
		slog.Error("fatal: unknown --resume-mode value",
			slog.String("resume_mode", *resumeMode),
			slog.String("known", scenario.ResumeModeExplicit+","+scenario.ResumeModeImplicit))
		os.Exit(1)
	}
	switch *suspendMode {
	case scenario.SuspendModeAsync, scenario.SuspendModeSync:
	default:
		slog.Error("fatal: unknown --suspend-mode value",
			slog.String("suspend_mode", *suspendMode),
			slog.String("known", scenario.SuspendModeAsync+","+scenario.SuspendModeSync))
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

	if *apiConnections < 1 || *instructionsPerCycle < 1 || *concurrentInstructions < 1 || *gluttonWriteDisk < 0 {
		slog.Error("fatal: --api-connections, --instructions-per-cycle and --concurrent-instructions must be at least 1," +
			" and --glutton-write-disk not negative")
		os.Exit(1)
	}
	// Each its own HTTP/2 connection, so the users' calls are not all
	// multiplexed over one. agent-sandbox calls no ateapi.
	var apiStubs []ateapi.ControlClient
	if *provider == "agent-substrate" {
		apiStubs = make([]ateapi.ControlClient, *apiConnections)
		for i := range apiStubs {
			conn, stub, err := boomerutil.DialControl(*apiEndpoint)
			if err != nil {
				slog.Error("failed to dial ateapi", slog.String("err", err.Error()))
				os.Exit(1)
			}
			defer conn.Close()
			apiStubs[i] = stub
		}
	}
	var apiStub ateapi.ControlClient
	if len(apiStubs) > 0 {
		apiStub = apiStubs[0]
	}

	// Upstream's tuning. Every VU calls the one router host, and the default two
	// idle connections per host would put a TCP connect in most ExecutionTime rows.
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if *provider == "agent-substrate" {
		routerTLS, err := boomerutil.RouterTLSConfig()
		if err != nil {
			slog.Error("failed to configure router TLS", slog.String("err", err.Error()))
			os.Exit(1)
		}
		transport.TLSClientConfig = routerTLS
		// One HTTP/1.1 connection per VU, as over cleartext, rather than all
		// of them multiplexed on one HTTP/2 connection.
		transport.ForceAttemptHTTP2 = false
	}
	transport.MaxIdleConns = 0 // no total cap; the per-host cap governs
	transport.MaxIdleConnsPerHost = *httpMaxIdleConnsPerHost
	transport.IdleConnTimeout = 5 * time.Minute
	httpClient := &http.Client{Timeout: 5 * time.Minute, Transport: transport}

	cfg := &userclass.Config{
		APIStub:    apiStub,
		HTTPClient: httpClient,
		RouterURL:  *routerURL,
		Atespace:   *atespace,
		// runworkload takes the flags above through runworkload.New, so its
		// holder carries nothing; an upstream user class gets its own below.
		Dyn: dynconfig.NewHolder(nil),
	}

	if *workloadTimeout < 0 {
		slog.Error("fatal: --workload-timeout cannot be negative",
			slog.Duration("workload_timeout", *workloadTimeout))
		os.Exit(1)
	}
	// runworkload and density are built here, from the flags, rather than
	// looked up: the registry hands an Init nothing but cfg.
	cycle := func() runworkload.Config {
		return runworkload.Config{
			Common: scenario.Setup(scenario.Options{
				Config:         cfg,
				Provider:       *provider,
				WarmPoolNames:  splitList(*warmPoolName),
				APIStubs:       apiStubs,
				WorkloadName:   *workloadName,
				Template:       *workloadTemplate,
				Timeout:        *workloadTimeout,
				MinWait:        *minWait,
				MaxWait:        *maxWait,
				ResumeMode:     *resumeMode,
				SuspendMode:    *suspendMode,
				EgressAllow:    splitList(*actorEgressAllow),
				EgressAllowTLS: splitList(*actorEgressAllowTLS),
			}),
			SuspendDisabled:        *suspendDisabled,
			InstructionsPerCycle:   *instructionsPerCycle,
			ConcurrentInstructions: *concurrentInstructions,
			GluttonWriteRAM:        *gluttonWriteRAM,
			GluttonWriteDisk:       *gluttonWriteDisk,
		}
	}
	var (
		taskFn     func()
		shutdownFn func(context.Context)
		taskName   string
	)
	switch class {
	case runworkload.Name:
		taskFn, shutdownFn = runworkload.New(cycle())
		taskName = runworkload.UserClass
	case density.Name:
		taskFn, shutdownFn = density.New(density.Config{
			Cycle:                cycle(),
			ActiveActors:         *activeActors,
			SkippedResumeSuspend: *skippedResumeSuspend,
		})
		taskName = density.UserClass
	default:
		entry, ok := userclass.Lookup(class)
		if !ok {
			slog.Error("fatal: unknown --user-class value",
				slog.String("user_class", *userClass),
				slog.String("known", strings.Join(userClasses(), ",")))
			os.Exit(1)
		}
		// The upstream user classes take the same values off their holder, as
		// the keys their codec reads; a key a class does not have is ignored.
		cfg.Dyn = dynconfig.NewHolder(entry.Config)
		payload, err := json.Marshal(map[string]any{
			"min_wait_time":     dynconfig.Seconds(*minWait),
			"max_wait_time":     dynconfig.Seconds(*maxWait),
			"resume_mode":       *resumeMode,
			"lifecycle_mode":    *suspendMode,
			"trace_probability": *traceProbability,
		})
		if err == nil {
			_, _, err = cfg.Dyn.Apply(payload)
		}
		if err != nil {
			slog.Error("fatal: the user class refused its settings",
				slog.String("user_class", class), slog.String("err", err.Error()))
			os.Exit(1)
		}
		taskFn, shutdownFn = entry.Init(cfg)
		taskName = entry.UserClass
	}

	slog.Info("registered boomer task",
		slog.String("user_class", taskName),
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

	// The master sends this stop before it quits (master-image/tests/_master.py) and
	// waits for it. Boomer reports stats only until the handler returns, so it
	// holds on until the teardown's rows have gone out.
	boomer.Events.Subscribe("boomer:stop", func() {
		slog.Info("boomer:stop event received; deleting actors")
		teardown()
		time.Sleep(statsReportWait)
	})

	// Blocks until SIGINT/SIGTERM or master quit. Boomer registers its own
	// signal handlers; we do cleanup after it returns.
	boomer.Run(&boomer.Task{
		Name:   taskName,
		Weight: 1,
		Fn:     taskFn,
	})

	// A worker stopped by a signal rather than by the master got no stop.
	teardown()
	slog.Info("boomer exited; waiting for the final scrape", slog.Duration("wait", finalScrapeWait))
	time.Sleep(finalScrapeWait)
}

// userClasses are the --user-class values: the upstream ones registered with
// userclass, runworkload and density.
func userClasses() []string {
	names := append(userclass.Names(), runworkload.Name, density.Name)
	slices.Sort(names)
	return names
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
