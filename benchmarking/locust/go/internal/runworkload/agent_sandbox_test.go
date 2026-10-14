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
	"testing"
	"time"

	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/userclass"
	"go.opentelemetry.io/otel"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	dynamicfake "k8s.io/client-go/dynamic/fake"
)

func TestParseClaimStatus(t *testing.T) {
	// 1. Not ready claim
	notReady := &unstructured.Unstructured{
		Object: map[string]interface{}{
			"status": map[string]interface{}{
				"sandbox": map[string]interface{}{
					"name": "sb-1234",
				},
				"conditions": []interface{}{
					map[string]interface{}{
						"type":   "Ready",
						"status": "False",
					},
				},
			},
		},
	}
	if name, podIP, ready := parseClaimStatus(notReady); ready || name != "" || podIP != "" {
		t.Fatalf("expected not ready, got name=%q podIP=%q ready=%v", name, podIP, ready)
	}

	// 2. Ready claim
	readyClaim := &unstructured.Unstructured{
		Object: map[string]interface{}{
			"status": map[string]interface{}{
				"sandbox": map[string]interface{}{
					"name": "sb-1234",
					"podIPs": []interface{}{
						"10.0.0.1",
					},
				},
				"conditions": []interface{}{
					map[string]interface{}{
						"type":   "Ready",
						"status": "True",
					},
				},
			},
		},
	}
	name, podIP, ready := parseClaimStatus(readyClaim)
	if !ready || name != "sb-1234" || podIP != "10.0.0.1" {
		t.Fatalf("expected ready with sb-1234 and 10.0.0.1, got name=%q podIP=%q ready=%v", name, podIP, ready)
	}

	// 3. Nil/missing status
	empty := &unstructured.Unstructured{Object: map[string]interface{}{}}
	if name, podIP, ready := parseClaimStatus(empty); ready || name != "" || podIP != "" {
		t.Fatalf("expected not ready for empty claim, got name=%q podIP=%q ready=%v", name, podIP, ready)
	}
}

func TestClaimWarmSandboxLifecycle(t *testing.T) {
	scheme := runtime.NewScheme()
	fakeDyn := dynamicfake.NewSimpleDynamicClientWithCustomListKinds(scheme, map[schema.GroupVersionResource]string{
		sandboxClaimsGVR: "SandboxClaimList",
	})

	wl, err := resolveWorkload(DefaultWorkload)
	if err != nil {
		t.Fatalf("resolveWorkload: %v", err)
	}

	u := &runWorkloadUser{
		cfg: &userclass.Config{
			Atespace: "benchmark-workloads",
			Tracer:   otel.Tracer("test"),
		},
		workload:     wl,
		templateName: "glutton",
		userClass:    runWorkloadUserClass,
		provider:     "agent-sandbox",
		warmPoolName: "glutton-warm-pool",
		dynClient:    fakeDyn,
		timeout:      5 * time.Second,
	}

	ctx := context.Background()

	// In background, simulate controller updating claim to ready when created
	go func() {
		for i := 0; i < 50; i++ {
			time.Sleep(20 * time.Millisecond)
			claims, err := fakeDyn.Resource(sandboxClaimsGVR).Namespace("benchmark-workloads").List(ctx, metav1.ListOptions{})
			if err == nil && len(claims.Items) > 0 {
				claim := &claims.Items[0]
				claim.Object["status"] = map[string]interface{}{
					"sandbox": map[string]interface{}{
						"name": "warm-sb-test",
					},
					"conditions": []interface{}{
						map[string]interface{}{
							"type":   "Ready",
							"status": "True",
						},
					},
				}
				_, _ = fakeDyn.Resource(sandboxClaimsGVR).Namespace("benchmark-workloads").Update(ctx, claim, metav1.UpdateOptions{})
				return
			}
		}
	}()

	err = u.claimWarmSandbox(ctx)
	if err != nil {
		t.Fatalf("claimWarmSandbox failed: %v", err)
	}
	if u.actorName != "warm-sb-test" {
		t.Fatalf("expected actorName=warm-sb-test, got %q", u.actorName)
	}
	if !u.isActive {
		t.Fatalf("expected u.isActive=true")
	}

	err = u.releaseClaim(ctx)
	if err != nil {
		t.Fatalf("releaseClaim failed: %v", err)
	}
	if u.isActive {
		t.Fatalf("expected u.isActive=false after release")
	}
}
