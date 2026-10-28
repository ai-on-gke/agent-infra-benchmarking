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
	"slices"
	"testing"

	"go.ate.dev/substrate/internal/atenet"
	"go.ate.dev/substrate/internal/benchmarking/boomer/userclass"
)

// The target header is the only thing atenet resolves an actor from, so its
// shape is part of the contract between the benchmark and every workload.
func TestActorTarget(t *testing.T) {
	s := &substrateSandbox{p: &Substrate{Config: &userclass.Config{Atespace: "benchmark"}}, name: "sb-1"}
	if got := s.Actor().Header.Get(atenet.TargetActorHeader); got != "benchmark/sb-1" {
		t.Errorf("target: got %q, want %q", got, "benchmark/sb-1")
	}
}

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
			got, err := EgressHosts(tc.hosts)
			if (err != nil) != tc.wantErr {
				t.Fatalf("EgressHosts: got err %v, want error %v", err, tc.wantErr)
			}
			if !tc.wantErr && !slices.Equal(got, tc.want) {
				t.Errorf("EgressHosts: got %v, want %v", got, tc.want)
			}
		})
	}
}

func TestEgressRules(t *testing.T) {
	for _, tc := range []struct {
		name            string
		hosts, tlsHosts []string
		wantHTTP        []string
		wantTLS         []string
	}{
		{name: "cleartext only", hosts: []string{"mock-llm"}, wantHTTP: []string{"mock-llm"}},
		{name: "tls only", tlsHosts: []string{"api.example.com"}, wantTLS: []string{"api.example.com"}},
		{name: "both", hosts: []string{"mock-llm"}, tlsHosts: []string{"api.example.com"},
			wantHTTP: []string{"mock-llm"}, wantTLS: []string{"api.example.com"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var gotHTTP, gotTLS []string
			for _, rule := range egressRules(tc.hosts, tc.tlsHosts) {
				if rule.GetHttp() != nil {
					gotHTTP = append(gotHTTP, rule.GetHttp().GetHostnames()...)
					if rule.GetHttp().GetPorts().GetAll() == nil {
						t.Error("cleartext rule: want every port")
					}
				}
				if rule.GetTlsPassthrough() != nil {
					gotTLS = append(gotTLS, rule.GetTlsPassthrough().GetHostnames()...)
					if rule.GetTlsPassthrough().GetPorts().GetAll() == nil {
						t.Error("TLS rule: want every port")
					}
				}
			}
			if !slices.Equal(gotHTTP, tc.wantHTTP) || !slices.Equal(gotTLS, tc.wantTLS) {
				t.Errorf("rules: got http %v tls %v, want http %v tls %v", gotHTTP, gotTLS, tc.wantHTTP, tc.wantTLS)
			}
		})
	}
}
