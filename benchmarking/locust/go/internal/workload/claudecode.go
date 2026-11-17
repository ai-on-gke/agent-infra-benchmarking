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
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"strings"

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

	// lastUnknown is where an actor is in its session after a failed turn,
	// which may or may not have committed.
	lastUnknown = -1
)

// claudeCodeTurn is the "(turn N)" mock-llm ends its answer with: how many
// prompts the conversation holds.
var claudeCodeTurn = regexp.MustCompile(`\(turn (\d+)\)`)

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
// With turns, it sends a recorded conversation's prompts instead, which
// mock-llm replays the answers to, and a turn counts if its number in the
// session is the one sent.
type claudeCodeWorkload struct {
	// last is the turn the actor answered last: 0 on a fresh actor, which
	// restores the golden snapshot's repo with no session.
	last  int
	turns []string
}

type claudeCodeReply struct {
	Result  string `json:"result"`
	Commits int    `json:"commits"`
	Tasks   int    `json:"tasks"`
}

func (*claudeCodeWorkload) Name() string { return claudeCodeWorkloadName }

func (*claudeCodeWorkload) DefaultTemplate() string { return claudeCodeTemplate }

func (w *claudeCodeWorkload) ForActor() Workload { return &claudeCodeWorkload{turns: w.turns} }

func (*claudeCodeWorkload) WithTurns(prompts []string) Workload {
	return &claudeCodeWorkload{turns: prompts}
}

func (w *claudeCodeWorkload) SendInstruction(ctx context.Context, actor sandbox.Actor) (bool, error) {
	if len(w.turns) > 0 {
		return w.sendTurn(ctx, actor)
	}
	nonce := uuid.NewString()
	turn := lastUnknown
	retry, err := sendHTTPInstruction(ctx, actor, httpInstruction{
		Method:      http.MethodPost,
		Path:        claudeCodeInvokePath,
		ContentType: textContentType,
		Body:        []byte(claudeCodePrompt + " (request " + nonce + ")"),
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
	if !strings.Contains(reply.Result, "(request "+nonce+")") {
		return 0, fmt.Errorf("reply %q does not carry nonce %q", reply.Result, nonce)
	}
	match := claudeCodeTurn.FindStringSubmatch(reply.Result)
	if match == nil {
		return 0, fmt.Errorf("reply %q gives no turn", reply.Result)
	}
	turn, _ := strconv.Atoi(match[1])
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

// sendTurn sends the conversation's next turn, the first after its last or a
// failed one, which the actor starts the session and the repo over on.
func (w *claudeCodeWorkload) sendTurn(ctx context.Context, actor sandbox.Actor) (bool, error) {
	turn := 1
	if w.last >= 1 && w.last < len(w.turns) {
		turn = w.last + 1
	}
	nonce := uuid.NewString()
	retry, err := sendHTTPInstruction(ctx, actor, httpInstruction{
		Method:      http.MethodPost,
		Path:        claudeCodeInvokePath + "?turn=" + strconv.Itoa(turn),
		ContentType: textContentType,
		Body:        []byte(w.turns[turn-1] + " (request " + nonce + ")"),
		Verify: func(body []byte) error {
			return checkClaudeCodeTurn(body, nonce, turn)
		},
	})
	w.last = turn
	if err != nil {
		w.last = lastUnknown
	}
	return retry, err
}

// checkClaudeCodeTurn checks that body answers the conversation's turn.
func checkClaudeCodeTurn(body []byte, nonce string, turn int) error {
	var reply claudeCodeReply
	if err := json.Unmarshal(body, &reply); err != nil {
		return fmt.Errorf("unreadable /invoke reply %q: %w", body, err)
	}
	if !strings.Contains(reply.Result, "(request "+nonce+")") {
		return fmt.Errorf("reply %q does not carry nonce %q", reply.Result, nonce)
	}
	match := claudeCodeTurn.FindStringSubmatch(reply.Result)
	if match == nil {
		return fmt.Errorf("reply %q gives no turn", reply.Result)
	}
	if got, _ := strconv.Atoi(match[1]); got != turn {
		return fmt.Errorf("session at turn %d, want turn %d", got, turn)
	}
	return nil
}
