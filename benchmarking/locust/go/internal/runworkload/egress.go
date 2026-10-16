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
	"fmt"
	"net/netip"
	"slices"
	"strings"

	"github.com/agent-substrate/substrate/pkg/proto/ateapipb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// The name ateapi requires of an actor's one egress policy.
const egressPolicyName = "default"

// egressHosts checks the hosts in --actor-egress-allow once, at startup. The
// gateway authorizes the name an actor dials, so each has to be that name as
// the actor writes it; an address matches nothing and ateapi rejects it. The
// rest of the hostname syntax is left to ateapi.
func egressHosts(hosts []string) ([]string, error) {
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
func egressRules(hosts, tlsHosts []string) []*ateapipb.EgressRule {
	var rules []*ateapipb.EgressRule
	if len(hosts) > 0 {
		rules = append(rules, &ateapipb.EgressRule{Http: &ateapipb.HTTPRule{
			Hostnames: hosts,
			Ports:     &ateapipb.Ports{All: &ateapipb.AllPorts{}},
		}})
	}
	if len(tlsHosts) > 0 {
		rules = append(rules, &ateapipb.EgressRule{TlsPassthrough: &ateapipb.TLSPassthroughRule{
			Hostnames: tlsHosts,
			Ports:     &ateapipb.Ports{All: &ateapipb.AllPorts{}},
		}})
	}
	return rules
}

// allowEgress gives the actor an egress policy that lets it reach hosts over
// cleartext HTTP and tlsHosts over TLS, and nothing else. Without one,
// substrate's egress gateway refuses every connection the actor opens,
// including the telemetry its SDK exports and a workload's calls to its own
// backends; and a cleartext rule refuses TLS to the same name, which shows in
// the actor as a call that times out rather than as a refusal.
//
// It runs between CreateActor and the first resume, because the gateway
// caches an actor's lack of a policy for a while and the actor may connect as
// soon as it runs. The policy is deleted with the actor.
func (u *runWorkloadUser) allowEgress(ctx context.Context, hosts, tlsHosts []string) error {
	return u.tracedCall(ctx, "CreateActorEgressPolicy", func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.client().CreateActorEgressPolicy(callCtx, &ateapipb.CreateActorEgressPolicyRequest{
			Actor: u.ref(),
			EgressPolicy: &ateapipb.EgressPolicy{
				Metadata: &ateapipb.ResourceMetadata{Atespace: u.cfg.Atespace, Name: egressPolicyName},
				Rules:    egressRules(hosts, tlsHosts),
			},
		}, grpc.Trailer(tr))
		// A retry after an Aborted attempt that did land.
		if status.Code(err) == codes.AlreadyExists {
			return nil
		}
		return err
	})
}
