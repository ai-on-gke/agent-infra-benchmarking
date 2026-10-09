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
	"slices"
	"testing"
)

func TestEgressHosts(t *testing.T) {
	for _, tc := range []struct {
		name    string
		hosts   []string
		want    []string
		wantErr bool
	}{
		{name: "none", hosts: nil, want: nil},
		{name: "names", hosts: []string{"b.svc", "A.svc", "b.svc"}, want: []string{"a.svc", "b.svc"}},
		{name: "ipv4", hosts: []string{"10.0.0.1"}, wantErr: true},
		{name: "ipv6", hosts: []string{"2001:db8::1"}, wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := egressHosts(tc.hosts)
			if (err != nil) != tc.wantErr {
				t.Fatalf("egressHosts: got err %v, want error %v", err, tc.wantErr)
			}
			if !tc.wantErr && !slices.Equal(got, tc.want) {
				t.Errorf("egressHosts: got %v, want %v", got, tc.want)
			}
		})
	}
}

// The policy has to be in place before the actor first runs, and a run that
// allows nothing makes no call for it.
func TestEgressPolicyIsCreatedBeforeTheFirstResume(t *testing.T) {
	for _, tc := range []struct {
		name  string
		hosts []string
		want  []string
	}{
		{name: "allowed", hosts: []string{"collector.svc"}, want: []string{"CreateAtespace", "CreateActor", "CreateActorEgressPolicy", "ResumeActor"}},
		{name: "none", hosts: nil, want: []string{"CreateAtespace", "CreateActor", "ResumeActor"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctrl := &fakeControlClient{}
			rt := newTestRuntime(t, ctrl)
			rt.egressHosts = tc.hosts

			rt.iterate()
			rt.iterate()

			calls := ctrl.recordedCalls()
			if len(calls) < len(tc.want) || !slices.Equal(calls[:len(tc.want)], tc.want) {
				t.Fatalf("calls: got %v, want them to start %v", calls, tc.want)
			}
			if tc.hosts == nil {
				return
			}
			policy := ctrl.recordedEgressPolicy()
			if got := policy.GetMetadata().GetName(); got != egressPolicyName {
				t.Errorf("policy name: got %q, want %q", got, egressPolicyName)
			}
			rules := policy.GetRules()
			if len(rules) != 1 || !slices.Equal(rules[0].GetHttp().GetHostnames(), tc.hosts) || rules[0].GetHttp().GetPorts().GetAll() == nil {
				t.Errorf("rules: got %v, want one HTTP rule for %v on all ports", rules, tc.hosts)
			}
		})
	}
}

// An actor that could not get its policy would fail every call it makes, so
// it is deleted rather than kept.
func TestActorWithoutItsEgressPolicyIsDeleted(t *testing.T) {
	ctrl := &fakeControlClient{egressErr: errors.New("refused")}
	rt := newTestRuntime(t, ctrl)
	rt.egressHosts = []string{"collector.svc"}

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
