# Reinforcement Learning Workload on Agent Substrate (`rl-rollout`)

A comprehensive technical recap of the Reinforcement Learning workload implementation, its internal architecture, how it evaluates **Agent Substrate**, and instructions for reproducing results.

---

## 1. What Is This?

This workload is a **realistic, end-to-end Reinforcement Learning benchmark** integrated into `agent-infra-benchmarking`. It evaluates how well **Agent Substrate** handles agentic RL training workloads (specifically modeled after **Moonshot AI’s Kimi-Researcher** and OpenAI Deep Research).

### Key distinction from prior workloads (`glutton`, `openclaw`):
* **`glutton`**: Measures pure network and echo latency with no agent or reasoning logic.
* **`openclaw`**: Runs a mock LLM tool loop against a fake completions server without updating model weights.
* **`rl-rollout`**: Runs an **actual on-policy Policy Gradient (REINFORCE) training pipeline with an Adam optimizer**. Every invocation executes multi-turn tool trajectories inside a sandboxed kernel, evaluates outcome rewards, computes policy gradients, and applies mathematical weight updates to a neural network.

---

## 2. Technical Description of the RL Pipeline Under the Hood

```text
+-----------------------------------------------------------------------------------+
|                        Substrate Actor Sandbox (rl_actor.py)                      |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  |                 Learner: Policy & Optimization Engine                       |  |
|  |                                                                             |  |
|  |   Neural Policy Network pi(a|s)                                             |  |
|  |     [MLP: State=9 -> Hidden=32 (ReLU) -> Softmax(Logits) -> 4 Actions]      |  |
|  |                                                                             |  |
|  |   Adam Optimizer & Baseline                                                 |  |
|  |     - Moving reward baseline: b = 0.9*b + 0.1*R_mean                        |  |
|  |     - Parameter moments: m_t, v_t (W1, b1, W2, b2)                          |  |
|  +-----------------------------------------------------------------------------+  |
|                                      |                                            |
|                       1. Tool action | 2. Observation                             |
|                          a_t         |    s_{t+1}                                 |
|                                      v                                            |
|  +-----------------------------------------------------------------------------+  |
|  |                 Environment: Stateful Execution Sandbox                     |  |
|  |                                                                             |  |
|  |   - Python Tool : Isolated subprocess executing generated Python scripts     |  |
|  |   - Bash Tool   : Isolated subprocess executing shell inspection & tests    |  |
|  |   - Search Tool : Knowledge retrieval engine (indexed database)             |  |
|  +-----------------------------------------------------------------------------+  |
|                                      |                                            |
|                                      | 3. Trajectory complete                     |
|                                      v                                            |
|  +-----------------------------------------------------------------------------+  |
|  |                     Reward Function & Policy Loss                           |  |
|  |                                                                             |  |
|  |   Outcome Correctness:                                                      |  |
|  |     R = +1.0 (task solved), -0.5 (task failed / max turns reached)          |  |
|  |                                                                             |  |
|  |   Kimi-Researcher Gamma-Decay Efficiency Discount:                          |  |
|  |     r_i = R * (gamma ^ (T - 1 - i)) - penalty_redundant                      |  |
|  |                                                                             |  |
|  |   Policy Gradient Loss:                                                     |  |
|  |     grad J(theta) = - (1/N) * sum log pi(a_i|s_i) * (R_traj - b)            |  |
|  +-----------------------------------------------------------------------------+  |
|                                      |                                            |
|                                      | 4. Adam weight update                      |
|                                      v                                            |
|              Updated Policy Weights [W1, b1, W2, b2] in Process RAM                |
|               (Preserved across Substrate SNAPSHOT_FIDELITY_MEMORY)               |
+-----------------------------------------------------------------------------------+
```

### Mathematical & Algorithmic Formulation

#### A. State & Action Representation
* **Action Space (A, 4 discrete actions):**
  1. `ACTION_SEARCH (0)`: Query the indexed knowledge base.
  2. `ACTION_PYTHON_COMPUTE (1)`: Execute generated Python code in the sandbox.
  3. `ACTION_BASH_VERIFY (2)`: Execute shell commands and test scripts.
  4. `ACTION_FINISH (3)`: Terminate trajectory and submit answer.

* **State Feature Vector (`s_t` in R^9):**
  ```text
  s_t = [ one_hot(task_id)[4],  (turn / max_turns),  has_searched,  has_computed,  has_verified,  error_flag ]
  ```
  Where:
  - `one_hot(task_id)[4]`: 4-dimensional binary vector indicating task identity.
  - `turn / max_turns`: Normalized trajectory step position.
  - `has_searched`, `has_computed`, `has_verified`: Binary flags recording tool execution history.
  - `error_flag`: 1.0 if the previous tool command failed or timed out.

#### B. Neural Policy Architecture
Two-layer Multi-Layer Perceptron (MLP) parameterized by `theta = {W1, b1, W2, b2}` with Xavier initialization:
```text
z1     = s * W1 + b1                (Hidden layer input: 1x32)
h1     = max(0, z1)                 (ReLU activation: 1x32)
logits = h1 * W2 + b2               (Action logits: 1x4)

pi(a | s) = Softmax(logits) = exp(logits[a]) / sum_j(exp(logits[j]))
```

#### C. Kimi-Researcher Outcome Reward & Gamma-Decay Discount
Rather than dense step-by-step human annotations, the agent uses **sparse outcome rewards with temporal efficiency discounting**:
```text
r_i = R_outcome * (gamma ^ (T - 1 - i)) - lambda * is_redundant_call
```
* `R_outcome = +1.0` if the required tool solved the task, `-0.5` if it failed or timed out.
* `gamma = 0.90`: Penalizes meandering exploration loops. A shorter 2-step trajectory earns a significantly higher initial reward than a 5-step loop achieving the same result.

