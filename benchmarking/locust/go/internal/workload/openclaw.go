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
	"flag"
	"fmt"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
)

// The openclaw actor's surface, mirrored from
// benchmarking/workloads/images/openclaw/openclaw_actor.py.
// Mirrored rather than shared because that actor is a Python program: there is
// no package to import, so the coupling is to its wire contract and these
// constants are where it is written down.
const (
	openclawWorkloadName = "openclaw"

	// The ActorTemplate in benchmarking/workloads/manifests.
	openclawTemplate = "openclaw"

	openclawInvokePath = "/invoke"

	// The instruction we send to OpenClaw to trigger the mock agent loop,
	// with a nonce after it.
	openclawPrompt = "do something please"

	// What the mock agent replies when it has successfully finished the loop,
	// followed by the nonce it found in the prompt.
	openclawExpectedReply = "I have successfully executed the bash commands!"
)

func init() {
	register(&openclawWorkload{})
}

// openclawWorkload drives the openclaw actor from
// benchmarking/workloads/images/openclaw. Its /invoke route wraps a headless
// coding agent. We send a prompt that triggers a mock agent loop simulating a
// multi-turn LLM interaction.
//
// Unlike glutton's ping, the reply carries no per-request nonce, so it proves
// the actor's handler ran but not that the answer is fresh. The actor's
// contract offers nothing better, and nothing in the path caches, so the
// weaker check is the honest one rather than a chosen tradeoff.
//
// With --openclaw-turns-url, it sends a recorded conversation's prompts
// instead, which mock-llm replays the answers to, one turn of one openclaw
// session each, and a turn counts if its number in the session is the one
// sent, as claude-code's does.
type openclawWorkload struct {
	conversation
}

func (*openclawWorkload) Name() string { return openclawWorkloadName }

func (*openclawWorkload) DefaultTemplate() string { return openclawTemplate }

func (w *openclawWorkload) ForActor() Workload { return &openclawWorkload{w.forActor()} }

func (w *openclawWorkload) Flags(fs *flag.FlagSet) { w.flags(fs, openclawWorkloadName) }

func (w *openclawWorkload) Configure(ctx context.Context) error { return w.configure(ctx) }

// SendInstruction POSTs the prompt to /invoke and verifies the
// reply contains the expected response. The actor answers 500 when an agent
// turn fails and 504 when one times out; neither is reachable from the fast
// path, so the shared retry classification treating 5xx as "still activating"
// stays true here.
// SendInstruction sends a prompt carrying a fresh nonce, which mock-llm puts
// in its last answer, so a reply counts only if it is this turn's: one that
// openclaw printed from an earlier turn, or without reaching the LLM, fails.
func (w *openclawWorkload) SendInstruction(ctx context.Context, actor sandbox.Actor) (bool, error) {
	if w.recorded() {
		// The actor's reply is openclaw's output, the answer in it.
		return w.sendTurn(ctx, actor, openclawInvokePath, func(body []byte) (string, error) { return string(body), nil })
	}
	nonce := uuid.NewString()
	want := openclawReplyFor(nonce)
	return sendHTTPInstruction(ctx, actor, httpInstruction{
		Method:      http.MethodPost,
		Path:        openclawInvokePath,
		ContentType: textContentType,
		Body:        []byte(openclawPromptFor(nonce)),
		Verify: func(respBody []byte) error {
			if !strings.Contains(string(respBody), want) {
				return fmt.Errorf("unexpected /invoke reply: got %q, want it to contain %q", string(respBody), want)
			}
			return nil
		},
	})
}

// openclawPromptFor and openclawReplyFor carry the nonce in the form mock-llm
// reads it from the prompt and writes it into its answer
// (workloads/images/mock-llm/mock_llm.py).
func openclawPromptFor(nonce string) string { return withNonce(openclawPrompt, nonce) }

func openclawReplyFor(nonce string) string { return openclawExpectedReply + " (request " + nonce + ")" }
