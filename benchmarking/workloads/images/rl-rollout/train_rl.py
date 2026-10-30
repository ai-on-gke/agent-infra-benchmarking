"""Reinforcement Learning Training Pipeline for Agent Substrate Workloads.

Trains an autonomous tool-calling policy using on-policy REINFORCE with
outcome rewards and gamma-decay efficiency discounting (inspired by Kimi-Researcher).
Interacts directly with Substrate Actors (or local actors) via HTTP.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# Action Space
ACTION_SEARCH = 0
ACTION_PYTHON_COMPUTE = 1
ACTION_BASH_VERIFY = 2
ACTION_FINISH = 3
ACTION_NAMES = ["SEARCH", "PYTHON_COMPUTE", "BASH_VERIFY", "FINISH"]
NUM_ACTIONS = len(ACTION_NAMES)
NUM_TASKS = 4
STATE_DIM = NUM_TASKS + 5  # [task_one_hot(4), turn_norm, has_searched, has_computed, has_verified, error_flag]


# ==============================================================================
# 1. Neural Policy Network (Vectorized NumPy with Adam Optimizer)
# ==============================================================================

class NeuralPolicy:
    """Multi-Layer Perceptron Policy with Softmax Output and Adam Optimizer."""

    def __init__(self, state_dim: int = STATE_DIM, hidden_dim: int = 32, num_actions: int = NUM_ACTIONS, seed: int = 42):
        np.random.seed(seed)
        # Xavier initialization
        self.w1 = np.random.randn(state_dim, hidden_dim) * np.sqrt(2.0 / (state_dim + hidden_dim))
        self.b1 = np.zeros((1, hidden_dim))
        self.w2 = np.random.randn(hidden_dim, num_actions) * np.sqrt(2.0 / (hidden_dim + num_actions))
        self.b2 = np.zeros((1, num_actions))

        # Adam optimizer state
        self.m_w1, self.v_w1 = np.zeros_like(self.w1), np.zeros_like(self.w1)
        self.m_b1, self.v_b1 = np.zeros_like(self.b1), np.zeros_like(self.b1)
        self.m_w2, self.v_w2 = np.zeros_like(self.w2), np.zeros_like(self.w2)
        self.m_b2, self.v_b2 = np.zeros_like(self.b2), np.zeros_like(self.b2)
        self.adam_t = 0

    def forward(self, state: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Forward pass: returns (probs, hidden_act, logits)."""
        x = state.reshape(1, -1)
        z1 = np.dot(x, self.w1) + self.b1
        h1 = np.maximum(0, z1)  # ReLU
        logits = np.dot(h1, self.w2) + self.b2
        # Numerically stable softmax
        shift_logits = logits - np.max(logits, axis=1, keepdims=True)
        exp_logits = np.exp(shift_logits)
        probs = exp_logits / np.sum(exp_logits, axis=1, keepdims=True)
        return probs[0], h1, logits[0]

    def select_action(self, state: np.ndarray, greedy: bool = False) -> tuple[int, float]:
        probs, _, _ = self.forward(state)
        if greedy:
            action = int(np.argmax(probs))
        else:
            action = int(np.random.choice(len(probs), p=probs))
        return action, float(probs[action])

    def adam_update(self, grad_w1: np.ndarray, grad_b1: np.ndarray, grad_w2: np.ndarray, grad_b2: np.ndarray,
                    lr: float = 0.01, beta1: float = 0.9, beta2: float = 0.999, eps: float = 1e-8) -> None:
        """Applies Adam parameter update with gradient clipping."""
        self.adam_t += 1
        t = self.adam_t

        for g in [grad_w1, grad_b1, grad_w2, grad_b2]:
            np.clip(g, -5.0, 5.0, out=g)

        # Update W2
        self.m_w2 = beta1 * self.m_w2 + (1 - beta1) * grad_w2
        self.v_w2 = beta2 * self.v_w2 + (1 - beta2) * (grad_w2 ** 2)
        m_hat = self.m_w2 / (1 - beta1 ** t)
        v_hat = self.v_w2 / (1 - beta2 ** t)
        self.w2 -= lr * m_hat / (np.sqrt(v_hat) + eps)

        # Update b2
        self.m_b2 = beta1 * self.m_b2 + (1 - beta1) * grad_b2
        self.v_b2 = beta2 * self.v_b2 + (1 - beta2) * (grad_b2 ** 2)
        m_hat = self.m_b2 / (1 - beta1 ** t)
        v_hat = self.v_b2 / (1 - beta2 ** t)
        self.b2 -= lr * m_hat / (np.sqrt(v_hat) + eps)

        # Update W1
        self.m_w1 = beta1 * self.m_w1 + (1 - beta1) * grad_w1
        self.v_w1 = beta2 * self.v_w1 + (1 - beta2) * (grad_w1 ** 2)
        m_hat = self.m_w1 / (1 - beta1 ** t)
        v_hat = self.v_w1 / (1 - beta2 ** t)
        self.w1 -= lr * m_hat / (np.sqrt(v_hat) + eps)

        # Update b1
        self.m_b1 = beta1 * self.m_b1 + (1 - beta1) * grad_b1
        self.v_b1 = beta2 * self.v_b1 + (1 - beta2) * (grad_b1 ** 2)
        m_hat = self.m_b1 / (1 - beta1 ** t)
        v_hat = self.v_b1 / (1 - beta2 ** t)
        self.b1 -= lr * m_hat / (np.sqrt(v_hat) + eps)

    def save(self, path: str) -> None:
        np.savez(path, w1=self.w1, b1=self.b1, w2=self.w2, b2=self.b2)

    def load(self, path: str) -> None:
        data = np.load(path)
        self.w1, self.b1 = data["w1"], data["b1"]
        self.w2, self.b2 = data["w2"], data["b2"]


