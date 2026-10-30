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

package sandbox

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"net/netip"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/measure"
	"go.ate.dev/substrate/api/ateapi/v1"
	"go.ate.dev/substrate/internal/atenet"
	"go.ate.dev/substrate/internal/benchmarking/boomer/userclass"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// The atespace scenario_runner/src/scenario_runner/workloads/ creates its
// ActorTemplates in. Mirrored from it rather than imported.
const templateAtespace = "benchmark-workloads"

// The name ateapi requires of an actor's one egress policy.
const egressPolicyName = "default"

// Substrate creates each sandbox as an actor on agent-substrate, which the
// sandbox then cycles resume/suspend.
type Substrate struct {
	// Config gives the run's atespace, its router, and the connection to
	// substrate's API the actors use when APIStubs is empty.
	Config *userclass.Config
	Rec    measure.Recorder

	// APIStubs are the connections to substrate's API the actors are spread
	// over, one each in turn.
	APIStubs []ateapi.ControlClient

	// Workload labels the actor gauges, and Template is the ActorTemplate the
	// actors are created from.
	Workload string
	Template string

	// EgressHosts are the hosts every actor may connect to over cleartext
	// HTTP, and EgressTLSHosts the ones over TLS, both as EgressHosts returns
	// them. Both empty gives the actors no egress policy.
	EgressHosts    []string
	EgressTLSHosts []string

	// ImplicitResume leaves the wake to the router, so Wake issues no
	// ResumeActor.
	ImplicitResume bool

	// SuspendDisabled skips the suspend phase during the benchmark.
	SuspendDisabled bool

	// SuspendMode is when SuspendActor returns: once the snapshot is on the
	// node (asynchronous) or once it is uploaded (synchronous). Unspecified
	// leaves it to substrate, which suspends asynchronously.
	SuspendMode ateapi.DurabilityMode

	// WaitForWorker has Wake wait out a ResumeActor refused for want of a free
	// worker, as the router does a request, rather than fail; the wait counts
	// toward the call's timeout and is reported as SlotWait.
	WaitForWorker bool

	// Counts the actors created, to spread them over APIStubs.
	started atomic.Int64

	// The run's atespace is created by the first actor, and by any later one
	// only while that has not yet worked.
	atespaceMu    sync.Mutex
	atespaceReady bool
}

// Create makes the actor, which CreateActor leaves SUSPENDED, and gives it its
// egress policy before anything can resume it.
func (p *Substrate) Create(ctx context.Context) (Sandbox, error) {
	s := &substrateSandbox{p: p, api: p.apiStub(), name: "sb-" + uuid.NewString()}
	if err := p.ensureAtespace(ctx, s); err != nil {
		return nil, err
	}
	if err := s.create(ctx); err != nil {
		return nil, err
	}
	if len(p.EgressHosts)+len(p.EgressTLSHosts) > 0 {
		if err := s.allowEgress(ctx); err != nil {
			if delErr := s.delete(ctx); delErr != nil {
				slog.Warn("actor without an egress policy was left behind",
					slog.String("actor", s.name),
					slog.String("err", delErr.Error()))
			}
			return nil, err
		}
	}
	return s, nil
}

// apiStub is the connection the next actor created calls substrate over.
func (p *Substrate) apiStub() ateapi.ControlClient {
	if len(p.APIStubs) == 0 {
		return p.Config.APIStub
	}
	return p.APIStubs[(p.started.Add(1)-1)%int64(len(p.APIStubs))]
}

// ensureAtespace creates the run's atespace once, through the first actor to
// get here. Every actor used to do it, which put a CreateAtespace row and call
// in the run for each actor it created.
func (p *Substrate) ensureAtespace(ctx context.Context, s *substrateSandbox) error {
	p.atespaceMu.Lock()
	defer p.atespaceMu.Unlock()
	if p.atespaceReady {
		return nil
	}
	if err := s.ensureAtespace(ctx); err != nil {
		return err
	}
	p.atespaceReady = true
	return nil
}

