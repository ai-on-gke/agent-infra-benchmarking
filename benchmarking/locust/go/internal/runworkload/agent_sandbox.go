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
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

var (
	sandboxClaimsGVR = schema.GroupVersionResource{
		Group:    "extensions.agents.x-k8s.io",
		Version:  "v1beta1",
		Resource: "sandboxclaims",
	}
	sandboxesGVR = schema.GroupVersionResource{
		Group:    "agents.x-k8s.io",
		Version:  "v1beta1",
		Resource: "sandboxes",
	}
	podSnapshotsGVR = schema.GroupVersionResource{
		Group:    "podsnapshot.gke.io",
		Version:  "v1",
		Resource: "podsnapshots",
	}
	podSnapshotManualTriggersGVR = schema.GroupVersionResource{
		Group:    "podsnapshot.gke.io",
		Version:  "v1",
		Resource: "podsnapshotmanualtriggers",
	}
)

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

// -----------------------------------------------------------------------------
// GKE Pod Snapshots Stateful Hibernation Mode
// -----------------------------------------------------------------------------

func (u *runWorkloadUser) resumeSandbox(ctx context.Context) error {
	ctx, span := u.cfg.Tracer.Start(ctx, resumeMetric)
	defer span.End()
	span.SetAttributes(
		attribute.String("actor", u.actorName),
		attribute.String("workload", u.workload.Name()),
	)

	start := time.Now()
	callCtx, cancel := u.callContext(ctx)
	defer cancel()

	// Initial cycle: claim warm sandbox from pool
	if u.claimName == "" {
		claimName := "claim-" + uuid.NewString()
		u.claimName = claimName
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
			return fmt.Errorf("failed to create sandbox claim: %w", err)
		}
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

	// Subsequent cycles: wake from snapshot
	patch := []byte(`{"spec":{"operatingMode":"Running"}}`)
	_, err := u.dynClient.Resource(sandboxesGVR).Namespace(u.cfg.Atespace).Patch(callCtx, u.actorName, types.MergePatchType, patch, metav1.PatchOptions{})
	if err != nil {
		u.recordWindow(ctx, span, resumeMetric, time.Since(start), err)
		return fmt.Errorf("failed to resume sandbox %s: %w", u.actorName, err)
	}

	podIP, err := u.waitForSandboxRunning(callCtx, u.actorName)
	if err != nil {
		u.recordWindow(ctx, span, resumeMetric, time.Since(start), err)
		return fmt.Errorf("failed waiting for sandbox %s to be running: %w", u.actorName, err)
	}

	u.podIP = podIP
	u.isActive = true
	activeActors.WithLabelValues(u.workload.Name()).Inc()
	u.recordWindow(ctx, span, resumeMetric, time.Since(start), nil)
	return nil
}

func (u *runWorkloadUser) snapshotAndSuspend(ctx context.Context) error {
	ctx, span := u.cfg.Tracer.Start(ctx, suspendMetric)
	defer span.End()
	span.SetAttributes(
		attribute.String("actor", u.actorName),
		attribute.String("workload", u.workload.Name()),
	)

	start := time.Now()
	callCtx, cancel := u.callContext(ctx)
	defer cancel()

	// 1. Take snapshot
	if err := u.triggerSnapshotAndWait(callCtx, u.actorName); err != nil {
		u.recordWindow(ctx, span, suspendMetric, time.Since(start), err)
		return fmt.Errorf("failed snapshot during suspend: %w", err)
	}

	// 2. Suspend sandbox
	patch := []byte(`{"spec":{"operatingMode":"Suspended"}}`)
	_, err := u.dynClient.Resource(sandboxesGVR).Namespace(u.cfg.Atespace).Patch(callCtx, u.actorName, types.MergePatchType, patch, metav1.PatchOptions{})
	if err != nil {
		u.recordWindow(ctx, span, suspendMetric, time.Since(start), err)
		return fmt.Errorf("failed to patch operatingMode Suspended: %w", err)
	}

	if err := u.waitForSandboxSuspended(callCtx, u.actorName); err != nil {
		u.recordWindow(ctx, span, suspendMetric, time.Since(start), err)
		return fmt.Errorf("failed waiting for sandbox suspension: %w", err)
	}

	if u.isActive {
		activeActors.WithLabelValues(u.workload.Name()).Dec()
		u.isActive = false
	}
	u.recordWindow(ctx, span, suspendMetric, time.Since(start), nil)
	return nil
}

func (u *runWorkloadUser) cleanupSnapshotSandbox(ctx context.Context) error {
	if u.claimName == "" {
		return nil
	}
	callCtx, cancel := u.callContext(ctx)
	defer cancel()

	_ = u.dynClient.Resource(sandboxClaimsGVR).Namespace(u.cfg.Atespace).Delete(callCtx, u.claimName, metav1.DeleteOptions{})
	_ = u.dynClient.Resource(sandboxesGVR).Namespace(u.cfg.Atespace).Delete(callCtx, u.actorName, metav1.DeleteOptions{})
	u.claimName = ""
	if u.isActive {
		activeActors.WithLabelValues(u.workload.Name()).Dec()
		u.isActive = false
	}
	return nil
}

