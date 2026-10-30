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

	"github.com/agent-substrate/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
	"github.com/google/uuid"
)

const (
	rlRolloutWorkloadName = "rl-rollout"

	// The ActorTemplate in benchmarking/workloads/manifests.
	rlRolloutTemplate = "rl-rollout"

	rlRolloutInvokePath = "/invoke"

	rlRolloutPrompt = "run rl training step"

	rlRolloutExpectedReply = "RL training step"
)

func init() {
	register(rlRolloutWorkload{})
}

// rlRolloutWorkload drives the rl-rollout actor from
// benchmarking/workloads/images/rl-rollout. Its /invoke route triggers an actual
// on-policy RL training iteration (REINFORCE) with policy parameter updates inside
// the actor sandbox.
type rlRolloutWorkload struct{}

func (rlRolloutWorkload) Name() string { return rlRolloutWorkloadName }

func (rlRolloutWorkload) DefaultTemplate() string { return rlRolloutTemplate }

func (rlRolloutWorkload) SendInstruction(ctx context.Context, actor sandbox.Actor) (bool, error) {
	nonce := uuid.NewString()
	wantNonce := fmt.Sprintf("(request %s)", nonce)
	return sendHTTPInstruction(ctx, actor, httpInstruction{
		Method:      http.MethodPost,
		Path:        rlRolloutInvokePath,
		ContentType: textContentType,
		Body:        []byte(rlRolloutPromptFor(nonce)),
		Verify: func(respBody []byte) error {
			respStr := string(respBody)
			if !strings.Contains(respStr, rlRolloutExpectedReply) || !strings.Contains(respStr, wantNonce) {
				return fmt.Errorf("unexpected /invoke reply: got %q, want it to contain %q and %q", respStr, rlRolloutExpectedReply, wantNonce)
			}
			return nil
		},
	})
}

func rlRolloutPromptFor(nonce string) string {
	return rlRolloutPrompt + " (request " + nonce + ")"
}
