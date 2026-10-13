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

// Package envd is the client for the in-sandbox envd daemon — the E2B data
// plane the benchmark drives its instruction over. It wraps the generated
// ConnectRPC Process stub (envdpb/process) with the host construction, auth
// headers, and stream handling E2B requires, so a caller runs a command and
// gets its output without touching Connect details. This is the "our own gRPC
// client" half of the integration; the lifecycle half is the sibling control
// package. envd is reached over the HTTPS edge proxy, which is why a ConnectRPC
// (HTTP) client is used rather than a raw gRPC dial.
package envd

import (
	"context"
	"fmt"
	"net/http"
	"strconv"

	"connectrpc.com/connect"

	"github.com/agent-substrate/substrate/agent-infra-benchmarking/internal/benchmarking/e2b/envdpb/process"
	"github.com/agent-substrate/substrate/agent-infra-benchmarking/internal/benchmarking/e2b/envdpb/process/processconnect"
)

// Port is the fixed port envd listens on inside every sandbox. The edge proxy
// routes "<Port>-<sandboxID>.<domain>" to it, so it is also the port that
// appears in the sandbox host and the E2b-Sandbox-Port header.
const Port = 49983

// Client issues Process RPCs to one sandbox's envd over ConnectRPC.
type Client struct {
	proc        processconnect.ProcessClient
	sandboxID   string
	accessToken string
}

// NewClient builds an envd client for one sandbox. domain is the sandbox's
// serving domain (control.Sandbox.Domain, or the configured base domain when
// that is null); accessToken is control.Sandbox.EnvdAccessToken. A nil
// httpClient uses http.DefaultClient.
func NewClient(sandboxID, domain, accessToken string, httpClient *http.Client) *Client {
	if httpClient == nil {
		httpClient = http.DefaultClient
	}
	baseURL := fmt.Sprintf("https://%d-%s.%s", Port, sandboxID, domain)
	return &Client{
		proc:        processconnect.NewProcessClient(httpClient, baseURL),
		sandboxID:   sandboxID,
		accessToken: accessToken,
	}
}

// ExecResult is the outcome of a finished command.
type ExecResult struct {
	Stdout   string
	Stderr   string
	ExitCode int32
}

// Exec runs command through "/bin/sh -c", draining envd's Start stream until
// the process exits, and returns its captured output and exit code. It blocks
// until the process ends or ctx is cancelled. This is the E2B equivalent of a
// substrate workload's single instruction: it returns only once the sandbox has
// demonstrably run the command, so a caller can time the full round-trip.
func (c *Client) Exec(ctx context.Context, command string) (*ExecResult, error) {
	// New SDKs default stdin to false; echo-style instructions read no stdin,
	// and leaving it open risks a process waiting on an EOF that never comes.
	stdin := false
	req := connect.NewRequest(&process.StartRequest{
		Process: &process.ProcessConfig{
			Cmd:  "/bin/sh",
			Args: []string{"-c", command},
		},
		Stdin: &stdin,
	})
	c.setHeaders(req.Header())

	stream, err := c.proc.Start(ctx, req)
	if err != nil {
		return nil, fmt.Errorf("envd Process.Start: %w", err)
	}
	defer stream.Close()

	res := &ExecResult{ExitCode: -1}
	for stream.Receive() {
		ev := stream.Msg().GetEvent()
		if ev == nil {
			continue
		}
		if d := ev.GetData(); d != nil {
			res.Stdout += string(d.GetStdout())
			res.Stderr += string(d.GetStderr())
		}
		if e := ev.GetEnd(); e != nil {
			res.ExitCode = e.GetExitCode()
		}
	}
	if err := stream.Err(); err != nil {
		return res, fmt.Errorf("envd Process.Start stream: %w", err)
	}
	return res, nil
}

// setHeaders stamps the sandbox-routing and auth headers envd requires on every
// request, matching what the E2B SDKs send on the RPC path.
func (c *Client) setHeaders(h http.Header) {
	h.Set("E2b-Sandbox-Id", c.sandboxID)
	h.Set("E2b-Sandbox-Port", strconv.Itoa(Port))
	if c.accessToken != "" {
		h.Set("X-Access-Token", c.accessToken)
	}
}
