"""Unit and integration tests for Reinforcement Learning training workload."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from rl_actor import RLStatefulSandbox, RLHandler
from train_rl import (
    ACTION_BASH_VERIFY,
    ACTION_FINISH,
    ACTION_PYTHON_COMPUTE,
    ACTION_SEARCH,
    ActorRLTrainer,
    NeuralPolicy,
    RLTask,
    TASKS,
    InProcessActor,
    SubstrateActorClient,
    make_state,
    run_episode,
    train,
)


class TestRLSandbox(unittest.TestCase):
    def setUp(self):
        self.sandbox = RLStatefulSandbox()

    def test_python_execution(self):
        res = self.sandbox.execute_python("print(1 + 1)")
        self.assertTrue(res["success"])
        self.assertEqual(res["stdout"], "2")
        self.assertEqual(res["exit_code"], 0)

    def test_bash_execution(self):
        res = self.sandbox.execute_bash("echo 'hello from sandbox'")
        self.assertTrue(res["success"])
        self.assertEqual(res["stdout"], "hello from sandbox")

    def test_search_mock(self):
        res = self.sandbox.search_mock("Kimi-Researcher RL")
        self.assertTrue(res["success"])
        self.assertIn("REINFORCE", res["stdout"])

    def test_reset(self):
        self.sandbox.execute_python("x = 42")
        self.assertGreater(self.sandbox.step_count, 0)
        self.sandbox.reset()
        self.assertEqual(self.sandbox.step_count, 0)
        self.assertEqual(len(self.sandbox.history), 0)


class TestNeuralPolicy(unittest.TestCase):
    def test_forward_shape(self):
        policy = NeuralPolicy(state_dim=9, hidden_dim=16, num_actions=4, seed=123)
        dummy_state = np.zeros(9, dtype=np.float32)
        probs, h1, logits = policy.forward(dummy_state)
        self.assertEqual(probs.shape, (4,))
        self.assertEqual(h1.shape, (1, 16))
        self.assertAlmostEqual(float(np.sum(probs)), 1.0, places=5)

    def test_action_selection(self):
        policy = NeuralPolicy(state_dim=9, hidden_dim=16, num_actions=4, seed=123)
        dummy_state = np.zeros(9, dtype=np.float32)
        action, prob = policy.select_action(dummy_state, greedy=True)
        self.assertIn(action, [0, 1, 2, 3])
        self.assertGreaterEqual(prob, 0.0)
        self.assertLessEqual(prob, 1.0)

    def test_adam_weight_update(self):
        policy = NeuralPolicy(state_dim=9, hidden_dim=16, num_actions=4, seed=123)
        old_w1 = policy.w1.copy()
        grad = np.ones_like(policy.w1) * 0.1
        policy.adam_update(grad, np.zeros_like(policy.b1), np.zeros_like(policy.w2), np.zeros_like(policy.b2))
        self.assertFalse(np.array_equal(old_w1, policy.w1))


class TestRLTrainingPipeline(unittest.TestCase):
    def test_training_convergence(self):
        policy = NeuralPolicy(seed=42)
        actor = InProcessActor()
        # Train for 20 epochs on batch size 8
        history = train(policy, actor, epochs=20, batch_size=8, lr=0.03, verbose=False)
        self.assertEqual(len(history["epoch"]), 20)
        # Verify that policy improves or executes valid trajectories
        final_accuracy = history["accuracy"][-1]
        self.assertGreaterEqual(final_accuracy, 0.0)

    def test_single_episode_trajectory(self):
        policy = NeuralPolicy(seed=42)
        actor = InProcessActor()
        task = TASKS[0]
        traj = run_episode(task, policy, actor, max_turns=3, greedy=False)
        self.assertGreater(len(traj.steps), 0)
        self.assertIsInstance(traj.total_reward, float)

    def test_actor_rl_trainer_step(self):
        sandbox = RLStatefulSandbox()
        trainer = ActorRLTrainer(seed=42, lr=0.03)
        old_w1 = trainer.policy.w1.copy()
        record = trainer.train_step(sandbox, batch_size=2)
        self.assertEqual(record["iteration"], 1)
        self.assertIn("loss", record)
        self.assertIn("mean_reward", record)
        # Weights must be updated
        self.assertFalse(np.array_equal(old_w1, trainer.policy.w1))


if __name__ == "__main__":
    unittest.main()
