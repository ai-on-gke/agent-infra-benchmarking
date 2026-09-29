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
	"context"
	"fmt"
	"net/http"
	"strings"
)

// The openclaw actor's surface, mirrored from demos/openclaw/openclaw_actor.py.
// Mirrored rather than shared because that actor is a Python program: there is
// no package to import, so the coupling is to its wire contract and these
// constants are where it is written down.
const (
	openclawWorkloadName = "openclaw"

	// The ActorTemplate in benchmarking/workloads/manifests. It is not in the
	// default WORKLOAD_TEMPLATES set, so deploying it is opt-in.
	openclawTemplate = "openclaw"

	openclawInvokePath = "/invoke"

	// The instruction we send to OpenClaw to trigger the mock agent loop.
	openclawPrompt = "do something please"

	// What the mock agent replies when it has successfully finished the loop.
	openclawExpectedReply = "I have successfully executed the bash commands!"

	textContentType = "text/plain"
)

func init() {
	registerWorkload(openclawWorkload{})
}

// openclawWorkload drives the openclaw actor from demos/openclaw. Its /invoke
// route wraps a headless coding agent. We send a prompt that triggers a 
// mock agent loop simulating a multi-turn LLM interaction.
//
// Unlike glutton's ping, the reply carries no per-request nonce, so it proves
// the actor's handler ran but not that the answer is fresh. The actor's
// contract offers nothing better, and nothing in the path caches, so the
// weaker check is the honest one rather than a chosen tradeoff.
type openclawWorkload struct{}

func (openclawWorkload) Name() string { return openclawWorkloadName }

func (openclawWorkload) DefaultTemplate() string { return openclawTemplate }

// SendInstruction POSTs the prompt to /invoke and verifies the
// reply contains the expected response. The actor answers 500 when an agent 
// turn fails and 504 when one times out; neither is reachable from the fast 
// path, so the shared retry classification treating 5xx as "still activating" 
// stays true here.
func (openclawWorkload) SendInstruction(ctx context.Context, actor actorRef) (bool, error) {
	return sendHTTPInstruction(ctx, actor, httpInstruction{
		Method:      http.MethodPost,
		Path:        openclawInvokePath,
		ContentType: textContentType,
		Body:        []byte(openclawPrompt),
		Verify: func(respBody []byte) error {
			if !strings.Contains(string(respBody), openclawExpectedReply) {
				return fmt.Errorf("unexpected /invoke reply: got %q, want it to contain %q", string(respBody), openclawExpectedReply)
			}
			return nil
		},
	})
}
