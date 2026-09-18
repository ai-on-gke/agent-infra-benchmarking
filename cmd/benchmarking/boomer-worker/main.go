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

// boomer-worker is the Go re-implementation of the RunWorkloadUser locust
// test. It speaks the locust worker protocol via myzhan/boomer, so it
// appears as a regular worker to the Python locust master while sidestepping
// gevent's scheduling tax.
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

	"github.com/myzhan/boomer"

	"github.com/ai-on-gke/agent-infra-benchmarking/internal/benchmarking/boomer/boomerutil"
	"github.com/ai-on-gke/agent-infra-benchmarking/internal/benchmarking/boomer/dynconfig"
	bmetrics "github.com/ai-on-gke/agent-infra-benchmarking/internal/benchmarking/boomer/metrics"
	btrace "github.com/ai-on-gke/agent-infra-benchmarking/internal/benchmarking/boomer/trace"
	"github.com/ai-on-gke/agent-infra-benchmarking/internal/benchmarking/boomer/userclass"

	// Imported for the user class it registers via init(). Named rather than
	// blank because its settings are declared here, as flags, and handed
	// over with runworkload.SetOptions.
	"github.com/ai-on-gke/agent-infra-benchmarking/internal/benchmarking/boomer/runworkload"
)

func main() {
	var (
		namespace = flag.String("namespace", "benchmarking",
			"Namespace every Sandbox/SandboxClaim this worker creates lives in.")
		promAddr           = flag.String("prometheus-addr", ":8001", "Address for the Prometheus /metrics endpoint.")
		configJSON         = flag.String("config-json", "", "Initial dynconfig as a JSON object (keys: trace_probability, min_wait_time, max_wait_time in seconds). Unset fields keep their built-in defaults.")
		masterWebPort      = flag.Int("master-web-port", 0, "If non-zero, fetch dynconfig from http://{master-host}:{master-web-port}/boomer-config on each spawn message and fail fatally on error. {master-host} comes from boomer's existing --master-host flag.")
		configPollInterval = flag.Duration("config-poll-interval", 10*time.Second, "With --master-web-port, also fetch dynconfig on this interval. A spawn message comes only when the number of users or the spawn rate changes, thus a load shape that changes the sample rate alone needs this. Zero stops the polling.")
		userClass          = flag.String("user-class", "runworkload", fmt.Sprintf("Locust user class to run, lowercase; one of %s.", strings.Join(userclass.Names(), "|")))

		// runworkload settings. Declared here with every other flag, so
		// --help is the whole command line rather than one list per user
		// class.
		workloadName = flag.String("workload-name", "",
			fmt.Sprintf("runworkload only: the workload a RunWorkloadUser drives. Unset selects %s.", runworkload.DefaultWorkload))
		workloadTemplate = flag.String("workload-template", "",
			"runworkload only: the SandboxTemplate the workload is measured on. Unset selects the workload's own.")
		workloadWarmPool = flag.String("workload-warmpool", "",
			"runworkload only: the SandboxWarmPool the pool-claim window adopts from. Unset selects \"<template>-warmpool\".")
		workloadTimeout = flag.Duration("workload-timeout", 0,
			fmt.Sprintf("runworkload only: ceiling on the instruction retry loop. Unset selects %s.", runworkload.DefaultTimeout))
		workloadProvisionTimeout = flag.Duration("workload-provision-timeout", 0,
			fmt.Sprintf("runworkload only: ceiling on waiting for a Sandbox/SandboxClaim's Ready condition. Unset selects %s.", runworkload.DefaultProvisionTimeout))
	)
	// boomer.Run will call flag.Parse() if we haven't yet; calling here so
	// our flag-derived values are usable before that.
	flag.Parse()

	class := strings.ToLower(*userClass)

	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))

	initialCfg, err := dynconfig.Parse([]byte(*configJSON), dynconfig.Config{MaxWait: 500 * time.Millisecond})
	if err != nil {
		slog.Error("failed to parse --config-json", slog.String("err", err.Error()))
		os.Exit(1)
	}

	ctx := context.Background()
	sampler := btrace.NewUpdatableSampler(initialCfg.TraceProbability)
	tp, err := btrace.Init(ctx, "agent-sandbox-boomer", sampler)
	if err != nil {
		slog.Error("failed to initialize tracing", slog.String("err", err.Error()))
		os.Exit(1)
	}
	defer func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = tp.Shutdown(shutdownCtx)
	}()

	sandboxes, extensions, err := boomerutil.DialAgentSandbox()
	if err != nil {
		slog.Error("failed to build agent-sandbox clients", slog.String("err", err.Error()))
		os.Exit(1)
	}

	httpClient := &http.Client{Timeout: 30 * time.Second}

	dyn := dynconfig.NewHolder(initialCfg)

	if *masterWebPort > 0 {
		masterHost := flag.Lookup("master-host").Value.String()
		configURL := fmt.Sprintf("http://%s:%d/boomer-config", masterHost, *masterWebPort)
		if err := dynconfig.SubscribeSpawn(configURL, dyn, sampler, 5*time.Second, func(err error) {
			slog.Error("fatal: dynconfig fetch failed; exiting so the pod restarts",
				slog.String("url", configURL), slog.String("err", err.Error()))
			os.Exit(1)
		}); err != nil {
			slog.Error("fatal: failed to subscribe to boomer:spawn",
				slog.String("err", err.Error()))
			os.Exit(1)
		}
		// A spawn message is not sufficient by itself: locust sends one only
		// when the number of users or the spawn rate changes, thus a step of
		// a load shape that changes the sample rate alone needs this. A
		// failed poll is not fatal, because the worker holds the last good
		// value.
		dynconfig.StartPoll(ctx, configURL, dyn, sampler,
			*configPollInterval, 5*time.Second, func(err error) {
				slog.Warn("dynconfig poll failed; keeping the current values",
					slog.String("url", configURL), slog.String("err", err.Error()))
			})
		slog.Info("dynconfig fetch enabled",
			slog.String("url", configURL),
			slog.Duration("poll_interval", *configPollInterval))
	}

	cfg := &userclass.Config{
		Sandboxes:  sandboxes,
		Extensions: extensions,
		HTTPClient: httpClient,
		Namespace:  *namespace,
		Dyn:        dyn,
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
		Workload:         *workloadName,
		Template:         *workloadTemplate,
		WarmPool:         *workloadWarmPool,
		Timeout:          *workloadTimeout,
		ProvisionTimeout: *workloadProvisionTimeout,
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

	// Blocks until SIGINT/SIGTERM or master quit. Boomer registers its own
	// signal handlers; we do cleanup after it returns.
	boomer.Run(&boomer.Task{
		Name:   entry.UserClass,
		Weight: 1,
		Fn:     taskFn,
	})

	slog.Info("boomer exited; tearing down sandboxes")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	shutdownFn(shutdownCtx)
}
