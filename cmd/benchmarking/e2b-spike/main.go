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

// Command e2b-spike exercises the E2B client end-to-end against a live E2B
// endpoint: it creates a sandbox, runs one instruction through envd, pauses,
// resumes, runs the instruction again, and kills the sandbox, printing the
// latency of each lifecycle operation. It is the Phase-1 validation harness for
// the E2B integration — proving the control (REST) and data (envd ConnectRPC)
// clients speak the wire correctly — before any of this is wired into the
// boomer/Locust worker. It is deliberately standalone and single-shot; it is
// not the benchmark worker.
//
// Auth comes from the E2B_API_KEY environment variable. Everything else has a
// flag with an E2B Cloud default.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/agent-substrate/substrate/agent-infra-benchmarking/internal/benchmarking/e2b/control"
	"github.com/agent-substrate/substrate/agent-infra-benchmarking/internal/benchmarking/e2b/envd"
)

func main() {
	var (
		domain   = flag.String("domain", "e2b.app", "E2B base domain. API host is https://api.<domain>; sandboxes serve at <port>-<id>.<domain>. Use your self-hosted domain for a self-hosted E2B.")
		template = flag.String("template", "base", "E2B template ID to create the sandbox from.")
		timeout  = flag.Int("timeout", 300, "Sandbox time-to-live in seconds, applied on create and resume.")
		command  = flag.String("command", "echo hello-from-e2b", "Instruction run inside the sandbox via /bin/sh -c, used as the measured round-trip.")
		opBudget = flag.Duration("op-timeout", 120*time.Second, "Per-operation context timeout.")
	)
	flag.Parse()

	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, nil)))

	apiKey := os.Getenv("E2B_API_KEY")
	if apiKey == "" {
		slog.Error("E2B_API_KEY is required")
		os.Exit(1)
	}

	httpClient := &http.Client{Timeout: 5 * time.Minute}
	ctrl := control.NewClient(*domain, apiKey, httpClient)

	r := &run{
		ctrl:       ctrl,
		httpClient: httpClient,
		domain:     *domain,
		opBudget:   *opBudget,
		command:    *command,
	}

	if err := r.execute(context.Background(), *template, *timeout); err != nil {
		slog.Error("spike failed", slog.String("err", err.Error()))
		r.report()
		os.Exit(1)
	}
	r.report()
}

// run holds the dependencies and the collected per-operation timings for one
// pass through the sandbox lifecycle.
type run struct {
	ctrl       *control.Client
	httpClient *http.Client
	domain     string
	opBudget   time.Duration
	command    string

	results []result
}

type result struct {
	op   string
	dur  time.Duration
	note string
	err  error
}

