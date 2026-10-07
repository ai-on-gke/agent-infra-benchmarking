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
	"net"
	"net/netip"
	"slices"

	"github.com/agent-substrate/substrate/pkg/proto/ateapipb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// The name ateapi requires of an actor's one egress policy.
const egressPolicyName = "default"

// resolveEgress turns the hosts in --actor-egress-allow into the /32 prefixes
// an egress policy's CIDR rule takes. Resolved once, here, because the
// gateway authorizes the address an actor dials and never a name; a Service's
// ClusterIP does not change while the Service exists. Actor networking is
// IPv4 only, so only IPv4 addresses are kept.
func resolveEgress(ctx context.Context, resolver *net.Resolver, hosts []string) ([]string, error) {
	var cidrs []string
	for _, host := range hosts {
		addrs := []netip.Addr{}
		if addr, err := netip.ParseAddr(host); err == nil {
			addrs = append(addrs, addr)
		} else {
			resolved, err := resolver.LookupNetIP(ctx, "ip4", host)
			if err != nil {
				return nil, fmt.Errorf("resolving %q: %w", host, err)
			}
			addrs = resolved
		}
		for _, addr := range addrs {
			addr = addr.Unmap()
			if !addr.Is4() {
				return nil, fmt.Errorf("%q: %s is not an IPv4 address", host, addr)
			}
			cidrs = append(cidrs, netip.PrefixFrom(addr, 32).String())
		}
	}
	slices.Sort(cidrs)
	return slices.Compact(cidrs), nil
}

// allowEgress gives the actor an egress policy that lets it reach cidrs and
// nothing else. Without one, substrate's egress gateway refuses every
// connection the actor opens, including the telemetry its SDK exports and a
// workload's calls to its own backends.
//
// It runs between CreateActor and the first resume, because the gateway
// caches an actor's lack of a policy for a while and the actor may connect as
// soon as it runs. The policy is deleted with the actor.
func (u *runWorkloadUser) allowEgress(ctx context.Context, cidrs []string) error {
	return u.tracedCall(ctx, "CreateActorEgressPolicy", func(callCtx context.Context, tr *metadata.MD) error {
		_, err := u.cfg.APIStub.CreateActorEgressPolicy(callCtx, &ateapipb.CreateActorEgressPolicyRequest{
			Actor: u.ref(),
			EgressPolicy: &ateapipb.EgressPolicy{
				Metadata: &ateapipb.ResourceMetadata{Atespace: u.cfg.Atespace, Name: egressPolicyName},
				Rules: []*ateapipb.EgressRule{{
					Cidrs: &ateapipb.CIDRRule{Cidrs: cidrs},
				}},
			},
		}, grpc.Trailer(tr))
		// A retry after an Aborted attempt that did land.
		if status.Code(err) == codes.AlreadyExists {
			return nil
		}
		return err
	})
}
