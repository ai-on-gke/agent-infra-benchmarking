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
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/sandbox"
)

// What the workloads whose actor's agent calls mock-llm
// (workloads/images/mock-llm/mock_llm.py) share: the nonce they end a prompt
// with, which mock-llm copies into its last answer, the turn that answer
// gives, and the recorded conversations mock-llm can replay in place of their
// own prompts.

// lastUnknown is where an actor is in its session after a failed turn,
// which may or may not have gone through.
const lastUnknown = -1

// withNonce is prompt as sent, carrying nonce where mock-llm reads it.
func withNonce(prompt, nonce string) string { return prompt + " (request " + nonce + ")" }

// answerTurn checks that the agent's answer carries nonce, so it is this
// turn's, and returns the turn it gives: the "(turn N)" mock-llm puts before
// the nonce, how many prompts the conversation holds.
func answerTurn(answer, nonce string) (int, error) {
	if !strings.Contains(answer, "(request "+nonce+")") {
		return 0, fmt.Errorf("reply %q does not carry nonce %q", answer, nonce)
	}
	match := regexp.MustCompile(`\(turn (\d+)\) \(request ` + regexp.QuoteMeta(nonce) + `\)`).FindStringSubmatch(answer)
	if match == nil {
		return 0, fmt.Errorf("reply %q gives no turn", answer)
	}
	turn, _ := strconv.Atoi(match[1])
	return turn, nil
}

// conversation is a recorded conversation a workload sends, one prompt a
// turn and from the first again after the last, in place of its own prompts,
// and where its actor is in it. A workload embeds it, declares its flag in
// Flags and fetches the prompts in Configure.
type conversation struct {
	turnsURL string
	turns    []string
	// last is the turn the actor answered last: 0 on a fresh actor.
	last int
}

func (c *conversation) flags(fs *flag.FlagSet, workload string) {
	fs.StringVar(&c.turnsURL, workload+"-turns-url", "",
		"mock-llm's /recordings/NAME/turns, which serves the prompts of the recorded conversation it replays:"+
			" they are sent one a turn, from the first again after the last, in place of the workload's own.")
}

// configure gets the prompts from mock-llm, which serves them only while it
// replays that recording, so they match the answers it replays.
func (c *conversation) configure(ctx context.Context) error {
	if c.turnsURL == "" {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, time.Minute)
	defer cancel()
	turns, err := fetchTurns(ctx, c.turnsURL)
	if err != nil {
		return fmt.Errorf("cannot get the conversation's turns: %w", err)
	}
	c.turns = turns
	return nil
}

// recorded reports whether there is a conversation to send.
func (c *conversation) recorded() bool { return len(c.turns) > 0 }

// forActor is the conversation from its start, for a new actor.
func (c *conversation) forActor() conversation { return conversation{turns: c.turns} }

// sendTurn sends the conversation's next turn, the first after its last or a
// failed one, which the actor starts its session over on, as
// POST path?turn=K. answer is the agent's answer in the actor's reply, which
// has to give that turn.
func (c *conversation) sendTurn(ctx context.Context, actor sandbox.Actor, path string, answer func(body []byte) (string, error)) (bool, error) {
	turn := 1
	if c.last >= 1 && c.last < len(c.turns) {
		turn = c.last + 1
	}
	nonce := uuid.NewString()
	retry, err := sendHTTPInstruction(ctx, actor, httpInstruction{
		Method:      http.MethodPost,
		Path:        path + "?turn=" + strconv.Itoa(turn),
		ContentType: textContentType,
		Body:        []byte(withNonce(c.turns[turn-1], nonce)),
		Verify: func(body []byte) error {
			text, err := answer(body)
			if err != nil {
				return err
			}
			got, err := answerTurn(text, nonce)
			if err == nil && got != turn {
				err = fmt.Errorf("session at turn %d, want turn %d", got, turn)
			}
			return err
		},
	})
	c.last = turn
	if err != nil {
		c.last = lastUnknown
	}
	return retry, err
}

// fetchTurns gets the prompts from url. A connection error or a 5xx is tried
// again until ctx is done; any other failure is final.
func fetchTurns(ctx context.Context, url string) ([]string, error) {
	for {
		turns, retry, err := fetchTurnsOnce(ctx, url)
		if err == nil || !retry {
			return turns, err
		}
		select {
		case <-ctx.Done():
			return nil, fmt.Errorf("%w (%w)", err, ctx.Err())
		case <-time.After(2 * time.Second):
		}
	}
}

func fetchTurnsOnce(ctx context.Context, url string) (turns []string, retry bool, err error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, false, err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, true, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, true, err
	}
	if resp.StatusCode != http.StatusOK {
		return nil, resp.StatusCode >= 500, fmt.Errorf("GET %s: HTTP %d: %s", url, resp.StatusCode, strings.TrimSpace(string(body)))
	}
	if err := json.Unmarshal(body, &turns); err != nil || len(turns) == 0 {
		return nil, false, fmt.Errorf("GET %s: want a JSON list of prompts, got %q", url, body)
	}
	return turns, false, nil
}
