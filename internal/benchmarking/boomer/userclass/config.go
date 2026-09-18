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

package userclass

import (
	"net/http"

	"github.com/ai-on-gke/agent-infra-benchmarking/internal/benchmarking/boomer/dynconfig"
	"go.opentelemetry.io/otel/trace"
	sandboxclientset "sigs.k8s.io/agent-sandbox/clients/k8s/clientset/versioned"
	extensionsclientset "sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned"
)

// Config holds the dependencies a user class needs. Built once at startup
// and passed to the entry's Init func.
type Config struct {
	// Sandboxes is the shared typed client for the core agents.x-k8s.io API
	// (Sandbox) — used to cold-create a sandbox directly from a
	// SandboxTemplate's blueprint, bypassing any warm pool.
	Sandboxes sandboxclientset.Interface
	// Extensions is the shared typed client for the extensions.agents.x-k8s.io
	// API (SandboxClaim, SandboxWarmPool, SandboxTemplate) — used to claim a
	// pre-warmed sandbox from a pool.
	Extensions extensionsclientset.Interface
	// HTTPClient is the shared HTTP client used to probe a ready sandbox's
	// own per-pod Service.
	HTTPClient *http.Client
	// Namespace every Sandbox/SandboxClaim this worker creates lives in.
	// Required; caller is responsible for it existing.
	Namespace string
	// Dyn is the runtime-mutable config (wait-time bounds, trace
	// probability). Required — every per-iteration read goes through it,
	// so tests can mutate it without touching workload internals.
	Dyn *dynconfig.Holder
	// Tracer anchors sampled spans; falls back to the otel global if nil.
	Tracer trace.Tracer
}