# ==============================================================================
# 2. Synthetic Agent Tasks (Reasoning, Code & Tool Augmented QA)
# ==============================================================================

@dataclass
class RLTask:
    task_id: int
    prompt: str
    required_tool: int  # Optimal action
    query_payload: str
    expected_output: str


TASKS = [
    RLTask(
        task_id=0,
        prompt="Calculate the 10th Fibonacci number using Python.",
        required_tool=ACTION_PYTHON_COMPUTE,
        query_payload="def f(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\nprint(f(10))",
        expected_output="55",
    ),
    RLTask(
        task_id=1,
        prompt="Search what algorithm Kimi-Researcher uses for agentic training.",
        required_tool=ACTION_SEARCH,
        query_payload="Kimi-Researcher RL algorithm",
        expected_output="REINFORCE",
    ),
    RLTask(
        task_id=2,
        prompt="Verify file count in /tmp using bash commands.",
        required_tool=ACTION_BASH_VERIFY,
        query_payload="ls -1 /tmp | wc -l",
        expected_output="count_verified",
    ),
    RLTask(
        task_id=3,
        prompt="Compute sum of squares of numbers from 1 to 5.",
        required_tool=ACTION_PYTHON_COMPUTE,
        query_payload="print(sum(x**2 for x in range(1, 6)))",
        expected_output="55",
    ),
]


# ==============================================================================
# 3. Substrate Actor Client (HTTP with ate-target-actor header support)
# ==============================================================================

