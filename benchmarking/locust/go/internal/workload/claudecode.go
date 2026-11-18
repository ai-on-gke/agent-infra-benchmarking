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
	"encoding/json"
	"flag"
	"fmt"
	"net/http"

	"github.com/google/uuid"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
)

// The claude-code actor's surface, mirrored from
// benchmarking/workloads/images/claude-code/claude_code_actor.py, and the
// answer mock-llm scripts (workloads/images/mock-llm/mock_llm.py).
const (
	claudeCodeWorkloadName = "claude-code"
	claudeCodeTemplate     = "claude-code"
	claudeCodeInvokePath   = "/invoke"
	claudeCodePrompt       = "Fix the next failing test and commit the fix."
)

func init() {
	register(&claudeCodeWorkload{})
}

// claudeCodeWorkload drives a Claude Code session in the actor, one turn per
// instruction, each fixing one more of the repo's failing tests and
// committing it. A turn counts only if the session and the repo both
// continue from the turn before: its turn number in the conversation and the
// repo's commits are each one more than last time, so a suspend that lost
// either fails it.
//
// With --claude-code-turns-url, it sends a recorded conversation's prompts
// instead, which mock-llm replays the answers to, and a turn counts if its
// number in the session is the one sent.
type claudeCodeWorkload struct {
	// Its last is also the turn of the workload's own session, 0 on a fresh
	// actor, which restores the golden snapshot's repo with no session.
	conversation
}

type claudeCodeReply struct {
	Result  string `json:"result"`
	Commits int    `json:"commits"`
	Tasks   int    `json:"tasks"`
}

func (*claudeCodeWorkload) Name() string { return claudeCodeWorkloadName }

func (*claudeCodeWorkload) DefaultTemplate() string { return claudeCodeTemplate }

func (w *claudeCodeWorkload) ForActor() Workload { return &claudeCodeWorkload{w.forActor()} }

func (w *claudeCodeWorkload) Flags(fs *flag.FlagSet) { w.flags(fs, claudeCodeWorkloadName) }

func (w *claudeCodeWorkload) Configure(ctx context.Context) error { return w.configure(ctx) }

func (w *claudeCodeWorkload) SendInstruction(ctx context.Context, actor sandbox.Actor) (bool, error) {
	if w.recorded() {
		return w.sendTurn(ctx, actor, claudeCodeInvokePath, claudeCodeAnswer)
	}
	nonce := uuid.NewString()
	turn := lastUnknown
	retry, err := sendHTTPInstruction(ctx, actor, httpInstruction{
		Method:      http.MethodPost,
		Path:        claudeCodeInvokePath,
		ContentType: textContentType,
		Body:        []byte(withNonce(claudeCodePrompt, nonce)),
		Verify: func(body []byte) (err error) {
			turn, err = checkClaudeCodeReply(body, nonce, w.last)
			return err
		},
	})
	if err != nil {
		turn = lastUnknown
	}
	w.last = turn
	return retry, err
}

// checkClaudeCodeReply returns the turn body answers, if it is the one that
// follows last.
func checkClaudeCodeReply(body []byte, nonce string, last int) (int, error) {
	var reply claudeCodeReply
	if err := json.Unmarshal(body, &reply); err != nil {
		return 0, fmt.Errorf("unreadable /invoke reply %q: %w", body, err)
	}
	turn, err := answerTurn(reply.Result, nonce)
	if err != nil {
		return 0, err
	}
	if turn != reply.Commits {
		return 0, fmt.Errorf("session at turn %d but repo at commit %d", turn, reply.Commits)
	}
	if reply.Tasks < 1 || turn < 1 || turn > reply.Tasks {
		return 0, fmt.Errorf("turn %d out of the session's %d", turn, reply.Tasks)
	}
	if last != lastUnknown {
		if want := last%reply.Tasks + 1; turn != want {
			return 0, fmt.Errorf("turn %d after turn %d, want turn %d", turn, last, want)
		}
	}
	return turn, nil
}

// claudeCodeAnswer is Claude Code's answer in the actor's reply.
func claudeCodeAnswer(body []byte) (string, error) {
	var reply claudeCodeReply
	if err := json.Unmarshal(body, &reply); err != nil {
		return "", fmt.Errorf("unreadable /invoke reply %q: %w", body, err)
	}
	return reply.Result, nil
}
