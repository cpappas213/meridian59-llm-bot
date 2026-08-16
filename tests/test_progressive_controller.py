from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from meridian_bot.controller import BotController
from meridian_bot.model import ModelResponseFormatError
from meridian_bot.simulator import SimulatedBroker
from meridian_bot.tactical_protocol import (
    EXECUTE_STEP,
    PLAN_CREATE,
    PLAN_REVISE,
    REPAIR_ACTION,
    REPAIR_PLAN,
)

from .helpers import config, goal_payload
from .test_controller import FixedModel, source_verify_safe_rooms, with_safe_ending


TacticalResponder = Callable[[str, dict[str, Any]], dict[str, Any]]


class ProgressiveModel(FixedModel):
    """Keep the campaign fixture while exposing the progressive completion API."""

    def __init__(self, responder: TacticalResponder) -> None:
        self.responder = responder
        self.tactical_calls: list[tuple[str, dict[str, Any]]] = []

    def tactical_complete(
        self, *, mode: str, envelope: dict[str, Any]
    ) -> dict[str, Any]:
        self.tactical_calls.append((mode, copy.deepcopy(envelope)))
        return self.responder(mode, envelope)


class NoTacticalCallModel(FixedModel):
    def __init__(self) -> None:
        self.tactical_calls: list[tuple[str, dict[str, Any]]] = []

    def tactical_complete(
        self, *, mode: str, envelope: dict[str, Any]
    ) -> dict[str, Any]:
        self.tactical_calls.append((mode, copy.deepcopy(envelope)))
        raise AssertionError(f"fixed controller-bound action called the model in {mode}")


