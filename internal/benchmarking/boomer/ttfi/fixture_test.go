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
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/agent-substrate/substrate/internal/ateinterceptors"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/dynconfig"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/userclass"
	"github.com/agent-substrate/substrate/pkg/proto/ateapipb"
	"go.opentelemetry.io/otel"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

// errResumeRefused stands in for a control plane that could not place the
// actor on a worker.
var errResumeRefused = errors.New("no worker available")

type fakeControlClient struct {
	ateapipb.ControlClient
	// resumeErr, when set, fails every ResumeActor call with it.
	resumeErr error
	// serverElapsedUs, when set, is returned as the x-server-elapsed-us
	// trailer the real ServerUnaryInterceptor emits.
	serverElapsedUs string
	mu              sync.Mutex
	calls           []string
	// template is the ActorTemplate name from the last CreateActor.
	template string
}

// setTrailer fills the caller's grpc.Trailer destination so a faked call can
// carry the server's handler duration the way a real one does.
func (f *fakeControlClient) setTrailer(opts []grpc.CallOption) {
	if f.serverElapsedUs == "" {
		return
	}
	for _, o := range opts {
		if t, ok := o.(grpc.TrailerCallOption); ok && t.TrailerAddr != nil {
			*t.TrailerAddr = metadata.Pairs(ateinterceptors.ServerElapsedTrailer, f.serverElapsedUs)
		}
	}
}

func (f *fakeControlClient) CreateAtespace(ctx context.Context, in *ateapipb.CreateAtespaceRequest, opts ...grpc.CallOption) (*ateapipb.Atespace, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "CreateAtespace")
	return &ateapipb.Atespace{}, nil
}

func (f *fakeControlClient) CreateActor(ctx context.Context, in *ateapipb.CreateActorRequest, opts ...grpc.CallOption) (*ateapipb.Actor, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "CreateActor")
	f.template = in.GetActor().GetActorTemplate().GetName()
	return &ateapipb.Actor{}, nil
}

// recordedTemplate returns the ActorTemplate the most recent CreateActor
// named, which is where a template choice stops being a local variable.
func (f *fakeControlClient) recordedTemplate() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.template
}

func (f *fakeControlClient) ResumeActor(ctx context.Context, in *ateapipb.ResumeActorRequest, opts ...grpc.CallOption) (*ateapipb.ResumeActorResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "ResumeActor")
	f.setTrailer(opts)
	if f.resumeErr != nil {
		return nil, f.resumeErr
	}
	return &ateapipb.ResumeActorResponse{}, nil
}

func (f *fakeControlClient) SuspendActor(ctx context.Context, in *ateapipb.SuspendActorRequest, opts ...grpc.CallOption) (*ateapipb.SuspendActorResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "SuspendActor")
	return &ateapipb.SuspendActorResponse{}, nil
}

func (f *fakeControlClient) DeleteActor(ctx context.Context, in *ateapipb.DeleteActorRequest, opts ...grpc.CallOption) (*ateapipb.Actor, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "DeleteActor")
	return &ateapipb.Actor{}, nil
}

func (f *fakeControlClient) recordedCalls() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...)
}

// newTestConfig starts srv, sets HTTPClient and RouterURL, and ensures
// APIStub, Tracer, and Dyn are populated if nil.
func newTestConfig(t *testing.T, srv *pingServer, cfg *userclass.Config) *userclass.Config {
	t.Helper()
	ts := srv.Start(t)
	if cfg == nil {
		cfg = &userclass.Config{}
	}
	if cfg.APIStub == nil {
		cfg.APIStub = &fakeControlClient{}
	}
	if cfg.Tracer == nil {
		cfg.Tracer = otel.Tracer("test")
	}
	if cfg.Atespace == "" {
		cfg.Atespace = "benchmark"
	}
	if cfg.Dyn == nil {
		cfg.Dyn = dynconfig.NewHolder(dynconfig.Config{})
	}
	cfg.HTTPClient = ts.Client()
	cfg.RouterURL = ts.URL
	return cfg
}

func newTestTTFIUser(t *testing.T, srv *pingServer, cfg *userclass.Config) *ttfiUser {
	t.Helper()
	c := newTestConfig(t, srv, cfg)
	return &ttfiUser{
		cfg:          c,
		workload:     gluttonWorkload{},
		actorName:    "ttfiactor",
		templateName: gluttonWorkload{}.DefaultTemplate(),
		userClass:    ttfiUserClass,
	}
}