#### D. Policy Gradient & Adam Weight Update
* **Advantage Estimation:** `A(tau) = R(tau) - b`, where `b = 0.9*b + 0.1*R_mean` is an exponential moving average baseline to reduce gradient variance.
* **Policy Gradient:**
  ```text
  grad_theta J(theta) = (1 / |Batch|) * sum_{tau} sum_{t=0}^T grad_theta log pi(a_t | s_t) * (R(tau) - b)
  ```
* **Adam Optimizer:** Updates parameters `W1, b1, W2, b2` with first and second momentum moments (`m_t`, `v_t`) and gradient clipping at `[-5.0, 5.0]`.

---

## 3. What Does This Test in Agent Substrate?

Running this workload on GKE benchmarks whether Substrate is structurally suited for agentic RL:

| Substrate Subsystem | How the RL Workload Exercises It | Observed Performance in GKE Run (`rl-rollout-single-2`) |
| :--- | :--- | :--- |
| **Full Memory Snapshotting (`SNAPSHOT_FIDELITY_MEMORY`)** | Captures the entire Python process heap (neural weights $W, b$, Adam moments $m, v$, and iteration count) to GCS on `Suspend`. | **820 ms median** suspend latency (**1,647 successful snapshots**, 0 failures). |
| **Instant Memory Restore (`Resume`)** | Restores the frozen heap directly into RAM. Python resumes execution with updated weights intact without disk reloads. | **510 ms median** resume latency (**1,649 successful restores**). |
| **Kernel Isolation (gVisor / microVM)** | Sandbox code execution (`subprocess.run(["python3", "-c", ...])` and `bash`) runs inside gVisor Sentry or microVM `/dev/kvm`. | Isolates dynamic agent-generated code safely without host escape. |
| **Actor Multiplexing & Density** | Allows many parallel rollout actors to share a small pool of physical worker pods. | Ran 10 active rollout actors on 1 single sandboxed C2 node. |
| **Ingress Routing (`atenet-router`)** | Dispatches requests via `ate-target-actor` to whichever worker currently hosts the thawed sandbox. | **600 ms TTFI** (Time To First Instruction) through router. |
| **Zero-Trust Egress** | Blocks unauthorized outbound network connections from sandbox environments during training. | Contained all actor communication within the cluster. |

---

## 4. Repository Files & Architecture Map

```text
agent-infra-benchmarking/
├── rl-recap.md                                           <-- This recap file
├── benchmarking/
│   ├── workloads/
│   │   ├── images.json                                   <-- Registers 'rl-rollout' image
│   │   ├── manifests/
│   │   │   └── rl-rollout-template.yaml.tmpl             <-- Substrate ActorTemplate (memory snapshot to GCS)
│   │   └── images/rl-rollout/
│   │       ├── Dockerfile                                <-- Builds Python 3.11 + numpy container
│   │       ├── rl_actor.py                               <-- HTTP Actor Server (executes train_step on /invoke)
│   │       ├── train_rl.py                               <-- Core RL Policy, Adam, REINFORCE algorithm
│   │       ├── test_train_rl.py                          <-- 10 unit/integration tests for sandbox & trainer
│   │       └── README.md                                 <-- Workload documentation & equations
│   ├── locust/go/internal/workload/
│   │   ├── rl_rollout.go                                 <-- Go Locust Workload driver (sends training prompts)
│   │   └── rl_rollout_test.go                            <-- Go driver unit tests
│   ├── agent-sandbox/manifests/
│   │   └── rl-rollout-template.yaml.tmpl                 <-- Agent-Sandbox v1beta1 CRD template
│   ├── scenario_runner/src/scenario_runner/
│   │   ├── workloads/__init__.py                         <-- Registers memory limits (512Mi) & images
│   │   ├── control_planes/agent_sandbox.py               <-- Maps workload image for agent-sandbox
│   │   └── infra/dev_infra.py                            <-- Fixed state tracking (is_complete checks)
│   └── scenarios/scenarios.yaml                          <-- Added 'rl-rollout-single' and matrix cases
└── reports/rl-rollout-single-2/                          <-- Verified run report (1,647 cycles, 0 failures)
```

---

## 5. How to Run

### Option 1: Standalone Local Mode (Train Model in Terminal)
Runs the REINFORCE pipeline directly in Python without needing GKE or Substrate:
```bash
./venv/bin/python benchmarking/workloads/images/rl-rollout/train_rl.py --epochs 40 --batch-size 8 --lr 0.03 --save-model rl_policy.npz --report-file rl_training_report.json
```
*Outputs loss, accuracy, and saves `rl_policy.npz` and `rl_training_report.json`.*

### Option 2: Run Unit & Integration Tests
```bash
# Python tests (10 tests: sandbox execution, policy forward pass, Adam update, RL convergence)
./venv/bin/python -m unittest benchmarking/workloads/images/rl-rollout/test_train_rl.py

# Go workload driver tests
cd benchmarking/locust/go && go test -v ./internal/workload/...
```

### Option 3: Run on GKE via Dev Infra (`rl-dev`)
Since your `rl-dev` cluster and images are already provisioned on GKE, you can run anytime using a fresh run ID:
```bash
./benchmarking/tools/run --dev-infra rl-dev --scenario rl-rollout-single --name rl-run-$(date +%s)
```

### Option 4: Run at Scale Across 30 Nodes (Production Cluster)
To benchmark Substrate under heavy load (300 concurrent rollout workers on 30 sandboxed nodes):
```bash
# On Substrate with gVisor
./benchmarking/tools/run --scenario rl-rollout-substrate-gvisor

# On Substrate with MicroVMs (nested virtualization)
./benchmarking/tools/run --scenario rl-rollout-substrate-microvm
```
