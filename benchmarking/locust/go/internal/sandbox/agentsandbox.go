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
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
	"go.ate.dev/substrate/agent-infra-benchmarking/benchmarking/locust/go/internal/measure"
	"go.ate.dev/substrate/internal/benchmarking/boomer/userclass"
	"go.opentelemetry.io/otel/attribute"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	utilnet "k8s.io/apimachinery/pkg/util/net"
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

// NewKubeDynamicClient is the client AgentSandbox makes its claims with.
func NewKubeDynamicClient() (dynamic.Interface, error) {
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

// AgentSandbox claims each sandbox from a SandboxWarmPool on agent-sandbox:
// Wake claims a warm one and Sleep releases the claim.
type AgentSandbox struct {
	// Config gives the namespace the claims are made in, and the router.
	Config *userclass.Config
	Client dynamic.Interface
	Rec    measure.Recorder

	// Workload labels the actor gauges, and WarmPools are the
	// SandboxWarmPools the actors are spread over, one each in turn.
	Workload  string
	WarmPools []string

	// Counts the actors created, to spread them over WarmPools.
	started atomic.Int64
}

// Create claims nothing: the first Wake does.
func (p *AgentSandbox) Create(context.Context) (Sandbox, error) {
	pool := p.WarmPools[(p.started.Add(1)-1)%int64(len(p.WarmPools))]
	return &agentSandbox{p: p, name: "sb-" + uuid.NewString(), warmPool: pool}, nil
}

type agentSandbox struct {
	p         *AgentSandbox
	name      string
	warmPool  string
	claimName string
	podIP     string
	isActive  bool
}

func (s *agentSandbox) Actor() Actor {
	h := http.Header{}
	h.Set("X-Sandbox-ID", s.name)
	h.Set("X-Sandbox-Port", "80")
	if s.p.Config.Atespace != "" {
		h.Set("X-Sandbox-Namespace", s.p.Config.Atespace)
	}
	if s.podIP != "" {
		h.Set("X-Sandbox-Pod-IP", s.podIP)
	}
	return Actor{
		HTTPClient: s.p.Config.HTTPClient,
		RouterURL:  s.p.Config.RouterURL,
		Name:       s.name,
		Header:     h,
	}
}

func (s *agentSandbox) Wake(ctx context.Context) error {
	ctx, span := s.p.Rec.Tracer.Start(ctx, ResumeMetric)
	defer span.End()
	span.SetAttributes(
		attribute.String("warm_pool", s.warmPool),
		attribute.String("workload", s.p.Workload),
	)

	start := time.Now()
	claimName := "claim-" + uuid.NewString()
	s.claimName = claimName

	callCtx, cancel := s.p.Rec.Context(ctx)
	defer cancel()

	claim := &unstructured.Unstructured{
		Object: map[string]interface{}{
			"apiVersion": "extensions.agents.x-k8s.io/v1beta1",
			"kind":       "SandboxClaim",
			"metadata": map[string]interface{}{
				"name":      claimName,
				"namespace": s.p.Config.Atespace,
				"labels": map[string]interface{}{
					"benchmark-user": s.p.Rec.UserClass,
				},
			},
			"spec": map[string]interface{}{
				"warmPoolRef": map[string]interface{}{
					"name": s.warmPool,
				},
			},
		},
	}

	err := retryAPI(callCtx, func(attempt int) error {
		_, err := s.claims().Create(callCtx, claim, metav1.CreateOptions{})
		// An earlier attempt the connection dropped on may have landed.
		if attempt > 1 && apierrors.IsAlreadyExists(err) {
			return nil
		}
		return err
	})
	if err != nil {
		s.p.Rec.Window(ctx, span, ResumeMetric, time.Since(start), err)
		return fmt.Errorf("failed to create sandbox claim %s: %w", claimName, err)
	}

	// Wait for claim to bind and become ready
	sbName, podIP, err := s.waitForClaimReady(callCtx, claimName)
	if err != nil {
		s.p.Rec.Window(ctx, span, ResumeMetric, time.Since(start), err)
		return err
	}

	s.name = sbName
	s.podIP = podIP
	s.isActive = true
	activeActors.WithLabelValues(s.p.Workload).Inc()
	s.p.Rec.Window(ctx, span, ResumeMetric, time.Since(start), nil)
	return nil
}

func (s *agentSandbox) claims() dynamic.ResourceInterface {
	return s.p.Client.Resource(sandboxClaimsGVR).Namespace(s.p.Config.Atespace)
}

func (s *agentSandbox) waitForClaimReady(ctx context.Context, claimName string) (string, string, error) {
	// First do a fast poll: pre-warmed pools often resolve in single-digit ms
	pollInterval := 25 * time.Millisecond
	for i := 0; i < 4; i++ {
		obj, err := s.claims().Get(ctx, claimName, metav1.GetOptions{})
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

	// Fallback to Watch, opened again whenever it ends early.
	delay := measure.RetryInitialDelay
	for {
		sbName, podIP, err := s.watchClaim(ctx, claimName)
		if err == nil {
			return sbName, podIP, nil
		}
		if ctx.Err() == nil && !errors.Is(err, errWatchEnded) && !transientAPIError(err) {
			return "", "", fmt.Errorf("failed to watch claim %s: %w", claimName, err)
		}
		select {
		case <-ctx.Done():
			return "", "", fmt.Errorf("timeout waiting for claim %s to become ready (last: %v): %w", claimName, err, ctx.Err())
		case <-time.After(delay):
		}
		delay = min(2*delay, measure.RetryMaxDelay)
	}
}

var errWatchEnded = errors.New("watch ended")

// watchClaim reads the claim, then watches it from that version until it is
// ready, so that no change between the two is missed.
func (s *agentSandbox) watchClaim(ctx context.Context, claimName string) (string, string, error) {
	obj, err := s.claims().Get(ctx, claimName, metav1.GetOptions{})
	if err != nil {
		return "", "", err
	}
	if sbName, podIP, ready := parseClaimStatus(obj); ready {
		return sbName, podIP, nil
	}
	watcher, err := s.claims().Watch(ctx, metav1.ListOptions{
		FieldSelector:   "metadata.name=" + claimName,
		ResourceVersion: obj.GetResourceVersion(),
	})
	if err != nil {
		return "", "", err
	}
	defer watcher.Stop()

	for {
		select {
		case <-ctx.Done():
			return "", "", ctx.Err()
		case event, ok := <-watcher.ResultChan():
			if !ok {
				return "", "", fmt.Errorf("%w: channel closed", errWatchEnded)
			}
			if event.Type == watch.Error {
				return "", "", fmt.Errorf("%w: %v", errWatchEnded, event.Object)
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

// transientAPIError is an error the API server gives while it cannot be
// reached for a while, as when GKE resizes the control plane.
func transientAPIError(err error) bool {
	return utilnet.IsConnectionRefused(err) || utilnet.IsConnectionReset(err) || utilnet.IsProbableEOF(err) ||
		utilnet.IsHTTP2ConnectionLost(err) || apierrors.IsServiceUnavailable(err) || apierrors.IsServerTimeout(err) ||
		apierrors.IsTimeout(err) || apierrors.IsTooManyRequests(err)
}

// retryAPI calls do again, backing off, while it fails with a transient API
// error, until ctx ends. attempt counts from 1.
func retryAPI(ctx context.Context, do func(attempt int) error) error {
	delay := measure.RetryInitialDelay
	for attempt := 1; ; attempt++ {
		err := do(attempt)
		if err == nil || !transientAPIError(err) {
			return err
		}
		select {
		case <-ctx.Done():
			return err
		case <-time.After(delay):
		}
		delay = min(2*delay, measure.RetryMaxDelay)
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

func (s *agentSandbox) Sleep(ctx context.Context) error {
	ctx, span := s.p.Rec.Tracer.Start(ctx, SuspendMetric)
	defer span.End()
	span.SetAttributes(
		attribute.String("claim", s.claimName),
		attribute.String("workload", s.p.Workload),
	)

	start := time.Now()
	err := s.Remove(ctx)
	s.p.Rec.Window(ctx, span, SuspendMetric, time.Since(start), err)
	return err
}

// Settle gives up the claim a failed call may have left, so the next Wake
// claims a warm sandbox afresh, as every Wake does.
func (s *agentSandbox) Settle(ctx context.Context) error { return s.Remove(ctx) }

// Remove deletes the claim, if the sandbox holds one. The gauge drops whether
// or not the delete works, since the sandbox is not used again either way.
func (s *agentSandbox) Remove(ctx context.Context) error {
	if s.isActive {
		activeActors.WithLabelValues(s.p.Workload).Dec()
		s.isActive = false
	}
	if s.claimName == "" {
		return nil
	}
	callCtx, cancel := s.p.Rec.Context(ctx)
	defer cancel()

	err := retryAPI(callCtx, func(attempt int) error {
		err := s.claims().Delete(callCtx, s.claimName, metav1.DeleteOptions{})
		if attempt > 1 && apierrors.IsNotFound(err) {
			return nil
		}
		return err
	})
	s.claimName = ""
	return err
}