class ProgressiveControllerTests(unittest.TestCase):
    @staticmethod
    def _create_general_phase(
        controller: BotController, goal: dict[str, Any]
    ) -> dict[str, Any]:
        run = controller.storage.ensure_campaign_run(goal)
        return controller.storage.create_campaign_phase(
            run,
            {
                "kind": "general",
                "objective": str(goal["objective"]),
                "success_criteria": list(goal["success_criteria"]),
                "abandon_predicates": [],
                "budget": {"max_actions": 12, "max_minutes": 30},
                "context": {},
                "rationale": "Exercise the progressive tactical router.",
            },
            mode="start",
        )

    @staticmethod
    def _plan_response(
        envelope: dict[str, Any],
        *,
        summary: str = "Drop the requested item, then finish safely.",
        revision_reason: str | None = None,
    ) -> dict[str, Any]:
        candidates = envelope["plan_constraints"]["safe_ending_candidates"]
        return {
            "request_id": envelope["request_id"],
            "summary": summary,
            "steps": [
                {
                    "id": "drop-rusty-sword",
                    "outcome": "Drop the requested rusty sword.",
                    "tool": "act",
                    "verification": "The rusty sword is absent from inventory.",
                }
            ],
            "safe_ending": {
                "candidate_id": candidates[0]["candidate_id"],
                "rationale": "Return to the source-verified safe staging room.",
            },
            "assumptions": [],
            "revision_reason": revision_reason,
        }

    def test_turn_without_plan_routes_plan_create_and_stores_compiled_epilogue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()

                def respond(mode: str, envelope: dict[str, Any]) -> dict[str, Any]:
                    self.assertEqual(PLAN_CREATE, mode)
                    return self._plan_response(envelope)

                model = ProgressiveModel(respond)
                controller.broker = broker
                controller.model = model  # type: ignore[assignment]
                controller.last_observation = broker.observe()
                source_verify_safe_rooms(controller, 100)
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="progressive-create")
                )["goal"]

                result = controller.turn()

                self.assertTrue(result["planned"])
                self.assertEqual([PLAN_CREATE], [mode for mode, _ in model.tactical_calls])
                stored = controller._execution_plan(controller.storage.goal(goal["id"]))
                self.assertIsNotNone(stored)
                assert stored is not None
                self.assertEqual("drop-rusty-sword", stored["steps"][0]["id"])
                safe_step = stored["steps"][-1]
                self.assertEqual("travel", safe_step["tool"])
                self.assertEqual(100, stored["safe_ending"]["room_id"])
                self.assertEqual(safe_step["id"], stored["safe_ending"]["step_id"])
                self.assertTrue(safe_step["id"].startswith("finish-safe-100"))
            finally:
                controller.close()

    def test_invalid_plan_json_gets_one_budgeted_protocol_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()

                def respond(mode: str, envelope: dict[str, Any]) -> dict[str, Any]:
                    if mode == PLAN_CREATE:
                        raise ModelResponseFormatError(
                            "model returned invalid JSON with repair disabled"
                        )
                    self.assertEqual(REPAIR_PLAN, mode)
                    self.assertTrue(envelope["rejected_response_unavailable"])
                    self.assertEqual(
                        "MODEL_RESPONSE_INVALID_JSON",
                        envelope["violations"][0]["code"],
                    )
                    return self._plan_response(envelope)

                model = ProgressiveModel(respond)
                controller.broker = broker
                controller.model = model  # type: ignore[assignment]
                controller.last_observation = broker.observe()
                source_verify_safe_rooms(controller, 100)
                controller.storage.submit_goal(
                    goal_payload(request_id="progressive-create-json-repair")
                )

                result = controller.turn()

                self.assertTrue(result["planned"])
                self.assertEqual(
                    [PLAN_CREATE, REPAIR_PLAN],
                    [mode for mode, _ in model.tactical_calls],
                )
            finally:
                controller.close()

    def test_execute_step_repairs_attempt_to_choose_another_step_or_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                first_option: dict[str, Any] = {}

                def respond(mode: str, envelope: dict[str, Any]) -> dict[str, Any]:
                    options = envelope["legal_actions"]
                    self.assertEqual(1, len(options))
                    option = options[0]
                    if mode == EXECUTE_STEP:
                        first_option.update(copy.deepcopy(option))
                        # The model cannot name a later step/tool. Those fields are
                        # outside the response contract even with a current token.
                        return {
                            "request_id": envelope["request_id"],
                            "action_token": option["action_token"],
                            "arguments": {"verb": "drop", "target": 1},
                            "rationale": "Try to jump to the later travel step.",
                            "expected_observation": {},
                            "tool": "travel",
                            "plan_step_id": "rest-after-work",
                        }
                    self.assertEqual(REPAIR_ACTION, mode)
                    return {
                        "request_id": envelope["request_id"],
                        "action_token": option["action_token"],
                        "arguments": {"verb": "drop", "target": 1},
                        "rationale": "Execute the sole controller-bound first step.",
                        "expected_observation": copy.deepcopy(
                            option["expected_observation"]
                        ),
                    }

                model = ProgressiveModel(respond)
                controller.broker = broker
                controller.model = model  # type: ignore[assignment]
                observation = broker.observe()
                controller.last_observation = observation
                source_verify_safe_rooms(controller, 100)
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="progressive-ordered-action")
                )["goal"]
                plan = controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {
                            "summary": "Drop the sword, rest, and finish safely.",
                            "steps": [
                                {
                                    "id": "drop-rusty-sword",
                                    "outcome": "Drop the requested rusty sword.",
                                    "tool": "act",
                                    "verification": "The sword is absent from inventory.",
                                },
                                {
                                    "id": "rest-after-work",
                                    "outcome": "Rest after completing the work.",
                                    "tool": "rest",
                                    "verification": "The character has rested.",
                                },
                            ],
                            "assumptions": [],
                        },
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )

                decision, context = controller._progressive_action_decision(
                    goal=goal,
                    observation=observation,
                    campaign_phase=None,
                    tools=controller._planner_tools(),
                    planner_feedback=None,
                    execution_plan=plan,
                    safe_return_required=False,
                )

                self.assertEqual([EXECUTE_STEP, REPAIR_ACTION], [mode for mode, _ in model.tactical_calls])
                self.assertEqual("drop-rusty-sword", first_option["step_id"])
                self.assertEqual("act", first_option["tool"])
                self.assertEqual("drop-rusty-sword", decision["plan_step_id"])
                self.assertEqual("act", decision["tool"])
                self.assertEqual({"verb": "drop", "target": 1}, decision["arguments"])
                self.assertTrue(context["repair_attempted"])
                self.assertFalse(context["direct"])
            finally:
                controller.close()

    def test_invalid_action_json_gets_one_budgeted_protocol_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()

                def respond(mode: str, envelope: dict[str, Any]) -> dict[str, Any]:
                    option = envelope["legal_actions"][0]
                    if mode == EXECUTE_STEP:
                        raise ModelResponseFormatError(
                            "model returned invalid JSON with repair disabled"
                        )
                    self.assertEqual(REPAIR_ACTION, mode)
                    self.assertTrue(envelope["rejected_response_unavailable"])
                    self.assertEqual(
                        "MODEL_RESPONSE_INVALID_JSON",
                        envelope["violations"][0]["code"],
                    )
                    return {
                        "request_id": envelope["request_id"],
                        "action_token": option["action_token"],
                        "arguments": {"verb": "drop", "target": 1},
                        "rationale": "Execute the sole repaired action contract.",
                        "expected_observation": copy.deepcopy(
                            option["expected_observation"]
                        ),
                    }

                model = ProgressiveModel(respond)
                controller.broker = broker
                controller.model = model  # type: ignore[assignment]
                observation = broker.observe()
                controller.last_observation = observation
                source_verify_safe_rooms(controller, 100)
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="progressive-action-json-repair")
                )["goal"]
                plan = controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {
                            "summary": "Drop the sword, then finish safely.",
                            "steps": [
                                {
                                    "id": "drop-rusty-sword",
                                    "outcome": "Drop the requested rusty sword.",
                                    "tool": "act",
                                    "verification": "The sword is absent from inventory.",
                                }
                            ],
                            "assumptions": [],
                        },
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )

                decision, context = controller._progressive_action_decision(
                    goal=goal,
                    observation=observation,
                    campaign_phase=None,
                    tools=controller._planner_tools(),
                    planner_feedback=None,
                    execution_plan=plan,
                    safe_return_required=False,
                )

                self.assertEqual(
                    [EXECUTE_STEP, REPAIR_ACTION],
                    [mode for mode, _ in model.tactical_calls],
                )
                self.assertEqual("drop-rusty-sword", decision["plan_step_id"])
                self.assertEqual("act", decision["tool"])
                self.assertTrue(context["repair_attempted"])
                self.assertFalse(context["direct"])
            finally:
                controller.close()

    def test_fixed_travel_partial_retry_and_safe_return_skip_tactical_model(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                model = NoTacticalCallModel()
                controller.broker = broker
                controller.model = model  # type: ignore[assignment]
                observation = broker.observe()
                controller.last_observation = observation
                source_verify_safe_rooms(controller, 100)
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="progressive-direct-travel")
                )["goal"]
                plan = controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {
                            "summary": "Travel to the work room, then finish safely.",
                            "steps": [
                                {
                                    "id": "reach-work-room",
                                    "outcome": "Travel to work room 200.",
                                    "tool": "travel",
                                    "verification": "Current room id is 200.",
                                }
                            ],
                            "assumptions": [],
                        },
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )
                tools = controller._planner_tools()

                fixed, fixed_context = controller._progressive_action_decision(
                    goal=goal,
                    observation=observation,
                    campaign_phase=None,
                    tools=tools,
                    planner_feedback=None,
                    execution_plan=plan,
                    safe_return_required=False,
                )
                self.assertEqual("reach-work-room", fixed["plan_step_id"])
                self.assertEqual({"to": 200}, fixed["arguments"])
                self.assertTrue(fixed_context["direct"])

                controller._record_plan_action(
                    goal,
                    step_id="reach-work-room",
                    tool="travel",
                    arguments={"to": 200},
                    result={"arrived": False, "reason": "hop budget"},
                    status="partial_progress",
                )
                partial_plan = controller._execution_plan(goal)
                assert partial_plan is not None
                partial, partial_context = controller._progressive_action_decision(
                    goal=goal,
                    observation=observation,
                    campaign_phase=None,
                    tools=tools,
                    planner_feedback={"message": "Continue the same destination."},
                    execution_plan=partial_plan,
                    safe_return_required=False,
                )
                self.assertEqual("reach-work-room", partial["plan_step_id"])
                self.assertEqual({"to": 200}, partial["arguments"])
                self.assertTrue(partial_context["direct"])

                safe, safe_context = controller._progressive_action_decision(
                    goal=goal,
                    observation=observation,
                    campaign_phase=None,
                    tools=tools,
                    planner_feedback=None,
                    execution_plan=partial_plan,
                    safe_return_required=True,
                )
                self.assertEqual(plan["safe_ending"]["step_id"], safe["plan_step_id"])
                self.assertEqual("travel", safe["tool"])
                self.assertEqual({"to": 100}, safe["arguments"])
                self.assertTrue(safe_context["direct"])
                self.assertEqual([], model.tactical_calls)
            finally:
                controller.close()

    def test_typed_failure_routes_plan_revise_and_controller_injects_authorization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()

                def respond(mode: str, envelope: dict[str, Any]) -> dict[str, Any]:
                    self.assertEqual(PLAN_REVISE, mode)
                    self.assertEqual(
                        envelope["revision_evidence"]["id"], expected_authorization["id"]
                    )
                    # The narrow model response deliberately has no authorization
                    # field. The controller-owned compiler must add it.
                    return self._plan_response(
                        envelope,
                        summary="Revise from the typed failure, then finish safely.",
                        revision_reason="The verified tool failure changed the next tactic.",
                    )

                model = ProgressiveModel(respond)
                controller.broker = broker
                controller.model = model  # type: ignore[assignment]
                controller.last_observation = broker.observe()
                source_verify_safe_rooms(controller, 100)
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="progressive-revision")
                )["goal"]
                self._create_general_phase(controller, goal)
                existing = controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {
                            "summary": "Use the original tactic, then finish safely.",
                            "steps": [
                                {
                                    "id": "drop-originally",
                                    "outcome": "Drop the requested rusty sword.",
                                    "tool": "act",
                                    "verification": "The sword is absent from inventory.",
                                }
                            ],
                            "assumptions": [],
                        },
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )
                controller._set_planner_feedback(
                    goal,
                    "The active tactic failed with typed controller evidence.",
                    failure_context={
                        "kind": "tool_error",
                        "code": "TARGET_STATE_CHANGED",
                        "tool": "act",
                    },
                )
                expected_authorization = controller._plan_revision_authorization(
                    goal, existing, controller._planner_feedback(goal)
                )
                self.assertIsNotNone(expected_authorization)
                assert expected_authorization is not None
                compiled_inputs: list[dict[str, Any]] = []
                original_store = controller._store_execution_plan

                def store_spy(
                    stored_goal: dict[str, Any],
                    raw_plan: Any,
                    *,
                    grounding: dict[str, Any],
                    revision: bool,
                ) -> dict[str, Any]:
                    compiled_inputs.append(copy.deepcopy(raw_plan))
                    return original_store(
                        stored_goal,
                        raw_plan,
                        grounding=grounding,
                        revision=revision,
                    )

                controller._store_execution_plan = store_spy  # type: ignore[method-assign]

                result = controller.turn()

                self.assertTrue(result["planned"])
                self.assertEqual([PLAN_REVISE], [mode for mode, _ in model.tactical_calls])
                self.assertEqual(
                    expected_authorization["id"],
                    compiled_inputs[-1]["revision_authorization_id"],
                )
                self.assertEqual(
                    "The verified tool failure changed the next tactic.",
                    compiled_inputs[-1]["revision_reason"],
                )
                self.assertEqual(
                    1,
                    len(
                        controller.storage.events(kinds=["planner.plan.revised"])[
                            "events"
                        ]
                    ),
                )
            finally:
                controller.close()


if __name__ == "__main__":
    unittest.main()