// execute drives create -> exec -> pause -> resume -> exec -> kill. Lifecycle
// faults on create/exec/kill abort the run; pause/resume faults are reported
// but do not abort, since pause/resume may be unavailable on a given E2B
// deployment and the create/exec/kill timings are still worth having.
func (r *run) execute(ctx context.Context, template string, ttl int) error {
	var sb *control.Sandbox
	if err := r.timed(ctx, "CreateSandbox", func(opCtx context.Context) (string, error) {
		created, err := r.ctrl.Create(opCtx, &control.CreateRequest{TemplateID: template, Timeout: ttl})
		if err != nil {
			return "", err
		}
		sb = created
		return fmt.Sprintf("sandboxID=%s envd=%s", sb.SandboxID, sb.EnvdVersion), nil
	}); err != nil {
		return err
	}

	// killed records that the metered KillSandbox step already removed the
	// sandbox, so the best-effort defer below fires only on an abort path (or
	// when that metered kill itself failed) rather than killing twice.
	killed := false
	defer func() {
		if killed {
			return
		}
		// A fresh context so cleanup still runs even if ctx was cancelled.
		killCtx, cancel := context.WithTimeout(context.Background(), r.opBudget)
		defer cancel()
		_ = r.ctrl.Kill(killCtx, sb.SandboxID)
	}()

	// finalKill runs the metered KillSandbox step and marks success, so the
	// defer above does not repeat it.
	finalKill := func() error {
		err := r.killStep(ctx, sb.SandboxID)
		if err == nil {
			killed = true
		}
		return err
	}

	domain := r.domain
	if sb.Domain != nil && *sb.Domain != "" {
		domain = *sb.Domain
	}
	ev := envd.NewClient(sb.SandboxID, domain, sb.EnvdAccessToken, r.httpClient)

	if err := r.exec(ctx, "Exec", ev); err != nil {
		return err
	}

	if err := r.timed(ctx, "PauseSandbox", func(opCtx context.Context) (string, error) {
		return "", r.ctrl.Pause(opCtx, sb.SandboxID)
	}); err != nil {
		slog.Warn("pause failed; skipping resume (may be unavailable on this deployment)", slog.String("err", err.Error()))
		return finalKill()
	}

	if err := r.timed(ctx, "ResumeSandbox", func(opCtx context.Context) (string, error) {
		resumed, err := r.ctrl.Resume(opCtx, sb.SandboxID, ttl)
		if err != nil {
			return "", err
		}
		// Resume may hand back a fresh token/domain; rebuild the envd client
		// from it so the post-resume exec uses current connection details.
		if resumed != nil {
			d := domain
			if resumed.Domain != nil && *resumed.Domain != "" {
				d = *resumed.Domain
			}
			ev = envd.NewClient(resumed.SandboxID, d, resumed.EnvdAccessToken, r.httpClient)
			return "refreshed connection details", nil
		}
		return "reused pre-pause connection details", nil
	}); err != nil {
		slog.Warn("resume failed", slog.String("err", err.Error()))
		return finalKill()
	}

	if err := r.exec(ctx, "ExecAfterResume", ev); err != nil {
		return err
	}

	return finalKill()
}

// exec runs the configured instruction and records it under op. A non-zero exit
// is a failure: the sandbox answered but did not run the instruction cleanly.
func (r *run) exec(ctx context.Context, op string, ev *envd.Client) error {
	return r.timed(ctx, op, func(opCtx context.Context) (string, error) {
		res, err := ev.Exec(opCtx, r.command)
		if err != nil {
			return "", err
		}
		if res.ExitCode != 0 {
			return "", fmt.Errorf("exit code %d, stderr=%q", res.ExitCode, res.Stderr)
		}
		return fmt.Sprintf("stdout=%q", res.Stdout), nil
	})
}

func (r *run) killStep(ctx context.Context, sandboxID string) error {
	return r.timed(ctx, "KillSandbox", func(opCtx context.Context) (string, error) {
		return "", r.ctrl.Kill(opCtx, sandboxID)
	})
}

// timed runs fn under a per-operation context derived from ctx, records its
// duration, note, and error, and logs the outcome. The returned error is fn's,
// so callers decide whether it aborts the run.
func (r *run) timed(ctx context.Context, op string, fn func(context.Context) (string, error)) error {
	opCtx, cancel := context.WithTimeout(ctx, r.opBudget)
	defer cancel()

	start := time.Now()
	note, err := fn(opCtx)
	dur := time.Since(start)

	r.results = append(r.results, result{op: op, dur: dur, note: note, err: err})
	if err != nil {
		slog.Error("op failed", slog.String("op", op), slog.Duration("dur", dur), slog.String("err", err.Error()))
	} else {
		slog.Info("op ok", slog.String("op", op), slog.Duration("dur", dur), slog.String("note", note))
	}
	return err
}

// report prints the collected timings as a table on stdout.
func (r *run) report() {
	fmt.Println()
	fmt.Printf("%-18s %12s  %s\n", "OPERATION", "DURATION", "RESULT")
	fmt.Printf("%-18s %12s  %s\n", "---------", "--------", "------")
	for _, res := range r.results {
		status := res.note
		if res.err != nil {
			status = "ERROR: " + res.err.Error()
		}
		fmt.Printf("%-18s %12s  %s\n", res.op, res.dur.Round(time.Millisecond), status)
	}
}
