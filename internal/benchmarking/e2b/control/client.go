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

// Package control is a thin client for the E2B control-plane REST API: the
// sandbox-lifecycle operations the benchmark measures (create, pause, resume,
// kill). It is hand-written rather than generated because the surface is four
// verbs and the benchmark needs to own exactly what crosses the wire — this is
// the substrate ateapi control stub's E2B counterpart. The in-sandbox
// instruction path (Process.Start over envd) lives in the sibling envd package;
// these two together mirror substrate's control-plus-router split.
package control

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

// Client talks to the E2B API host (https://api.<domain>) with an API key. It
// is safe for concurrent use: it holds no per-request state.
type Client struct {
	httpClient *http.Client
	baseURL    string
	apiKey     string
}

// NewClient builds a control-plane client. domain is the E2B base domain
// (e.g. "e2b.app" for E2B Cloud, or the self-hosted domain); the API host is
// derived as "https://api.<domain>", matching the E2B SDKs. apiKey is sent as
// the X-API-Key header. A nil httpClient uses http.DefaultClient.
func NewClient(domain, apiKey string, httpClient *http.Client) *Client {
	if httpClient == nil {
		httpClient = http.DefaultClient
	}
	return &Client{
		httpClient: httpClient,
		baseURL:    "https://api." + domain,
		apiKey:     apiKey,
	}
}

// CreateRequest is the POST /v2/sandboxes body. Only TemplateID is required;
// the rest are omitted when zero so the server applies its defaults.
type CreateRequest struct {
	TemplateID string            `json:"templateID"`
	Timeout    int               `json:"timeout,omitempty"`
	Metadata   map[string]string `json:"metadata,omitempty"`
	EnvVars    map[string]string `json:"envVars,omitempty"`
}

// Sandbox is the response returned by create and resume. The fields the
// benchmark needs are the identity (SandboxID), where to reach envd (Domain),
// and how to authenticate to it (EnvdAccessToken). Domain and TrafficAccessToken
// are nullable in the API, hence pointers.
type Sandbox struct {
	SandboxID          string  `json:"sandboxID"`
	ClientID           string  `json:"clientID"`
	TemplateID         string  `json:"templateID"`
	EnvdVersion        string  `json:"envdVersion"`
	EnvdAccessToken    string  `json:"envdAccessToken"`
	TrafficAccessToken *string `json:"trafficAccessToken"`
	Domain             *string `json:"domain"`
	Alias              string  `json:"alias"`
}

// resumeRequest is the POST /resume body. Timeout is omitted when zero.
type resumeRequest struct {
	Timeout int `json:"timeout,omitempty"`
}

// Create boots a sandbox from a template. On E2B a create is a real microVM
// boot (unlike substrate's CreateActor, which only reserves a suspended
// identity) — a difference the benchmark report must account for, not this
// client.
func (c *Client) Create(ctx context.Context, req *CreateRequest) (*Sandbox, error) {
	var sb Sandbox
	if err := c.do(ctx, http.MethodPost, "/v2/sandboxes", req, &sb); err != nil {
		return nil, err
	}
	return &sb, nil
}

// Pause snapshots and suspends the sandbox. Maps to substrate's SuspendActor.
func (c *Client) Pause(ctx context.Context, sandboxID string) error {
	return c.do(ctx, http.MethodPost, "/v2/sandboxes/"+sandboxID+"/pause", nil, nil)
}

// Resume wakes a paused sandbox. Maps to substrate's ResumeActor. The resumed
// sandbox may carry a fresh envd access token and domain, so the response is
// decoded and returned; a resume that answers with no body yields a nil Sandbox
// and the caller keeps the pre-pause connection details.
func (c *Client) Resume(ctx context.Context, sandboxID string, timeout int) (*Sandbox, error) {
	var sb Sandbox
	decoded, err := c.doMaybe(ctx, http.MethodPost, "/v2/sandboxes/"+sandboxID+"/resume", resumeRequest{Timeout: timeout}, &sb)
	if err != nil {
		return nil, err
	}
	if !decoded || sb.SandboxID == "" {
		return nil, nil
	}
	return &sb, nil
}

// Kill deletes the sandbox. Maps to substrate's DeleteActor.
func (c *Client) Kill(ctx context.Context, sandboxID string) error {
	return c.do(ctx, http.MethodDelete, "/v2/sandboxes/"+sandboxID, nil, nil)
}

// do performs one request and, when out is non-nil, decodes a JSON response
// into it. A missing or empty body with a non-nil out is an error.
func (c *Client) do(ctx context.Context, method, path string, body, out any) error {
	decoded, err := c.doMaybe(ctx, method, path, body, out)
	if err != nil {
		return err
	}
	if out != nil && !decoded {
		return fmt.Errorf("e2b %s %s: expected a JSON body, got none", method, path)
	}
	return nil
}

// doMaybe performs one request. It reports whether a JSON body was decoded into
// out, which lets callers tolerate endpoints (resume) that may answer 2xx with
// no body. A non-nil out with an empty 2xx body is not an error here.
func (c *Client) doMaybe(ctx context.Context, method, path string, body, out any) (decoded bool, err error) {
	var reqBody io.Reader
	if body != nil {
		buf, err := json.Marshal(body)
		if err != nil {
			return false, fmt.Errorf("e2b %s %s: marshal request: %w", method, path, err)
		}
		reqBody = bytes.NewReader(buf)
	}

	httpReq, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reqBody)
	if err != nil {
		return false, fmt.Errorf("e2b %s %s: build request: %w", method, path, err)
	}
	httpReq.Header.Set("X-API-Key", c.apiKey)
	httpReq.Header.Set("Accept", "application/json")
	if body != nil {
		httpReq.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return false, fmt.Errorf("e2b %s %s: %w", method, path, err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return false, fmt.Errorf("e2b %s %s: read response: %w", method, path, err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return false, fmt.Errorf("e2b %s %s: HTTP %d: %s", method, path, resp.StatusCode, bytes.TrimSpace(respBody))
	}

	if out == nil || len(bytes.TrimSpace(respBody)) == 0 {
		return false, nil
	}
	if err := json.Unmarshal(respBody, out); err != nil {
		return false, fmt.Errorf("e2b %s %s: decode response: %w", method, path, err)
	}
	return true, nil
}
