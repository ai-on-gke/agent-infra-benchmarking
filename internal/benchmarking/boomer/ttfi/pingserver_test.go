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

package ttfi

import (
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/agent-substrate/substrate/internal/atenet"
	gluttonpb "github.com/agent-substrate/substrate/internal/proto/glutton"
	"google.golang.org/protobuf/proto"
)

// pingServer is a stand-in for a glutton actor's /ping route — the one
// instruction TTFI times. It is deliberately not the shared glutton fake:
// that double exists for the disk and RAM routes, and TTFI touches none of
// them, so depending on it would only couple this benchmark to a fixture it
// does not use.
type pingServer struct {
	// Status fails every request with this HTTP status code.
	Status int
	// PingFailures makes the first N requests fail with PingFailStatus before
	// the route starts answering, standing in for an actor that is still
	// activating.
	PingFailures int
	// PingFailStatus is the status those first PingFailures requests return.
	// Defaults to 503, the code the router returns when it cannot place a
	// request on a worker.
	PingFailStatus int
	// PingEcho replaces the echoed message, so a caller that verifies the
	// echo sees a mismatch.
	PingEcho string

	mu          sync.Mutex
	pingCount   int
	targetActor string
}

// RecordedTargetActor returns the ate-target-actor header from the most recent
// request. The router resolves the actor from this alone, so a request that
// omits or misformats it is routed nowhere regardless of its Host.
func (s *pingServer) RecordedTargetActor() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.targetActor
}

// Start serves the fake until the test ends.
func (s *pingServer) Start(t *testing.T) *httptest.Server {
	t.Helper()
	ts := httptest.NewServer(http.HandlerFunc(s.serve))
	t.Cleanup(ts.Close)
	return ts
}

// RecordedPings returns how many requests the route has seen, including the
// ones PingFailures rejected — a TTFI window counts every attempt a client
// would really have made.
func (s *pingServer) RecordedPings() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.pingCount
}

func (s *pingServer) serve(w http.ResponseWriter, r *http.Request) {
	if s.Status != 0 {
		http.Error(w, http.StatusText(s.Status), s.Status)
		return
	}
	if r.URL.Path != pingPath {
		http.NotFound(w, r)
		return
	}

	s.mu.Lock()
	s.pingCount++
	// Recorded before the activating check so a rejected ping still shows what
	// it addressed.
	s.targetActor = r.Header.Get(atenet.TargetActorHeader)
	activating := s.pingCount <= s.PingFailures
	s.mu.Unlock()
	if activating {
		status := s.PingFailStatus
		if status == 0 {
			status = http.StatusServiceUnavailable
		}
		http.Error(w, http.StatusText(status), status)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	var req gluttonpb.PingRequest
	if err := proto.Unmarshal(body, &req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	echo := req.GetMessage()
	if s.PingEcho != "" {
		echo = s.PingEcho
	}

	w.Header().Set("Content-Type", "application/x-protobuf")
	resp, _ := proto.Marshal(&gluttonpb.PingResponse{Message: echo})
	_, _ = w.Write(resp)
}