type substrateSandbox struct {
	p *Substrate
	// api is the connection to substrate's API this actor's calls go over.
	api  ateapi.ControlClient
	name string

	// Track the state of the actor so we don't double-count metrics if
	// the benchmark calls Resume/Suspend idempotently or repeatedly.
	isActive  bool
	isCreated bool
}

func (s *substrateSandbox) ref() *ateapi.ObjectRef {
	return &ateapi.ObjectRef{Atespace: s.p.Config.Atespace, Name: s.name}
}

func (s *substrateSandbox) suspendRequest() *ateapi.SuspendActorRequest {
	req := &ateapi.SuspendActorRequest{Actor: s.ref()}
	if s.p.SuspendMode != ateapi.DurabilityMode_DURABILITY_MODE_UNSPECIFIED {
		req.Options = &ateapi.SuspendOptions{Mode: s.p.SuspendMode}
	}
	return req
}

func (s *substrateSandbox) Actor() Actor {
	h := http.Header{}
	// The router resolves the actor from this header alone — atenet's ingress
	// handler parses it and 404s with "invalid actor reference" when it is
	// absent, regardless of the request's Host.
	h.Set(atenet.TargetActorHeader, s.p.Config.Atespace+"/"+s.name)
	return Actor{
		HTTPClient: s.p.Config.HTTPClient,
		RouterURL:  s.p.Config.RouterURL,
		Name:       s.name,
		Header:     h,
	}
}

func (s *substrateSandbox) ensureAtespace(ctx context.Context) error {
	return s.p.Rec.GRPC(ctx, "CreateAtespace", func(callCtx context.Context, tr *metadata.MD) error {
		_, err := s.api.CreateAtespace(callCtx, &ateapi.CreateAtespaceRequest{
			Atespace: &ateapi.Atespace{
				Metadata: &ateapi.ResourceMetadata{Name: s.p.Config.Atespace},
			},
		}, grpc.Trailer(tr))
		if err == nil {
			return nil
		}
		if st, ok := status.FromError(err); ok && st.Code() == codes.AlreadyExists {
			return nil
		}
		return err
	})
}

func (s *substrateSandbox) create(ctx context.Context) error {
	err := s.p.Rec.GRPC(ctx, "CreateActor", func(callCtx context.Context, tr *metadata.MD) error {
		_, err := s.api.CreateActor(callCtx, &ateapi.CreateActorRequest{
			Actor: &ateapi.Actor{
				Metadata:      &ateapi.ResourceMetadata{Atespace: s.p.Config.Atespace, Name: s.name},
				ActorTemplate: &ateapi.ObjectRef{Atespace: templateAtespace, Name: s.p.Template},
			},
		}, grpc.Trailer(tr))
		return err
	})
	// CreateActor leaves the actor SUSPENDED; the first resume moves it over.
	if err == nil && !s.isCreated {
		suspendedActors.WithLabelValues(s.p.Workload).Inc()
		s.isCreated = true
	}
	return err
}

// EgressHosts checks the hosts in --actor-egress-allow once, at startup. The
// gateway authorizes the name an actor dials, so each has to be that name as
// the actor writes it; an address matches nothing and ateapi rejects it. The
// rest of the hostname syntax is left to ateapi.
func EgressHosts(hosts []string) ([]string, error) {
	var names []string
	for _, host := range hosts {
		if _, err := netip.ParseAddr(host); err == nil {
			return nil, fmt.Errorf("%q is an address; egress policies allow hostnames only", host)
		}
		names = append(names, strings.ToLower(host))
	}
	slices.Sort(names)
	return slices.Compact(names), nil
}

