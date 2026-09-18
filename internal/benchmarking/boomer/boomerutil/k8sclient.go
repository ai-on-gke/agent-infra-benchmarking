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

package boomerutil

import (
	"fmt"

	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
	sandboxclientset "sigs.k8s.io/agent-sandbox/clients/k8s/clientset/versioned"
	extensionsclientset "sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned"
)

// LoadKubeConfig resolves a *rest.Config the same way kubectl does: in-cluster
// config first (the normal case — this worker runs as a Pod in the cluster it
// benchmarks), falling back to the local kubeconfig for a benchmark driven
// from outside the cluster (e.g. against a kind cluster during development).
func LoadKubeConfig() (*rest.Config, error) {
	if cfg, err := rest.InClusterConfig(); err == nil {
		return cfg, nil
	}
	loadingRules := clientcmd.NewDefaultClientConfigLoadingRules()
	overrides := &clientcmd.ConfigOverrides{}
	cfg, err := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(loadingRules, overrides).ClientConfig()
	if err != nil {
		return nil, fmt.Errorf("k8s config: %w", err)
	}
	return cfg, nil
}

// DialAgentSandbox builds the two typed clients runworkload needs: the core
// agents.x-k8s.io client for direct (cold) Sandbox creation, and the
// extensions.agents.x-k8s.io client for SandboxClaim/SandboxWarmPool
// (warm-pool) creation. Both share one *rest.Config and its connection pool.
func DialAgentSandbox() (sandboxclientset.Interface, extensionsclientset.Interface, error) {
	cfg, err := LoadKubeConfig()
	if err != nil {
		return nil, nil, err
	}
	sandboxes, err := sandboxclientset.NewForConfig(cfg)
	if err != nil {
		return nil, nil, fmt.Errorf("agent-sandbox client: %w", err)
	}
	extensions, err := extensionsclientset.NewForConfig(cfg)
	if err != nil {
		return nil, nil, fmt.Errorf("agent-sandbox extensions client: %w", err)
	}
	return sandboxes, extensions, nil
}
