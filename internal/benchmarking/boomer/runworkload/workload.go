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

package runworkload

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"slices"
	"strings"

	"github.com/agent-substrate/substrate/internal/atenet"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
)

// workload is the actor-side application a measurement window drives. It owns
// the things that differ per workload — which backend it targets, which
// ActorTemplate/SandboxTemplate deploys it, and what counts as an
// instruction it has demonstrably run — and nothing else. The windows, the
// retry loop, the spans and the rows belong to the benchmark and are
// identical whichever workload is measured, which is what keeps a
// measurement from one workload comparable to one from another (within the
// same backend — see the README for why substrate and agent-sandbox numbers
// don't compare directly).
//
// glutton and openclaw target substrate; ping targets agent-sandbox. Another
// one is a file that implements this interface and calls registerWorkload in
// its init.
type workload interface {
	// Name is the --workload-name value that selects this workload.
	Name() string

	// Backend is the sandboxBackend this workload's target application
	// speaks to. resolveWorkload rejects a name/--backend combination that
	// disagrees with this rather than silently measuring the wrong thing.
	Backend() Backend

	// DefaultTemplate is the ActorTemplate/SandboxTemplate measured when
	// --workload-template says nothing. It has to deploy something this
	// workload can address.
	DefaultTemplate() string

	// SendInstruction issues exactly one instruction to actor. A nil error
	// closes the measurement window, so an implementation must not return one
	// until the sandbox has actually run the instruction — accepting the
	// connection is not the same thing.
	//
	// retry reports whether a real client would try again: true while the
	// actor may still be activating, false for a fault that waiting cannot
	// fix. It is consulted only when err is non-nil.
	SendInstruction(ctx context.Context, actor actorRef) (retry bool, err error)
}

// actorRef is the handle a workload needs to reach one sandbox, generalized
// across backends. Exactly one addressing scheme is populated depending on
// which backend built it:
//   - substrate addresses a shared atenet router by an atespace/name header
//     (RouterURL, Atespace, Name).
//   - agent-sandbox addresses a sandbox's own per-pod Service directly, no
//     router or header involved (ServiceFQDN, Port).
type actorRef struct {
	HTTPClient *http.Client

	// substrate addressing.
	RouterURL string
	Atespace  string
	Name      string

	// agent-sandbox addressing.
	ServiceFQDN string
	Port        int32
}

// target is the value atenet resolves the actor from. substrate only.
func (a actorRef) target() string {
	return a.Atespace + "/" + a.Name
}

// baseURL is the request base sendHTTPInstruction issues against: the
// atenet router for substrate, or the sandbox's own Service for
// agent-sandbox.
func (a actorRef) baseURL() string {
	if a.ServiceFQDN != "" {
		return fmt.Sprintf("http://%s:%d", a.ServiceFQDN, a.Port)
	}
	return a.RouterURL
}

var workloads = map[string]workload{}

// registerWorkload adds w to the set --workload-name can select. It
// panics on a duplicate name, which can only be a build-time mistake,
// mirroring how upstream's userclass registry treats the same error.
func registerWorkload(w workload) {
	if _, exists := workloads[w.Name()]; exists {
		panic(fmt.Sprintf("runworkload: duplicate workload registration for %q", w.Name()))
	}
	workloads[w.Name()] = w
}

func lookupWorkload(name string) (workload, bool) {
	w, ok := workloads[name]
	return w, ok
}

// workloadNames returns the registered names, sorted so an error message
// listing them reads the same on every run.
func workloadNames() []string {
	names := make([]string, 0, len(workloads))
	for name := range workloads {
		names = append(names, name)
	}
	slices.Sort(names)
	return names
}

// workloadNamesFor returns the registered names that target backend, sorted.
func workloadNamesFor(backend Backend) []string {
	var names []string
	for name, w := range workloads {
		if w.Backend() == backend {
			names = append(names, name)
		}
	}
	slices.Sort(names)
	return names
}

// defaultWorkloadFor is each backend's own default when --workload-name says
// nothing: glutton for substrate (this benchmark's original default), ping
// for agent-sandbox.
func defaultWorkloadFor(backend Backend) string {
	if backend == BackendAgentSandbox {
		return pingWorkloadName
	}
	return gluttonWorkloadName
}

// resolveWorkload picks the workload a run drives. An unknown name, or one
// that targets a different backend than the one selected, is an error rather
// than a silent fallback: the operator asked for a particular workload, and
// measuring a different one (or sending its instructions at a sandbox
// nothing deployed) produces a row that reads as valid while answering the
// wrong question.
func resolveWorkload(name string, backend Backend) (workload, error) {
	if name == "" {
		name = defaultWorkloadFor(backend)
	}
	w, ok := lookupWorkload(name)
	if !ok {
		return nil, fmt.Errorf("unknown --workload-name %q for --backend %s; known: %s",
			name, backend, strings.Join(workloadNamesFor(backend), ", "))
	}
	if w.Backend() != backend {
		return nil, fmt.Errorf("--workload-name %q targets backend %q, not --backend %q; pick one of %s or switch --backend",
			name, w.Backend(), backend, strings.Join(workloadNamesFor(backend), ", "))
	}
	return w, nil
}

// httpInstruction is one request a workload sends to the sandbox, together
// with the check that decides whether the answer proves the sandbox ran it.
type httpInstruction struct {
	Method      string
	Path        string
	ContentType string
	Body        []byte

	// Verify inspects the body of a non-error response. The error it returns
	// fails the window without a retry: the actor answered, so waiting longer
	// cannot change the answer. Nil means any non-error response counts.
	Verify func(body []byte) error
}

// sendHTTPInstruction performs inst against actor. The addressing, the trace
// propagation and the retry classification live here rather than in each
// workload because they describe the ingress path, not any one application:
// a workload that got them wrong would change what the benchmark measures
// instead of what it drives.
func sendHTTPInstruction(ctx context.Context, actor actorRef, inst httpInstruction) (retry bool, err error) {
	httpReq, err := http.NewRequestWithContext(ctx, inst.Method, actor.baseURL()+inst.Path, bytes.NewReader(inst.Body))
	if err != nil {
		return false, err
	}
	if actor.ServiceFQDN == "" {
		// substrate: the router resolves the actor from this header alone —
		// atenet's ingress handler parses it and 404s with "invalid actor
		// reference" when it is absent, regardless of the request's Host.
		// agent-sandbox needs no such header: actor.baseURL() already points
		// straight at the target sandbox's own Service.
		httpReq.Header.Set(atenet.TargetActorHeader, actor.target())
	}
	if inst.ContentType != "" {
		httpReq.Header.Set("Content-Type", inst.ContentType)
	}
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(httpReq.Header))

	resp, err := actor.HTTPClient.Do(httpReq)
	if err != nil {
		return true, err
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return true, err
	}
	// A 5xx or a 429 means the actor is still activating, while a 4xx is a
	// misrouted or misconfigured request that waiting will not fix.
	if resp.StatusCode >= 400 {
		httpErr := fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(respBody)))
		return resp.StatusCode >= 500 || resp.StatusCode == http.StatusTooManyRequests, httpErr
	}

	if inst.Verify == nil {
		return false, nil
	}
	return false, inst.Verify(respBody)
}