// egressRules allows hosts over cleartext HTTP, gRPC included, and tlsHosts
// over TLS, each on any port. TLS is passed through rather than intercepted,
// so the actor needs no trust in the gateway's CA.
func egressRules(hosts, tlsHosts []string) []*ateapi.EgressRule {
	var rules []*ateapi.EgressRule
	if len(hosts) > 0 {
		rules = append(rules, &ateapi.EgressRule{Http: &ateapi.HttpRule{
			Hostnames: hosts,
			Ports:     &ateapi.Ports{All: &ateapi.AllPorts{}},
		}})
	}
	if len(tlsHosts) > 0 {
		rules = append(rules, &ateapi.EgressRule{TlsPassthrough: &ateapi.TlsPassthroughRule{
			Hostnames: tlsHosts,
			Ports:     &ateapi.Ports{All: &ateapi.AllPorts{}},
		}})
	}
	return rules
}

// allowEgress gives the actor an egress policy that lets it reach EgressHosts
// over cleartext HTTP and EgressTLSHosts over TLS, and nothing else. Without
// one, substrate's egress gateway refuses every connection the actor opens,
// including the telemetry its SDK exports and a workload's calls to its own
// backends; and a cleartext rule refuses TLS to the same name, which shows in
// the actor as a call that times out rather than as a refusal.
//
// It runs between CreateActor and the first resume, because the gateway
// caches an actor's lack of a policy for a while and the actor may connect as
// soon as it runs. The policy is deleted with the actor.
func (s *substrateSandbox) allowEgress(ctx context.Context) error {
	return s.p.Rec.GRPC(ctx, "CreateActorEgressPolicy", func(callCtx context.Context, tr *metadata.MD) error {
		_, err := s.api.CreateActorEgressPolicy(callCtx, &ateapi.CreateActorEgressPolicyRequest{
			Actor: s.ref(),
			EgressPolicy: &ateapi.EgressPolicy{
				Metadata: &ateapi.ResourceMetadata{Atespace: s.p.Config.Atespace, Name: egressPolicyName},
				Rules:    egressRules(s.p.EgressHosts, s.p.EgressTLSHosts),
			},
		}, grpc.Trailer(tr))
		// A retry after an Aborted attempt that did land.
		if status.Code(err) == codes.AlreadyExists {
			return nil
		}
		return err
	})
}

// Wake resumes the actor, except under implicit resume, where the router
// wakes it while holding the first instruction.
func (s *substrateSandbox) Wake(ctx context.Context) error {
	if s.p.ImplicitResume {
		return nil
	}
	if s.p.SuspendDisabled && s.isActive {
		return nil
	}

	resume := func(callCtx context.Context, tr *metadata.MD) error {
		_, err := s.api.ResumeActor(callCtx, &ateapi.ResumeActorRequest{
			Actor: s.ref(),
		}, grpc.Trailer(tr))
		return err
	}
	var err error
	if s.p.WaitForWorker {
		err = s.p.Rec.GRPCWaiting(ctx, ResumeMetric, SlotWaitMetric, noRoom, resume)
	} else {
		err = s.p.Rec.GRPC(ctx, ResumeMetric, resume)
	}
	if err != nil {
		return err
	}
	s.markActive()
	return nil
}

// noRoom is a ResumeActor refused because no worker has room for the actor.
func noRoom(err error) bool { return status.Code(err) == codes.ResourceExhausted }

// markActive and markSuspended move the actor between the gauges, once per
// transition however often the state is reported.
func (s *substrateSandbox) markActive() {
	if !s.isActive {
		suspendedActors.WithLabelValues(s.p.Workload).Dec()
		activeActors.WithLabelValues(s.p.Workload).Inc()
		s.isActive = true
	}
}

func (s *substrateSandbox) markSuspended() {
	if s.isActive {
		activeActors.WithLabelValues(s.p.Workload).Dec()
		suspendedActors.WithLabelValues(s.p.Workload).Inc()
		s.isActive = false
	}
}

// Sleep suspends the actor and reports whether it went.
func (s *substrateSandbox) Sleep(ctx context.Context) error {
	if s.p.SuspendDisabled {
		return nil
	}

	err := s.p.Rec.GRPC(ctx, SuspendMetric, func(callCtx context.Context, tr *metadata.MD) error {
		_, err := s.api.SuspendActor(callCtx, s.suspendRequest(), grpc.Trailer(tr))
		return err
	})
	if err != nil {
		// measure records the row but logs only a sampled trace, so this
		// is the one place the reason is always said.
		slog.Warn("suspend failed; the actor may be left mid transition",
			slog.String("actor", s.name),
			slog.String("err", err.Error()))
		return err
	}
	s.markSuspended()
	return nil
}