func (u *runWorkloadUser) waitForSandboxRunning(ctx context.Context, sbName string) (string, error) {
	pollInterval := 50 * time.Millisecond
	for {
		obj, err := u.dynClient.Resource(sandboxesGVR).Namespace(u.cfg.Atespace).Get(ctx, sbName, metav1.GetOptions{})
		if err == nil {
			if podIP, ready := parseSandboxReady(obj); ready {
				return podIP, nil
			}
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(pollInterval):
			pollInterval = min(pollInterval*2, 500*time.Millisecond)
		}
	}
}

func parseSandboxReady(obj *unstructured.Unstructured) (string, bool) {
	status, ok := obj.Object["status"].(map[string]interface{})
	if !ok || status == nil {
		return "", false
	}
	var podIP string
	if podIPs, ok := status["podIPs"].([]interface{}); ok && len(podIPs) > 0 {
		podIP, _ = podIPs[0].(string)
	}
	conditions, ok := status["conditions"].([]interface{})
	if !ok {
		return "", false
	}
	for _, c := range conditions {
		cMap, ok := c.(map[string]interface{})
		if ok && cMap["type"] == "Ready" && cMap["status"] == "True" {
			return podIP, true
		}
	}
	return "", false
}

func (u *runWorkloadUser) waitForSandboxSuspended(ctx context.Context, sbName string) error {
	pollInterval := 50 * time.Millisecond
	for {
		obj, err := u.dynClient.Resource(sandboxesGVR).Namespace(u.cfg.Atespace).Get(ctx, sbName, metav1.GetOptions{})
		if err == nil {
			if isSandboxSuspended(obj) {
				return nil
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(pollInterval):
			pollInterval = min(pollInterval*2, 500*time.Millisecond)
		}
	}
}

func isSandboxSuspended(obj *unstructured.Unstructured) bool {
	status, ok := obj.Object["status"].(map[string]interface{})
	if !ok || status == nil {
		return false
	}
	conditions, ok := status["conditions"].([]interface{})
	if !ok {
		return false
	}
	for _, c := range conditions {
		cMap, ok := c.(map[string]interface{})
		if ok && cMap["type"] == "Suspended" && cMap["status"] == "True" {
			return true
		}
	}
	return false
}

func (u *runWorkloadUser) triggerSnapshotAndWait(ctx context.Context, targetPod string) error {
	triggerName := "snap-" + uuid.NewString()[:8]
	trigger := &unstructured.Unstructured{
		Object: map[string]interface{}{
			"apiVersion": "podsnapshot.gke.io/v1",
			"kind":       "PodSnapshotManualTrigger",
			"metadata": map[string]interface{}{
				"name":      triggerName,
				"namespace": u.cfg.Atespace,
			},
			"spec": map[string]interface{}{
				"targetPod": targetPod,
			},
		},
	}

	_, err := u.dynClient.Resource(podSnapshotManualTriggersGVR).Namespace(u.cfg.Atespace).Create(ctx, trigger, metav1.CreateOptions{})
	if err != nil {
		return fmt.Errorf("failed to create PodSnapshotManualTrigger: %w", err)
	}

	var snapName string
	pollInterval := 100 * time.Millisecond
	deadline, ok := ctx.Deadline()
	if !ok {
		deadline = time.Now().Add(120 * time.Second)
	}

	for time.Now().Before(deadline) {
		obj, err := u.dynClient.Resource(podSnapshotManualTriggersGVR).Namespace(u.cfg.Atespace).Get(ctx, triggerName, metav1.GetOptions{})
		if err == nil {
			status, _ := obj.Object["status"].(map[string]interface{})
			if status != nil {
				if created, ok := status["snapshotCreated"].(map[string]interface{}); ok && created != nil {
					snapName, _ = created["name"].(string)
				}
				conditions, _ := status["conditions"].([]interface{})
				for _, c := range conditions {
					cMap, _ := c.(map[string]interface{})
					if cMap["type"] == "Triggered" && cMap["status"] == "True" {
						goto waitSnapshotReady
					}
				}
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(pollInterval):
			pollInterval = min(pollInterval*2, 1*time.Second)
		}
	}
	return fmt.Errorf("timed out waiting for trigger %s to be Triggered", triggerName)

waitSnapshotReady:
	if snapName == "" {
		return fmt.Errorf("no snapshot name found on trigger %s", triggerName)
	}

	pollInterval = 200 * time.Millisecond
	for time.Now().Before(deadline) {
		obj, err := u.dynClient.Resource(podSnapshotsGVR).Namespace(u.cfg.Atespace).Get(ctx, snapName, metav1.GetOptions{})
		if err == nil {
			status, _ := obj.Object["status"].(map[string]interface{})
			if status != nil {
				conditions, _ := status["conditions"].([]interface{})
				for _, c := range conditions {
					cMap, _ := c.(map[string]interface{})
					if cMap["type"] == "Ready" && cMap["status"] == "True" {
						return nil
					}
				}
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(pollInterval):
			pollInterval = min(pollInterval*2, 1*time.Second)
		}
	}
	return fmt.Errorf("timed out waiting for PodSnapshot %s to become Ready", snapName)
}
