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

// Package sandbox puts each infrastructure provider's sandbox lifecycle behind
// one interface: a VU creates its sandbox once, wakes it for every cycle, puts
// it to sleep between cycles and removes it at the end, without knowing which
// provider it runs on. Every call reports its own row and span through measure.
package sandbox

import (
	"context"
	"net/http"

	"github.com/prometheus/client_golang/prometheus"
)

// The rows Wake and Sleep report. SlotWait is a Wake's wait for a free worker,
// for a provider that waits for one.
const (
	ResumeMetric   = "Resume"
	SuspendMetric  = "Suspend"
	SlotWaitMetric = "SlotWait"
)

// Provider creates the sandboxes of a run on one infrastructure provider.
type Provider interface {
	// Create makes one sandbox, asleep: the first Wake brings it up.
	Create(ctx context.Context) (Sandbox, error)
}

// Sandbox is one VU's sandbox. Its calls are made one at a time.
type Sandbox interface {
	// Wake brings the sandbox up to serve instructions.
	Wake(ctx context.Context) error

	// Sleep takes it down again. A failed one can leave it mid transition,
	// and nothing reconciles it, so the caller decides what happens next.
	Sleep(ctx context.Context) error

	// Settle, after a failed call, waits for the sandbox to come out of the
	// transition the call left it in and leaves it asleep for the next Wake.
	// An error means it cannot be cycled again and is to be removed.
	Settle(ctx context.Context) error

	// Remove gets rid of the sandbox whatever state it is in. A nil return
	// means it is gone.
	Remove(ctx context.Context) error

	// Actor is the handle a workload addresses the sandbox by. Built per call,
	// since a Wake can move the sandbox.
	Actor() Actor
}

// Actor is the handle a workload needs to reach one sandbox. It carries what
// the provider requires to route a request and nothing about the measurement,
// so a workload cannot accidentally depend on benchmark state.
type Actor struct {
	HTTPClient *http.Client
	RouterURL  string
	Name       string

	// Header is what routes a request to the sandbox; every request to it
	// carries these headers.
	Header http.Header
}

var (
	activeActors = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "boomer_active_actors",
			Help: "Number of currently active actors.",
		},
		[]string{"workload"},
	)
	suspendedActors = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "boomer_suspended_actors",
			Help: "Number of currently suspended actors.",
		},
		[]string{"workload"},
	)
)

func init() {
	prometheus.MustRegister(activeActors, suspendedActors)
}
