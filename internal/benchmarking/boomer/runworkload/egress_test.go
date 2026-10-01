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
	"errors"
	"net"
	"slices"
	"testing"
)

func TestResolveEgress(t *testing.T) {
	for _, tc := range []struct {
		name    string
		hosts   []string
		want    []string
		wantErr bool
	}{
		{name: "none", hosts: nil, want: nil},
		{name: "addresses", hosts: []string{"10.0.0.2", "10.0.0.1", "10.0.0.2"}, want: []string{"10.0.0.1/32", "10.0.0.2/32"}},
		{name: "name", hosts: []string{"localhost"}, want: []string{"127.0.0.1/32"}},
		{name: "ipv6", hosts: []string{"2001:db8::1"}, wantErr: true},
		{name: "unresolvable", hosts: []string{"no-such-host.invalid"}, wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := resolveEgress(context.Background(), net.DefaultResolver, tc.hosts)
			if (err != nil) != tc.wantErr {
				t.Fatalf("resolveEgress: got err %v, want error %v", err, tc.wantErr)
			}
			if !tc.wantErr && !slices.Equal(got, tc.want) {
				t.Errorf("resolveEgress: got %v, want %v", got, tc.want)
			}
		})
	}
}

// The policy has to be in place before the actor first runs, and a run that
// allows nothing makes no call for it.
func TestEgressPolicyIsCreatedBeforeTheFirstResume(t *testing.T) {
	for _, tc := range []struct {
		name  string
		cidrs []string
		want  []string
	}{
		{name: "allowed", cidrs: []string{"10.0.0.1/32"}, want: []string{"CreateAtespace", "CreateActor", "CreateActorEgressPolicy", "ResumeActor"}},
		{name: "none", cidrs: nil, want: []string{"CreateAtespace", "CreateActor", "ResumeActor"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctrl := &fakeControlClient{}
			rt := newTestRuntime(t, ctrl)
			rt.egressCIDRs = tc.cidrs

			rt.iterate()
			rt.iterate()

			calls := ctrl.recordedCalls()
			if len(calls) < len(tc.want) || !slices.Equal(calls[:len(tc.want)], tc.want) {
				t.Fatalf("calls: got %v, want them to start %v", calls, tc.want)
			}
			if tc.cidrs == nil {
				return
			}
			policy := ctrl.recordedEgressPolicy()
			if got := policy.GetMetadata().GetName(); got != egressPolicyName {
				t.Errorf("policy name: got %q, want %q", got, egressPolicyName)
			}
			rules := policy.GetRules()
			if len(rules) != 1 || !slices.Equal(rules[0].GetCidrs().GetCidrs(), tc.cidrs) {
				t.Errorf("rules: got %v, want one CIDR rule for %v", rules, tc.cidrs)
			}
		})
	}
}

// An actor that could not get its policy would fail every call it makes, so
// it is deleted rather than kept.
func TestActorWithoutItsEgressPolicyIsDeleted(t *testing.T) {
	ctrl := &fakeControlClient{egressErr: errors.New("refused")}
	rt := newTestRuntime(t, ctrl)
	rt.egressCIDRs = []string{"10.0.0.1/32"}

	if _, err := rt.startUser(context.Background(), rt.opts); err == nil {
		t.Fatal("startUser: got nil error, want the policy's")
	}
	if got := ctrl.countCalls("DeleteActor"); got != 1 {
		t.Errorf("DeleteActor calls: got %d, want 1", got)
	}
	if got := ctrl.countCalls("ResumeActor"); got != 0 {
		t.Errorf("ResumeActor calls: got %d, want 0", got)
	}
}
