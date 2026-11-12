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

package workload

import (
	"context"
	"fmt"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
)

// The hermes actor's surface, mirrored from
// benchmarking/workloads/images/hermes/hermes_actor.py.
const (
	hermesWorkloadName = "hermes"
	hermesTemplate     = "hermes"
	hermesInvokePath   = "/invoke"
)

func init() {
	register(hermesWorkload{})
}

// hermesWorkload drives the hermes actor from
// benchmarking/workloads/images/hermes: a resident Hermes Agent gateway whose
// /invoke runs one agent turn against mock-llm's scripted loop.
type hermesWorkload struct{}

func (hermesWorkload) Name() string            { return hermesWorkloadName }
func (hermesWorkload) DefaultTemplate() string { return hermesTemplate }

// SendInstruction POSTs the prompt with a fresh nonce to /invoke and succeeds
// only on a reply that carries it, which mock-llm writes at the end of the
// loop.
func (hermesWorkload) SendInstruction(ctx context.Context, actor sandbox.Actor) (bool, error) {
	nonce := uuid.NewString()
	want := mockLLMReplyFor(nonce)
	return sendHTTPInstruction(ctx, actor, httpInstruction{
		Method:      http.MethodPost,
		Path:        hermesInvokePath,
		ContentType: textContentType,
		Body:        []byte(mockLLMPromptFor(nonce)),
		Verify: func(body []byte) error {
			if !strings.Contains(string(body), want) {
				return fmt.Errorf("unexpected /invoke reply: got %q, want it to contain %q", body, want)
			}
			return nil
		},
	})
}
