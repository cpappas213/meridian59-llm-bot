from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from meridian_bot.controller import (
    EXECUTION_PLAN_PROGRESS_RUNTIME_KEY,
    BotController,
)

from .helpers import config


def execution_plan(*, repeat_count: int = 1) -> dict[str, Any]:
    return {
        "schema_version": 5,
        "summary": "Perform two ordered actions, then return safely.",
        "steps": [
            {
                "id": "first",
                "tool": "act",
                "outcome": "Perform the first action.",
                "verification": "The first action is observed.",
                "repeat_count": repeat_count,
            },
            {
                "id": "second",
                "tool": "inventory",
                "outcome": "Verify inventory after the first action.",
                "verification": "A current inventory is observed.",
            },
            {
                "id": "finish-safe",
                "tool": "travel",
                "outcome": "Finish in source-verified safe room 100.",
                "verification": "Current room id is 100.",
            },
        ],
        "safe_ending": {
            "room_id": 100,
            "step_id": "finish-safe",
            "rationale": "Use the source-verified safe staging room.",
        },
    }


class ExecutionPlanProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.controller = BotController(config(Path(self.temporary.name)))
        self.goal: dict[str, Any] = {"id": "goal-progress"}
        self.plan = execution_plan()
        # Progress updates intentionally resolve the currently persisted plan.
        # Keep these tests focused on the sidecar state machine itself.
        self.controller._execution_plan = (  # type: ignore[method-assign]
            lambda goal: self.plan
        )

    def tearDown(self) -> None:
        self.controller.storage.close()
        self.temporary.cleanup()

    def progress(self) -> dict[str, Any]:
        return self.controller._execution_plan_progress(self.goal, self.plan)

    def update(
        self,
        step_id: str,
        *,
        status: str = "succeeded",
        arguments: dict[str, Any] | None = None,
    ) -> None:
        self.controller._update_execution_plan_progress(
            self.goal,
            step_id=step_id,
            arguments=arguments or {},
            result={"ok": True, "step": step_id},
            status=status,
        )

    def test_only_the_ordered_current_step_is_active(self) -> None:
        initialized = self.controller._reset_execution_plan_progress(
            self.goal, self.plan
        )

        self.assertEqual("first", self.controller._active_execution_plan_step(
            self.goal, self.plan, safe_return_required=False
        )["id"])
        self.assertEqual(0, initialized["cursor"])
        self.assertEqual("ready", initialized["steps"]["first"]["status"])
        self.assertEqual("pending", initialized["steps"]["second"]["status"])
        self.assertEqual(
            "locked_safe_return",
            initialized["steps"]["finish-safe"]["status"],
        )

    def test_success_advances_to_the_next_ordered_step(self) -> None:
        self.controller._reset_execution_plan_progress(self.goal, self.plan)

        self.update("first")

        progress = self.progress()
        self.assertEqual(1, progress["cursor"])
        self.assertEqual("satisfied", progress["steps"]["first"]["status"])
        self.assertEqual("ready", progress["steps"]["second"]["status"])
        self.assertEqual("second", self.controller._active_execution_plan_step(
            self.goal, self.plan, safe_return_required=False
        )["id"])

    def test_repeat_count_requires_every_success_before_advancing(self) -> None:
        self.plan = execution_plan(repeat_count=2)
        self.controller._reset_execution_plan_progress(self.goal, self.plan)

        self.update("first")

        after_one = self.progress()
        self.assertEqual(0, after_one["cursor"])
        self.assertEqual(1, after_one["steps"]["first"]["successful_calls"])
        self.assertEqual("ready", after_one["steps"]["first"]["status"])
        self.assertEqual("first", self.controller._active_execution_plan_step(
            self.goal, self.plan, safe_return_required=False
        )["id"])

        self.update("first")

        after_two = self.progress()
        self.assertEqual(1, after_two["cursor"])
        self.assertEqual(2, after_two["steps"]["first"]["successful_calls"])
        self.assertEqual("satisfied", after_two["steps"]["first"]["status"])
        self.assertEqual("second", self.controller._active_execution_plan_step(
            self.goal, self.plan, safe_return_required=False
        )["id"])

    def test_partial_progress_keeps_step_locked_and_remembers_arguments(self) -> None:
        self.controller._reset_execution_plan_progress(self.goal, self.plan)
        arguments = {"verb": "move", "target": "north"}

        self.update("first", status="partial_progress", arguments=arguments)

        progress = self.progress()
        state = progress["steps"]["first"]
        self.assertEqual(0, progress["cursor"])
        self.assertEqual("partial", state["status"])
        self.assertEqual(1, state["attempt_count"])
        self.assertEqual(arguments, state["last_arguments"])
        self.assertEqual("first", self.controller._active_execution_plan_step(
            self.goal, self.plan, safe_return_required=False
        )["id"])

    def test_safe_step_is_hidden_until_safe_return_is_required(self) -> None:
        self.controller._reset_execution_plan_progress(self.goal, self.plan)
        self.update("first")
        self.update("second")

        progress = self.progress()
        self.assertEqual(2, progress["cursor"])
        self.assertIsNone(self.controller._active_execution_plan_step(
            self.goal, self.plan, safe_return_required=False
        ))
        safe_step = self.controller._active_execution_plan_step(
            self.goal, self.plan, safe_return_required=True
        )
        self.assertIsNotNone(safe_step)
        self.assertEqual("finish-safe", safe_step["id"])
        self.assertEqual(
            "locked_safe_return",
            progress["steps"]["finish-safe"]["status"],
        )

        stored = self.controller.storage.get_runtime(
            EXECUTION_PLAN_PROGRESS_RUNTIME_KEY, {}
        )
        self.assertIn(self.goal["id"], stored)


if __name__ == "__main__":
    unittest.main()