class SubstrateActorClient:
    """Client for communicating with an Actor running on Agent Substrate."""

    def __init__(self, base_url: str = "http://127.0.0.1:80", target_actor: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.target_actor = target_actor

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json"}
        if self.target_actor:
            # Substrate's router matches incoming traffic to actor via ate-target-actor
            headers["ate-target-actor"] = self.target_actor

        data = json.dumps(payload).encode("utf-8") if payload else None
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8")
                elapsed_ms = (time.monotonic() - start) * 1000.0
                try:
                    res = json.loads(body)
                    res["rtt_ms"] = elapsed_ms
                    return res
                except json.JSONDecodeError:
                    return {"stdout": body, "exit_code": 0, "rtt_ms": elapsed_ms, "success": True}
        except Exception as e:
            return {"error": str(e), "exit_code": 1, "success": False, "rtt_ms": (time.monotonic() - start) * 1000.0}

    def reset(self) -> dict[str, Any]:
        return self._request("/reset")

    def execute_step(self, tool: str, action: str) -> dict[str, Any]:
        return self._request("/step", {"tool": tool, "action": action})


# ==============================================================================
# 4. In-Memory Direct Actor (Fallback for zero-dependency standalone execution)
# ==============================================================================

class InProcessActor:
    """Direct execution environment used when no remote Substrate actor is running."""

    def __init__(self) -> None:
        try:
            from .rl_actor import RLStatefulSandbox
        except ImportError:
            from rl_actor import RLStatefulSandbox
        self.sandbox = RLStatefulSandbox()

    def reset(self) -> dict[str, Any]:
        return self.sandbox.reset()

    def execute_step(self, tool: str, action: str) -> dict[str, Any]:
        start = time.monotonic()
        if tool == "python":
            res = self.sandbox.execute_python(action)
        elif tool == "bash":
            res = self.sandbox.execute_bash(action)
        elif tool == "search":
            res = self.sandbox.search_mock(action)
        else:
            res = {"error": f"Unknown tool: {tool}", "success": False, "exit_code": 1}
        res["rtt_ms"] = (time.monotonic() - start) * 1000.0
        return res


# ==============================================================================
# 5. Trajectory & Episode Collection
# ==============================================================================

@dataclass
class StepRecord:
    state: np.ndarray
    action: int
    prob: float
    hidden: np.ndarray
    reward: float = 0.0


@dataclass
class Trajectory:
    task: RLTask
    steps: list[StepRecord] = field(default_factory=list)
    total_reward: float = 0.0
    solved: bool = False
    duration_ms: float = 0.0


def make_state(task_id: int, turn: int, max_turns: int, has_searched: bool, has_computed: bool,
               has_verified: bool, error_flag: bool) -> np.ndarray:
    one_hot_task = [1.0 if i == task_id else 0.0 for i in range(NUM_TASKS)]
    features = one_hot_task + [
        float(turn) / float(max_turns),
        1.0 if has_searched else 0.0,
        1.0 if has_computed else 0.0,
        1.0 if has_verified else 0.0,
        1.0 if error_flag else 0.0,
    ]
    return np.array(features, dtype=np.float32)


def run_episode(task: RLTask, policy: NeuralPolicy, actor: Any, max_turns: int = 5,
                gamma: float = 0.90, greedy: bool = False) -> Trajectory:
    actor.reset()
    traj = Trajectory(task=task)
    start_time = time.monotonic()

    has_searched, has_computed, has_verified = False, False, False
    error_flag = False
    task_completed = False

    for turn in range(max_turns):
        state = make_state(task.task_id, turn, max_turns, has_searched, has_computed,
                           has_verified, error_flag)
        action, prob = policy.select_action(state, greedy=greedy)
        _, h1, _ = policy.forward(state)
        step = StepRecord(state=state, action=action, prob=prob, hidden=h1)

        if action == ACTION_FINISH:
            # Episode finishes
            traj.steps.append(step)
            # Evaluate if task was solved
            if (task.required_tool == ACTION_SEARCH and has_searched) or \
               (task.required_tool == ACTION_PYTHON_COMPUTE and has_computed) or \
               (task.required_tool == ACTION_BASH_VERIFY and has_verified):
                task_completed = True
            break

        # Execute tool call in actor sandbox
        tool_name = "search" if action == ACTION_SEARCH else "python" if action == ACTION_PYTHON_COMPUTE else "bash"
        payload = task.query_payload
        res = actor.execute_step(tool_name, payload)
        error_flag = not res.get("success", False)

        if action == ACTION_SEARCH and res.get("success"):
            has_searched = True
        elif action == ACTION_PYTHON_COMPUTE and res.get("success"):
            has_computed = True
        elif action == ACTION_BASH_VERIFY and res.get("success"):
            has_verified = True

        traj.steps.append(step)

    # Outcome reward assignment (Kimi-Researcher recipe: outcome reward with gamma decay)
    traj.duration_ms = (time.monotonic() - start_time) * 1000.0
    traj.solved = task_completed
    T = len(traj.steps)

    # Outcome reward R
    if task_completed:
        base_reward = 1.0
    else:
        base_reward = -0.5 if T >= max_turns else -0.2

    # Step rewards with gamma decay: r_i = R * (gamma ^ (T - 1 - i))
    total_r = 0.0
    for i, s in enumerate(traj.steps):
        decayed_r = base_reward * (gamma ** (T - 1 - i))
        # Efficiency and redundancy penalty: penalize unnecessary repeat calls
        if s.action != task.required_tool and s.action != ACTION_FINISH:
            decayed_r -= 0.15
        s.reward = decayed_r
        total_r += decayed_r

    traj.total_reward = total_r
    return traj


# ==============================================================================
# 6. Embedded Actor Trainer & REINFORCE Policy Gradient Training Loop
# ==============================================================================

class ActorRLTrainer:
    """Manages in-memory neural policy training within a Substrate Actor across Locust cycles."""

    def __init__(self, seed: int = 42, lr: float = 0.03, gamma: float = 0.90):
        self.policy = NeuralPolicy(seed=seed)
        self.lr = lr
        self.gamma = gamma
        self.iteration = 0
        self.baseline = 0.0
        self.history: list[dict[str, Any]] = []

    def train_step(self, sandbox: Any, batch_size: int = 2) -> dict[str, Any]:
        """Executes an actual on-policy REINFORCE training iteration with Adam weight update."""
        self.iteration += 1
        batch_trajectories = []
        for _ in range(batch_size):
            task = random.choice(TASKS)
            traj = run_episode(task, self.policy, sandbox, gamma=self.gamma, greedy=False)
            batch_trajectories.append(traj)

        grad_w1 = np.zeros_like(self.policy.w1)
        grad_b1 = np.zeros_like(self.policy.b1)
        grad_w2 = np.zeros_like(self.policy.w2)
        grad_b2 = np.zeros_like(self.policy.b2)
        total_loss = 0.0
        total_steps = 0

        epoch_rewards = [t.total_reward for t in batch_trajectories]
        mean_reward = float(np.mean(epoch_rewards))
        self.baseline = 0.9 * self.baseline + 0.1 * mean_reward if self.iteration > 1 else mean_reward

        for traj in batch_trajectories:
            advantage = traj.total_reward - self.baseline
            for s in traj.steps:
                total_steps += 1
                probs, h1, _ = self.policy.forward(s.state)
                one_hot = np.zeros(NUM_ACTIONS)
                one_hot[s.action] = 1.0
                d_logits = (probs - one_hot) * advantage

                grad_w2 += np.dot(h1.T, d_logits.reshape(1, -1))
                grad_b2 += d_logits.reshape(1, -1)

                dh1 = np.dot(d_logits.reshape(1, -1), self.policy.w2.T)
                dz1 = dh1 * (h1 > 0)
                grad_w1 += np.dot(s.state.reshape(-1, 1), dz1)
                grad_b1 += dz1

                total_loss += -math.log(max(s.prob, 1e-8)) * advantage

        norm = max(1, total_steps)
        self.policy.adam_update(grad_w1 / norm, grad_b1 / norm, grad_w2 / norm, grad_b2 / norm, lr=self.lr)

        solved_count = sum(1 for t in batch_trajectories if t.solved)
        avg_loss = total_loss / norm

        record = {
            "iteration": self.iteration,
            "mean_reward": round(mean_reward, 3),
            "loss": round(avg_loss, 4),
            "solved": solved_count > 0,
            "accuracy": round((solved_count / len(batch_trajectories)) * 100.0, 1),
            "steps": total_steps,
        }
        self.history.append(record)
        return record


def train(policy: NeuralPolicy, actor: Any, epochs: int = 40, batch_size: int = 8,
          lr: float = 0.02, gamma: float = 0.90, verbose: bool = True) -> dict[str, Any]:
    history = {
        "epoch": [],
        "mean_reward": [],
        "accuracy": [],
        "mean_steps": [],
        "policy_loss": [],
    }
    baseline = 0.0

    if verbose:
        print("Starting Reinforcement Learning on Agent Substrate Workload...")
        print("Algorithm: REINFORCE (On-Policy with Gamma Decay & Outcome Rewards)")
        print(f"Configuration: Epochs={epochs}, BatchSize={batch_size}, LR={lr}, Gamma={gamma}\n")
        print(f"{'Epoch':<8} | {'Mean Reward':<12} | {'Accuracy':<10} | {'Avg Steps':<10} | {'Policy Loss':<12}")
        print("-" * 62)

    for epoch in range(1, epochs + 1):
        batch_trajectories = []
        for _ in range(batch_size):
            task = random.choice(TASKS)
            traj = run_episode(task, policy, actor, gamma=gamma, greedy=False)
            batch_trajectories.append(traj)

        # Compute Policy Gradients
        grad_w1 = np.zeros_like(policy.w1)
        grad_b1 = np.zeros_like(policy.b1)
        grad_w2 = np.zeros_like(policy.w2)
        grad_b2 = np.zeros_like(policy.b2)
        total_loss = 0.0
        total_steps = 0

        # Moving baseline for variance reduction
        epoch_rewards = [t.total_reward for t in batch_trajectories]
        mean_reward = float(np.mean(epoch_rewards))
        baseline = 0.9 * baseline + 0.1 * mean_reward if epoch > 1 else mean_reward

        for traj in batch_trajectories:
            advantage = traj.total_reward - baseline
            for s in traj.steps:
                total_steps += 1
                probs, h1, _ = policy.forward(s.state)
                # dL/dz2 for softmax cross-entropy with advantage weight
                # grad = (probs - one_hot) * advantage
                one_hot = np.zeros(NUM_ACTIONS)
                one_hot[s.action] = 1.0
                d_logits = (probs - one_hot) * advantage

                # Gradient for W2 and b2
                grad_w2 += np.dot(h1.T, d_logits.reshape(1, -1))
                grad_b2 += d_logits.reshape(1, -1)

                # Backprop into hidden layer
                dh1 = np.dot(d_logits.reshape(1, -1), policy.w2.T)
                dz1 = dh1 * (h1 > 0)  # ReLU derivative
                grad_w1 += np.dot(s.state.reshape(-1, 1), dz1)
                grad_b1 += dz1

                # Policy gradient loss: - log(prob) * advantage
                total_loss += -math.log(max(s.prob, 1e-8)) * advantage

        # Normalize gradients by batch step count
        norm = max(1, total_steps)
        policy.adam_update(grad_w1 / norm, grad_b1 / norm, grad_w2 / norm, grad_b2 / norm, lr=lr)

        accuracy = float(np.mean([1.0 if t.solved else 0.0 for t in batch_trajectories]))
        avg_steps = float(np.mean([len(t.steps) for t in batch_trajectories]))
        avg_loss = total_loss / norm

        history["epoch"].append(epoch)
        history["mean_reward"].append(round(mean_reward, 3))
        history["accuracy"].append(round(accuracy * 100.0, 1))
        history["mean_steps"].append(round(avg_steps, 2))
        history["policy_loss"].append(round(avg_loss, 4))

        if verbose and (epoch % 5 == 0 or epoch == 1 or epoch == epochs):
            print(f"{epoch:<8} | {mean_reward:<12.3f} | {accuracy * 100.0:<9.1f}% | {avg_steps:<10.2f} | {avg_loss:<12.4f}")

    return history


# ==============================================================================
# 7. Evaluation & Main CLI Entrypoint
# ==============================================================================

def evaluate(policy: NeuralPolicy, actor: Any) -> None:
    print("\n--- Evaluation on Test Tasks (Greedy Policy) ---")
    solved_count = 0
    for task in TASKS:
        traj = run_episode(task, policy, actor, greedy=True)
        actions_taken = [ACTION_NAMES[s.action] for s in traj.steps]
        status = "PASSED" if traj.solved else "FAILED"
        print(f"Task {task.task_id}: {task.prompt[:45]}...")
        print(f"  Actions taken : {' -> '.join(actions_taken)}")
        print(f"  Result        : {status} (Reward={traj.total_reward:.2f}, Latency={traj.duration_ms:.1f}ms)")
        if traj.solved:
            solved_count += 1
    print(f"Final Success Rate: {solved_count}/{len(TASKS)} ({solved_count / len(TASKS) * 100.0:.0f}%)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Train an RL Agent Policy against Substrate Actor sandbox.")
    parser.add_argument("--actor-url", type=str, default="", help="Substrate router or actor URL (e.g. http://127.0.0.1:8000)")
    parser.add_argument("--target-actor", type=str, default="", help="ate-target-actor header (e.g. benchmark-workloads/rl-actor-1)")
    parser.add_argument("--epochs", type=int, default=35, help="Number of RL training epochs")
    parser.add_argument("--batch-size", type=int, default=8, help="Rollout batch size per epoch")
    parser.add_argument("--lr", type=float, default=0.03, help="Learning rate")
    parser.add_argument("--save-model", type=str, default="", help="Path to save trained policy weights")
    parser.add_argument("--report-file", type=str, default="", help="Path to save training report JSON")
    args = parser.parse_args()

    # Determine actor execution mode
    if args.actor_url:
        print(f"Connecting to Substrate Actor at {args.actor_url} (target-actor: {args.target_actor or 'none'})...")
        actor = SubstrateActorClient(base_url=args.actor_url, target_actor=args.target_actor or None)
    else:
        print("Using In-Process Substrate Actor Sandbox (zero-dependency standalone mode)...")
        # Ensure fallback can import RLStatefulSandbox
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        actor = InProcessActor()

    policy = NeuralPolicy(seed=42)
    history = train(policy, actor, epochs=args.epochs, batch_size=args.batch_size, lr=args.lr)
    evaluate(policy, actor)

    if args.save_model:
        policy.save(args.save_model)
        print(f"\nTrained model weights saved to {args.save_model}")

    if args.report_file:
        with open(args.report_file, "w") as f:
            json.dump(history, f, indent=2)
        print(f"Training history report saved to {args.report_file}")


if __name__ == "__main__":
    main()
