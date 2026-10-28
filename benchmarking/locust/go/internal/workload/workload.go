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

// Package workload holds the actor-side applications a run can drive, the
// registry --workload-name selects one from, and the HTTP path to an actor
// they share.
package workload

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"maps"
	"net/http"
	"slices"
	"strings"

	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
)

// Workload is the actor-side application a measurement window drives. It owns
// the two things that differ per workload — which ActorTemplate deploys it,
// and what counts as an instruction it has demonstrably run — and nothing
// else. The windows, the retry loop, the spans and the rows belong to the
// benchmark and are identical whichever workload is measured, which is what
// keeps a measurement from one workload comparable to one from another.
//
// glutton and openclaw are the implementations today. Another one is a file
// that implements this interface and calls register in its init.
type Workload interface {
	// Name is the --workload-name value that selects this workload.
	Name() string

	// DefaultTemplate is the ActorTemplate measured when
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
	SendInstruction(ctx context.Context, actor sandbox.Actor) (retry bool, err error)
}

// Loader is a workload whose actors can be made to change their memory and
// disk on request, which a cycle can do before its suspend so the snapshot
// holds that much changed state.
type Loader interface {
	// WriteRAM rewrites size (a quantity such as "64Mi") of the actor's
	// memory, first on an actor not yet written to, and WriteDisk size bytes
	// of a file on its disk.
	WriteRAM(ctx context.Context, actor sandbox.Actor, size string, first bool) error
	WriteDisk(ctx context.Context, actor sandbox.Actor, size int) error
}

// Default is the workload Resolve selects when no name is given.
const Default = GluttonName

var workloads = map[string]Workload{}

// register adds w to the set --workload-name can select. It
// panics on a duplicate name, which can only be a build-time mistake,
// mirroring how upstream's userclass registry treats the same error.
func register(w Workload) {
	if _, exists := workloads[w.Name()]; exists {
		panic(fmt.Sprintf("runworkload: duplicate workload registration for %q", w.Name()))
	}
	workloads[w.Name()] = w
}

func lookup(name string) (Workload, bool) {
	w, ok := workloads[name]
	return w, ok
}

// registeredNames returns the registered names, sorted so an error message
// listing them reads the same on every run.
func registeredNames() []string {
	names := make([]string, 0, len(workloads))
	for name := range workloads {
		names = append(names, name)
	}
	slices.Sort(names)
	return names
}

// Resolve picks the workload a run drives, Default when name is empty. An
// unknown name is an error rather than a fallback to Default: the operator asked for a
// particular workload, and silently measuring a different one produces a row
// that reads as valid while answering the wrong question.
func Resolve(name string) (Workload, error) {
	if name == "" {
		name = Default
	}
	w, ok := lookup(name)
	if !ok {
		return nil, fmt.Errorf("unknown --workload-name %q; known: %s", name, strings.Join(registeredNames(), ", "))
	}
	return w, nil
}

// ErrStaleAssignment marks a 421 from the actor's worker: the router sent the
// request to a worker the actor had already left. The next request is routed
// afresh, so unlike other failures it is worth sending again at once.
var ErrStaleAssignment = errors.New("stale worker assignment")

// httpInstruction is one request a workload sends through the router, together
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

// sendHTTPInstruction performs inst against actor through the router. The actor
// addressing, the trace propagation and the retry classification live here
// rather than in each workload because they describe substrate's ingress path,
// not any one application: a workload that got them wrong would change what
// the benchmark measures instead of what it drives.
func sendHTTPInstruction(ctx context.Context, actor sandbox.Actor, inst httpInstruction) (retry bool, err error) {
	httpReq, err := http.NewRequestWithContext(ctx, inst.Method, actor.RouterURL+inst.Path, bytes.NewReader(inst.Body))
	if err != nil {
		return false, err
	}
	maps.Copy(httpReq.Header, actor.Header)
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
	// A 5xx or a 429 means the actor is still activating, and a 421 that the
	// request reached a worker the actor has left. Any other 4xx is a
	// misconfigured request that waiting will not fix.
	if resp.StatusCode >= 400 {
		httpErr := fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(respBody)))
		if resp.StatusCode == http.StatusMisdirectedRequest {
			return true, fmt.Errorf("%w: %w", ErrStaleAssignment, httpErr)
		}
		return resp.StatusCode >= 500 || resp.StatusCode == http.StatusTooManyRequests, httpErr
	}

	if inst.Verify == nil {
		return false, nil
	}
	return false, inst.Verify(respBody)
}
