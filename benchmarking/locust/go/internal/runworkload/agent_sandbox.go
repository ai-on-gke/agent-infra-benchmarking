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
	"os"
	"path/filepath"
	"time"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel/attribute"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

var sandboxClaimsGVR = schema.GroupVersionResource{
	Group:    "extensions.agents.x-k8s.io",
	Version:  "v1beta1",
	Resource: "sandboxclaims",
}

func initKubeDynamicClient() (dynamic.Interface, error) {
	cfg, err := rest.InClusterConfig()
	if err != nil {
		kubeconfig := os.Getenv("KUBECONFIG")
		if kubeconfig == "" {
			if home, _ := os.UserHomeDir(); home != "" {
				kubeconfig = filepath.Join(home, ".kube", "config")
			}
		}
		cfg, err = clientcmd.BuildConfigFromFlags("", kubeconfig)
		if err != nil {
			return nil, err
		}
	}
	cfg.QPS = 200
	cfg.Burst = 400
	return dynamic.NewForConfig(cfg)
}

func (u *runWorkloadUser) claimWarmSandbox(ctx context.Context) error {
	ctx, span := u.cfg.Tracer.Start(ctx, resumeMetric)
	defer span.End()
	span.SetAttributes(
		attribute.String("warm_pool", u.warmPoolName),
		attribute.String("workload", u.workload.Name()),
	)

	start := time.Now()
	claimName := "claim-" + uuid.NewString()
	u.claimName = claimName

	callCtx, cancel := u.callContext(ctx)
	defer cancel()

	claim := &unstructured.Unstructured{
		Object: map[string]interface{}{
			"apiVersion": "extensions.agents.x-k8s.io/v1beta1",
			"kind":       "SandboxClaim",
			"metadata": map[string]interface{}{
				"name":      claimName,
				"namespace": u.cfg.Atespace,
				"labels": map[string]interface{}{
					"benchmark-user": u.userClass,
				},
			},
			"spec": map[string]interface{}{
				"warmPoolRef": map[string]interface{}{
					"name": u.warmPoolName,
				},
			},
		},
	}

	_, err := u.dynClient.Resource(sandboxClaimsGVR).Namespace(u.cfg.Atespace).Create(callCtx, claim, metav1.CreateOptions{})
	if err != nil {
		u.recordWindow(ctx, span, resumeMetric, time.Since(start), err)
		return fmt.Errorf("failed to create sandbox claim %s: %w", claimName, err)
	}

	// Wait for claim to bind and become ready
	sbName, podIP, err := u.waitForClaimReady(callCtx, claimName)
	if err != nil {
		u.recordWindow(ctx, span, resumeMetric, time.Since(start), err)
		return err
	}

	u.actorName = sbName
	u.podIP = podIP
	u.isActive = true
	activeActors.WithLabelValues(u.workload.Name()).Inc()
	u.recordWindow(ctx, span, resumeMetric, time.Since(start), nil)
	return nil
}

func (u *runWorkloadUser) waitForClaimReady(ctx context.Context, claimName string) (string, string, error) {
	// First do a fast poll: pre-warmed pools often resolve in single-digit ms
	pollInterval := 25 * time.Millisecond
	for i := 0; i < 4; i++ {
		obj, err := u.dynClient.Resource(sandboxClaimsGVR).Namespace(u.cfg.Atespace).Get(ctx, claimName, metav1.GetOptions{})
		if err == nil {
			if sbName, podIP, ready := parseClaimStatus(obj); ready {
				return sbName, podIP, nil
			}
		}
		select {
		case <-ctx.Done():
			return "", "", ctx.Err()
		case <-time.After(pollInterval):
		}
	}

	// Fallback to Watch
	watcher, err := u.dynClient.Resource(sandboxClaimsGVR).Namespace(u.cfg.Atespace).Watch(ctx, metav1.ListOptions{
		FieldSelector: "metadata.name=" + claimName,
	})
	if err != nil {
		return "", "", fmt.Errorf("failed to watch claim %s: %w", claimName, err)
	}
	defer watcher.Stop()

	for {
		select {
		case <-ctx.Done():
			return "", "", fmt.Errorf("timeout waiting for claim %s to become ready: %w", claimName, ctx.Err())
		case event, ok := <-watcher.ResultChan():
			if !ok {
				return "", "", fmt.Errorf("watch channel closed for claim %s", claimName)
			}
			if event.Type == watch.Error {
				return "", "", fmt.Errorf("watch error on claim %s: %v", claimName, event.Object)
			}
			obj, ok := event.Object.(*unstructured.Unstructured)
			if ok {
				if sbName, podIP, ready := parseClaimStatus(obj); ready {
					return sbName, podIP, nil
				}
			}
		}
	}
}

func parseClaimStatus(obj *unstructured.Unstructured) (string, string, bool) {
	status, ok := obj.Object["status"].(map[string]interface{})
	if !ok || status == nil {
		return "", "", false
	}
	sb, ok := status["sandbox"].(map[string]interface{})
	if !ok || sb == nil {
		return "", "", false
	}
	sbName, _ := sb["name"].(string)
	if sbName == "" {
		return "", "", false
	}
	var podIP string
	if podIPs, ok := sb["podIPs"].([]interface{}); ok && len(podIPs) > 0 {
		podIP, _ = podIPs[0].(string)
	}
	conditions, ok := status["conditions"].([]interface{})
	if !ok {
		return "", "", false
	}
	for _, c := range conditions {
		cMap, ok := c.(map[string]interface{})
		if ok && cMap["type"] == "Ready" && cMap["status"] == "True" {
			return sbName, podIP, true
		}
	}
	return "", "", false
}

func (u *runWorkloadUser) releaseClaim(ctx context.Context) error {
	ctx, span := u.cfg.Tracer.Start(ctx, suspendMetric)
	defer span.End()
	span.SetAttributes(
		attribute.String("claim", u.claimName),
		attribute.String("workload", u.workload.Name()),
	)

	start := time.Now()
	err := u.deleteClaim(ctx)
	if u.isActive {
		activeActors.WithLabelValues(u.workload.Name()).Dec()
		u.isActive = false
	}
	u.recordWindow(ctx, span, suspendMetric, time.Since(start), err)
	return err
}

func (u *runWorkloadUser) deleteClaim(ctx context.Context) error {
	if u.claimName == "" {
		return nil
	}
	callCtx, cancel := u.callContext(ctx)
	defer cancel()

	err := u.dynClient.Resource(sandboxClaimsGVR).Namespace(u.cfg.Atespace).Delete(callCtx, u.claimName, metav1.DeleteOptions{})
	u.claimName = ""
	return err
}
