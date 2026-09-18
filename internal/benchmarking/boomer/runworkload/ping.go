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
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/google/uuid"
)

// ping's own wire contract, mirrored from
// benchmarking/workloads/probe/server.py: neither the route nor its encoding
// belongs to the load generator, so it is written down here rather than
// imported from anywhere.
const (
	pingWorkloadName = "ping"

	// The SandboxTemplate benchmarking/workloads/deploy.sh creates for ping.
	pingTemplate = "ping"

	pingPath        = "/ping"
	pingContentType = "application/json"
)

func init() {
	registerWorkload(pingWorkload{})
}

// pingRequest/pingResponse mirror the probe's /ping JSON contract: it echoes
// the nonce it was sent, which is what makes the probe usable here — it
// distinguishes a sandbox that ran the instruction from one that merely
// accepted the connection, the line a measurement window closes on.
type pingRequest struct {
	Nonce string `json:"nonce"`
}

type pingResponse struct {
	Nonce string `json:"nonce"`
}

// pingWorkload drives the tiny HTTP probe built from
// benchmarking/workloads/probe/. This is the substrate glutton analog: a
// minimal, purpose-built target whose only job is to prove a sandbox is
// reachable and running, so activation latency isn't buried under
// application startup time.
type pingWorkload struct{}

func (pingWorkload) Name() string { return pingWorkloadName }

func (pingWorkload) DefaultTemplate() string { return pingTemplate }

// SendInstruction POSTs one ping and verifies the echo. A wrong echo is a
// fault rather than a symptom of activation, so it is reported as
// unretryable even though the sandbox answered.
func (pingWorkload) SendInstruction(ctx context.Context, target sandboxTarget) (bool, error) {
	nonce := uuid.NewString()
	body, err := json.Marshal(pingRequest{Nonce: nonce})
	if err != nil {
		return false, err
	}

	return sendHTTPInstruction(ctx, target, httpInstruction{
		Method:      http.MethodPost,
		Path:        pingPath,
		ContentType: pingContentType,
		Body:        body,
		Verify: func(respBody []byte) error {
			var pong pingResponse
			if err := json.Unmarshal(respBody, &pong); err != nil {
				return err
			}
			if pong.Nonce != nonce {
				return fmt.Errorf("ping echo mismatch: sent=%q recv=%q", nonce, pong.Nonce)
			}
			return nil
		},
	})
}
