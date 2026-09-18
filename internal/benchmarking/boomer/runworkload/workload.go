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

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
)

// workload is the actor-side application a measurement window drives. It owns
// the two things that differ per workload — which SandboxTemplate deploys it,
// and what counts as an instruction it has demonstrably run — and nothing
// else. The windows, the retry loop, the spans and the rows belong to the
// benchmark and are identical whichever workload is measured, which is what
// keeps a measurement from one workload comparable to one from another.
//
// ping is the only implementation today. Another one is a file that
// implements this interface and calls registerWorkload in its init.
type workload interface {
	// Name is the --workload-name value that selects this workload.
	Name() string

	// DefaultTemplate is the SandboxTemplate measured when
	// --workload-template says nothing. It has to deploy something this
	// workload can address, and a same-named SandboxWarmPool must exist
	// unless --workload-warmpool says otherwise.
	DefaultTemplate() string

	// SendInstruction issues exactly one instruction to the sandbox at
	// target. A nil error closes the measurement window, so an
	// implementation must not return one until the sandbox has actually run
	// the instruction — accepting the connection is not the same thing.
	//
	// retry reports whether a real client would try again: true while the
	// sandbox's application may still be starting up, false for a fault that
	// waiting cannot fix. It is consulted only when err is non-nil.
	SendInstruction(ctx context.Context, target sandboxTarget) (retry bool, err error)
}

// sandboxTarget is the handle a workload needs to reach one ready sandbox.
// Unlike substrate's atenet router (addressed by an atespace/name header
// through one shared endpoint), every agent-sandbox Sandbox gets its own
// per-pod Service and DNS name, so a target is just that name and the port
// the workload's container listens on.
type sandboxTarget struct {
	HTTPClient  *http.Client
	ServiceFQDN string
	Port        int32
}

func (t sandboxTarget) baseURL() string {
	return fmt.Sprintf("http://%s:%d", t.ServiceFQDN, t.Port)
}

var workloads = map[string]workload{}

// registerWorkload adds w to the set --workload-name can select. It
// panics on a duplicate name, which can only be a build-time mistake.
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

// resolveWorkload picks the workload a run drives. An unknown name is an error
// rather than a fallback to DefaultWorkload: the operator asked for a
// particular workload, and silently measuring a different one produces a row
// that reads as valid while answering the wrong question.
func resolveWorkload(name string) (workload, error) {
	if name == "" {
		name = DefaultWorkload
	}
	w, ok := lookupWorkload(name)
	if !ok {
		return nil, fmt.Errorf("unknown --workload-name %q; known: %s", name, strings.Join(workloadNames(), ", "))
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
	// fails the window without a retry: the sandbox answered, so waiting
	// longer cannot change the answer. Nil means any non-error response
	// counts.
	Verify func(body []byte) error
}

// sendHTTPInstruction performs inst against the sandbox at target. The retry
// classification lives here rather than in each workload because it
// describes how every client on this path should behave, not any one
// application: a workload that got it wrong would change what the benchmark
// measures instead of what it drives.
func sendHTTPInstruction(ctx context.Context, target sandboxTarget, inst httpInstruction) (retry bool, err error) {
	httpReq, err := http.NewRequestWithContext(ctx, inst.Method, target.baseURL()+inst.Path, bytes.NewReader(inst.Body))
	if err != nil {
		return false, err
	}
	if inst.ContentType != "" {
		httpReq.Header.Set("Content-Type", inst.ContentType)
	}
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(httpReq.Header))

	resp, err := target.HTTPClient.Do(httpReq)
	if err != nil {
		// Includes DNS resolution failure, which is expected for the first
		// instant after Ready flips True while kube-dns/coredns catches up —
		// retryable rather than fatal.
		return true, err
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return true, err
	}
	// A 5xx or a 429 means the sandbox's application is still starting, while
	// a 4xx is a misrouted or misconfigured request that waiting will not fix.
	if resp.StatusCode >= 400 {
		httpErr := fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(respBody)))
		return resp.StatusCode >= 500 || resp.StatusCode == http.StatusTooManyRequests, httpErr
	}

	if inst.Verify == nil {
		return false, nil
	}
	return false, inst.Verify(respBody)
}