// SettleTimeout bounds how long Settle waits for an actor to leave a
// transitional state, and SettlePoll is how often it looks. Exported variables
// so that tests of the packages using this one can shorten them.
var (
	SettleTimeout = time.Minute
	SettlePoll    = time.Second
)

// Settle polls the actor until it leaves the transitional state a failed call
// can leave it in: a suspend that timed out on the client keeps snapshotting
// on the server. A SUSPENDED actor is kept as it is, and a RUNNING one is
// suspended, unmetered, or left running when suspends are disabled. A CRASHED
// or deleted actor, or one still in transition after SettleTimeout, is not
// cycled again, and the error says which.
func (s *substrateSandbox) Settle(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, SettleTimeout)
	defer cancel()
	for {
		actor, err := s.api.GetActor(ctx, &ateapi.GetActorRequest{Actor: s.ref()})
		state := actor.GetStatus().GetState()
		switch {
		case status.Code(err) == codes.NotFound:
			return fmt.Errorf("actor is gone: %w", err)
		case err != nil:
			// Tried again until the bound, as a refused read says nothing
			// about the actor.
		case state == ateapi.ActorState_ACTOR_STATE_SUSPENDED:
			s.markSuspended()
			return nil
		case state == ateapi.ActorState_ACTOR_STATE_RUNNING:
			return s.settleRunning(ctx)
		case state == ateapi.ActorState_ACTOR_STATE_CRASHED,
			state == ateapi.ActorState_ACTOR_STATE_DELETING:
			return fmt.Errorf("actor is %s", state)
		}

		select {
		case <-ctx.Done():
			if err != nil {
				return fmt.Errorf("actor state unknown after %v: %w", SettleTimeout, err)
			}
			return fmt.Errorf("actor still %s after %v", state, SettleTimeout)
		case <-time.After(SettlePoll):
		}
	}
}

func (s *substrateSandbox) settleRunning(ctx context.Context) error {
	if s.p.SuspendDisabled {
		s.markActive()
		return nil
	}
	callCtx, cancel := s.p.Rec.ControlContext(ctx)
	defer cancel()
	if _, err := s.api.SuspendActor(callCtx, s.suspendRequest()); err != nil {
		return fmt.Errorf("suspending the running actor: %w", err)
	}
	s.markSuspended()
	return nil
}

// Remove deletes the actor, which DeleteActor does in any state, so a running
// or stuck one frees its worker too. The delete is metered so true leaks still
// surface.
func (s *substrateSandbox) Remove(ctx context.Context) error {
	if err := s.delete(ctx); err != nil {
		return fmt.Errorf("delete: %w", err)
	}
	return nil
}

func (s *substrateSandbox) delete(ctx context.Context) error {
	err := s.p.Rec.GRPC(ctx, "DeleteActor", func(callCtx context.Context, tr *metadata.MD) error {
		_, err := s.api.DeleteActor(callCtx, &ateapi.DeleteActorRequest{Actor: s.ref()}, grpc.Trailer(tr))
		// Already gone, which is what the call is for: the teardown and a VU
		// discarding the same actor can both get to it.
		if status.Code(err) == codes.NotFound {
			return nil
		}
		return err
	})

	// Regardless of whether DeleteActor succeeds (it might fail if the actor is
	// stuck suspending, but we're throwing away the benchmark tracking struct anyway),
	// we must decrement our local gauge so it doesn't leak in Prometheus.
	if s.isCreated {
		if s.isActive {
			activeActors.WithLabelValues(s.p.Workload).Dec()
		} else {
			suspendedActors.WithLabelValues(s.p.Workload).Dec()
		}
		s.isCreated = false
		s.isActive = false
	}
	return err
}
