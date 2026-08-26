from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from meridian_bot.controller import BotController
from meridian_bot.model import (
    ModelError,
    ModelResponseFormatError,
    TACTICAL_PLAN_PROMPT_TOKEN_BUDGET,
)
from meridian_bot.simulator import SimulatedBroker
from meridian_bot.tactical_protocol import (
    EXECUTE_STEP,
    PLAN_CREATE,
    PLAN_REVISE,
    REPAIR_ACTION,
    REPAIR_PLAN,
    compile_plan_response,
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
    def _oversized_sale_catalogue() -> dict[str, Any]:
        buyers = []
        for item_index in range(8):
            candidates = []
            for merchant_index in range(5):
                candidates.append(
                    {
                        "merchant": f"MerchantClass{merchant_index}",
                        "room_ids": [100 + merchant_index, 200 + merchant_index],
                        "buys_anything": merchant_index == 0,
                        "matched_category": "Reagent",
                        "buying_categories": ["Reagent", "Weapon", "Wearable"],
                        "verification": (
                            "Source categories narrow candidates; only a fresh live "
                            "quote proves acceptance. "
                            + ("V" * 240)
                        ),
                        "entity_id": f"merchant:fixture:{merchant_index}",
                        "instances": [
                            {
                                "seller_id_at_build": 9000 + merchant_index,
                                "name": f"Catalogue merchant {merchant_index}",
                                "room_id": 100 + merchant_index,
                            },
                            {
                                "seller_id_at_build": 9100 + merchant_index,
                                "name": f"Second catalogue merchant {merchant_index}",
                                "room_id": 200 + merchant_index,
                            },
                        ],
                    }
                )
            buyers.append(
                {
                    "item": f"carried sale item {item_index}",
                    "item_kind": "reagent",
                    "inferred_source_category": "Reagent",
                    "candidates": candidates,
                    "next_evidence": (
                        "Use merchants with buys=<exact carried item> and then sell "
                        "confirm=false. Only the live quote proves acceptance."
                    ),
                }
            )
        return {
            "carried_shillings": 0,
            "source_estimated_inventory_value": 1275,
            "source_estimated_liquidatable_inventory_value": 975,
            "confirmed_live_quote_liquidatable_value": 0,
            "known_inventory_item_value": 1275,
            "known_liquidatable_inventory_value": 975,
            "known_total_carried_value": 1275,
            "valuation_complete": True,
            "valued_items": [
                {
                    "id": 7000 + index,
                    "name": f"carried sale item {index}",
                    "quantity": 1,
                    "unit_value": 100 + index,
                    "subtotal": 100 + index,
                    "basis": "source-derived item value " + ("B" * 180),
                    "source_ref": f"item-source-{index}",
                    "npc_transferable": True,
                    "sale_protected": False,
                    "sale_evidence_exhausted": False,
                    "liquidatable": True,
                }
                for index in range(9)
            ],
            "unknown_value_items": [],
            "unquoted_liquidatable_items": [
                {"id": 7000 + index, "name": f"carried sale item {index}", "quantity": 1}
                for index in range(8)
            ],
            "valid_live_sell_quotes": [],
            "liquidation_status": {
                "state": "quote_required",
                "interpretation": "Live quotes are required before a sale.",
            },
            "npc_transfer_restricted_items": [],
            "protected_sale_items": [
                {"id": 7999, "name": "short sword", "reason": "equipped"}
            ],
            "npc_transfer_rules": [
                {"source": "Create Weapon", "rule": "Created weapons are equipment only."}
            ],
            "bank_accounts": [
                {"account": "shared mainland", "last_known_balance": 0}
            ],
            "buyer_candidates": buyers,
            "rejected_buyer_candidates": [],
            "merchant_sale_refusals": [],
            "sale_exhausted_items": [],
            "valuation_note": "N" * 740,
            "banking_policy": {
                "mode": "planner_discretion",
                "never_blocks_travel_or_combat": True,
            },
        }

    @staticmethod
    def _large_tactical_observation() -> dict[str, Any]:
        return {
            "id": "observation-budget-regression",
            "observed_at": 1_700_000_000.0,
            "look": {
                "room": {"num": 562, "name": "The sandy shores"},
                "self": {"id": 1, "name": "MANIAC"},
                "objects": [
                    {
                        "id": 1000 + index,
                        "name": f"nearby object {index}",
                        "description": "O" * 180,
                    }
                    for index in range(20)
                ],
                "exits": [
                    {
                        "to": 600 + index,
                        "name": f"route {index}",
                        "description": "E" * 120,
                    }
                    for index in range(20)
                ],
            },
            "status": {
                "vitals": {
                    "health": {"current": 40, "max": 40},
                    "mana": {"current": 18, "max": 18},
                    "vigor": {"current": 192, "max": 200},
                }
            },
            "inventory": {
                "items": [
                    {
                        "id": 7000 + index,
                        "name": f"carried sale item {index}",
                        "description": "I" * 150,
                    }
                    for index in range(20)
                ]
            },
            "equipment": {"equipped": [{"id": 7999, "name": "short sword"}]},
            "abilities": [
                {
                    "name": f"ability {index}",
                    "level": index,
                    "description": "A" * 120,
                }
                for index in range(20)
            ],
        }

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

    def test_overbudget_retry_circuit_is_durable_until_material_state_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = BotController(config(root))
            try:
                broker = SimulatedBroker()

                def overbudget(
                    _mode: str, _envelope: dict[str, Any]
                ) -> dict[str, Any]:
                    raise ModelError(
                        "tactical plan_create required context exceeds its "
                        "6000-token prompt budget"
                    )

                model = ProgressiveModel(overbudget)
                first.broker = broker
                first.model = model  # type: ignore[assignment]
                first.last_observation = broker.observe()
                source_verify_safe_rooms(first, 100)
                first.storage.submit_goal(
                    goal_payload(request_id="durable-overbudget-circuit")
                )

                opened = first.turn()
                suppressed = first.turn()

                self.assertTrue(opened["planner_failure_circuit_opened"])
                self.assertEqual("overbudget", opened["failure_kind"])
                self.assertTrue(suppressed["planner_retry_suppressed"])
                self.assertEqual(1, len(model.tactical_calls))
            finally:
                first.close()

            reopened = BotController(config(root))
            try:
                broker = SimulatedBroker()
                model = ProgressiveModel(overbudget)
                reopened.broker = broker
                reopened.model = model  # type: ignore[assignment]
                reopened.last_observation = broker.observe()
                source_verify_safe_rooms(reopened, 100)

                still_suppressed = reopened.turn()
                self.assertTrue(still_suppressed["planner_retry_suppressed"])
                self.assertEqual([], model.tactical_calls)

                broker.inventory_items.append(
                    {"id": 2, "name": "bread", "amount": 1, "can": ["use"]}
                )
                retried = reopened.turn()

                self.assertTrue(retried["planner_failure_circuit_opened"])
                self.assertEqual(1, len(model.tactical_calls))
                self.assertEqual(
                    1,
                    len(
                        reopened.storage.events(
                            kinds=["planner.retry_circuit.reopened"]
                        )["events"]
                    ),
                )
            finally:
                reopened.close()

    def test_protocol_rejection_is_not_retried_against_identical_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()

                def malformed(
                    _mode: str, envelope: dict[str, Any]
                ) -> dict[str, Any]:
                    return {"request_id": envelope["request_id"]}

                model = ProgressiveModel(malformed)
                controller.broker = broker
                controller.model = model  # type: ignore[assignment]
                controller.last_observation = broker.observe()
                source_verify_safe_rooms(controller, 100)
                controller.storage.submit_goal(
                    goal_payload(request_id="protocol-retry-circuit")
                )

                rejected = controller.turn()
                call_count = len(model.tactical_calls)
                suppressed = controller.turn()

                self.assertTrue(rejected["tactical_protocol_rejected"])
                self.assertTrue(rejected["planner_failure_circuit_opened"])
                self.assertGreaterEqual(call_count, 1)
                self.assertTrue(suppressed["planner_retry_suppressed"])
                self.assertEqual(call_count, len(model.tactical_calls))
            finally:
                controller.close()

    def test_plan_create_projects_non_actionable_sale_catalogue_before_budgeting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="progressive-financial-budget")
                )["goal"]
                observation = self._large_tactical_observation()
                phase = {
                    "id": "phase-financial-budget",
                    "kind": "farm",
                    "objective": "Farm the assigned prey without selling inventory.",
                    "success_criteria": copy.deepcopy(goal["success_criteria"]),
                    "abandon_predicates": [],
                    "budget": {"max_actions": 100, "max_minutes": 180},
                    "context": {
                        "hunt": "fungus beast",
                        "assigned_room": 562,
                        "use_safe_spots": False,
                    },
                }
                tool_names = [
                    "abilities",
                    "autopilot",
                    "bank",
                    "cast",
                    "equip_best",
                    "equipment",
                    "hunting_grounds",
                    "inventory",
                    "map",
                    "merchants",
                    "prey",
                    "rest_up",
                    "safe_spots",
                    "shop",
                    "travel",
                    "wear_best",
                    "knowledge_search",
                ]
                tools = [
                    {
                        "name": name,
                        "description": f"Exact semantics for {name}. " + ("T" * 470),
                        "input_schema": {"type": "object", "properties": {}},
                    }
                    for name in tool_names
                ]
                financial = self._oversized_sale_catalogue()
                grounded = {
                    "safe_ending_candidates": {
                        "status": "verified",
                        "candidates": [
                            {
                                "room_id": 100 + index,
                                "name": f"Verified safe room {index}",
                                "flags": ["ROOM_NO_COMBAT"],
                                "distance": index,
                                "basis": "source_connection_graph",
                            }
                            for index in range(8)
                        ],
                    },
                    "direct_phase_capabilities": {
                        "status": "ready",
                        "details": "G" * 2200,
                    },
                }

                def complete(messages: list[dict[str, Any]], *_: Any, **__: Any) -> dict[str, Any]:
                    envelope = json.loads(messages[1]["content"])
                    return {
                        "request_id": envelope["request_id"],
                        "summary": "Launch the bounded farm keeper.",
                        "steps": [
                            {
                                "id": "inspect-inventory",
                                "outcome": "Inspect current inventory.",
                                "tool": "inventory",
                                "verification": "Current inventory is observed.",
                            }
                        ],
                        "safe_ending": {
                            "candidate_id": envelope["plan_constraints"]
                            ["safe_ending_candidates"][0]["candidate_id"],
                            "rationale": "Finish at the verified safe room.",
                        },
                        "assumptions": [],
                        "revision_reason": None,
                    }

                with patch.object(controller.model, "_complete", side_effect=complete) as model_call:
                    _, context = controller._progressive_plan_decision(
                        mode=PLAN_CREATE,
                        goal=goal,
                        observation=observation,
                        completion={"criteria": [{"id": "item-gone", "met": False}]},
                        campaign_phase=phase,
                        tools=tools,
                        planner_feedback=None,
                        grounded_context=grounded,
                        learned_failures={},
                        financial_context=financial,
                        policy_summary={
                            "avoid_death": True,
                            "guidance": "P" * 380,
                        },
                        execution_plan=None,
                        revision_authorization=None,
                    )

                model_call.assert_called_once()
                metrics = controller.model.last_tactical_prompt_metrics
                self.assertIsNotNone(metrics)
                assert metrics is not None
                self.assertLessEqual(
                    metrics["estimated_tokens"], TACTICAL_PLAN_PROMPT_TOKEN_BUDGET
                )
                self.assertFalse(metrics["over_budget"])
                envelope = context["base_envelope"]
                projected_financial = envelope["relevant_facts"]["financial"]
                self.assertNotIn("buyer_candidates", projected_financial)
                self.assertEqual(
                    financial["bank_accounts"], projected_financial["bank_accounts"]
                )
                self.assertEqual(
                    financial["protected_sale_items"],
                    projected_financial["protected_sale_items"],
                )
                self.assertEqual(tool_names, envelope["plan_constraints"]["allowed_tools"])
                self.assertEqual(goal["id"], envelope["goal_contract"]["id"])
                self.assertEqual(phase["id"], envelope["phase_contract"]["id"])
                self.assertEqual(
                    "safe:100",
                    envelope["plan_constraints"]["safe_ending_candidates"][0][
                        "candidate_id"
                    ],
                )

                unprojected = copy.deepcopy(envelope)
                unprojected["relevant_facts"]["financial"] = (
                    controller._compact_tactical_value(
                        {"financial": financial},
                        max_list=20,
                        max_dict=32,
                        max_string=800,
                    )["financial"]
                )
                with self.assertRaisesRegex(ModelError, "required context exceeds"):
                    controller.model._budget_tactical_envelope(
                        PLAN_CREATE, unprojected
                    )
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
                    completion=controller.criteria.evaluate(goal, observation),
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
                    completion=controller.criteria.evaluate(goal, observation),
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
                    completion=controller.criteria.evaluate(goal, observation),
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
                    completion=controller.criteria.evaluate(goal, observation),
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
                    completion=controller.criteria.evaluate(goal, observation),
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

    def test_fixed_farm_launch_skips_tactical_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                model = NoTacticalCallModel()
                controller.broker = broker
                controller.model = model  # type: ignore[assignment]
                broker.vitals["health"] = {"current": 99, "max": 100}
                observation = broker.observe()
                observation["status"]["vitals"]["vigor"] = {
                    "value": 80,
                    "scale_max": 200,
                    "rested": True,
                }
                observation["equipment"] = {
                    "known": True,
                    "equipped": [{"id": 1, "name": "Rusty sword"}],
                }
                controller.last_observation = observation
                source_verify_safe_rooms(controller, 100)
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="progressive-direct-farm-launch",
                        title="Raise maximum health",
                        objective="Raise maximum health by farming fungus beasts.",
                        success_criteria=[
                            {
                                "id": "max-hp-101",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 101,
                            }
                        ],
                        constraints={
                            "bank_before_hazard": False,
                            "operator_notes": (
                                "hunt=fungus beast; assigned_room=562; "
                                "use_safe_spots=false; flee_below=0.425; "
                                "fight_above_vigor=80; break_out_via_logoff=false"
                            ),
                        },
                    )
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                phase = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "farm",
                        "objective": "Farm fungus beasts in room 562.",
                        "success_criteria": copy.deepcopy(goal["success_criteria"]),
                        "abandon_predicates": [],
                        "budget": {"max_actions": 100, "max_minutes": 90},
                        "context": {
                            "room": 562,
                            "target": "fungus beast",
                            "use_safe_spots": False,
                            "flee_below": 17 / 40,
                            "fight_above_vigor": 80,
                        },
                    },
                    mode="start",
                )
                plan = controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {
                            "summary": "Launch the bounded fungus farm, then finish safely.",
                            "steps": [
                                {
                                    "id": "launch-goal-keeper",
                                    "outcome": (
                                        "Autopilot keeper launched to farm fungus beasts "
                                        "in room 562 until max health reaches 101."
                                    ),
                                    "tool": "autopilot",
                                    "verification": (
                                        "Keeper reports the requested goal-owned fungus farm."
                                    ),
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
                    completion=controller.criteria.evaluate(goal, observation),
                    campaign_phase=phase,
                    tools=controller._planner_tools(phase),
                    planner_feedback=None,
                    execution_plan=plan,
                    safe_return_required=False,
                )

                self.assertEqual("launch-goal-keeper", decision["plan_step_id"])
                self.assertEqual("autopilot", decision["tool"])
                self.assertEqual("start", decision["arguments"]["action"])
                self.assertEqual("farm", decision["arguments"]["mode"])
                self.assertEqual("fungus beast", decision["arguments"]["hunt"])
                self.assertEqual(562, decision["arguments"]["assigned_room"])
                self.assertFalse(decision["arguments"]["use_safe_spots"])
                self.assertTrue(context["direct"])
                self.assertEqual(
                    {}, context["options"][0]["free_argument_schema"]["properties"]
                )
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

    def test_tactical_phase_contract_preserves_criteria_and_typed_context(self) -> None:
        success_criteria = [
            {
                "id": "hp-gain",
                "kind": "numeric_delta",
                "metric": "status.vitals.health.max",
                "operator": ">=",
                "value": 2,
                "baseline": 20,
            },
            {
                "id": "has-bread",
                "kind": "inventory_contains",
                "item": "bread",
                "count": 4,
            },
            {
                "id": "two-weapons",
                "kind": "equipment_count",
                "category": "weapon",
                "count": 2,
            },
            {
                "id": "at-inn",
                "kind": "location_reached",
                "location": "Tos Inn",
                "room_id": 52,
            },
            {
                "id": "spoke",
                "kind": "event_occurred",
                "event_kind": "conversation.responded",
                "after_cursor": 177,
            },
            {
                "id": "all-done",
                "kind": "composite_all",
                "criterion_ids": ["has-bread", "at-inn"],
            },
            *[
                {
                    "id": f"checkpoint-{index}",
                    "kind": "location_reached",
                    "room_id": 100 + index,
                }
                for index in range(8)
            ],
        ]
        abandon_predicates = [
            {
                "id": f"low-health-{index}",
                "kind": "numeric_threshold",
                "metric": "status.vitals.health.current",
                "operator": "<",
                "value": index + 1,
            }
            for index in range(13)
        ]
        huge = "source material that must not reach the tactical prompt " * 200
        phase = {
            "id": "phase-projection",
            "kind": "farm",
            "objective": "Farm safely and return to Tos Inn.",
            "success_criteria": success_criteria,
            "abandon_predicates": abandon_predicates,
            "budget": {"max_actions": 40, "max_minutes": 90},
            "source_ref": huge,
            "context": {
                "target": "giant rat",
                "room": 535,
                "use_safe_spots": True,
                "flee_below": 0.7,
                "fight_above_vigor": 80,
                "keep_candidates": ["rat tail", "healing herb"],
                "constraints": {"maximum_price": 200, "allow_pvp": False},
                "farm_recipe": {
                    "target": "giant rat",
                    "room": 535,
                    "buy_food": True,
                    "source_ref": huge,
                },
                "route": {
                    "from": 52,
                    "via": [
                        {
                            "to": 535,
                            "kind": "go",
                            "name": "Rat Warrens",
                            "source_ref": huge,
                        }
                    ],
                    "evidence": huge,
                },
                "source_ref": huge,
                "source_evidence": {"raw": huge},
                "ignored_unverified_tactic_preferences": [{"raw": huge}],
                "ignored_invalid_abandon_predicates": [{"raw": huge}],
                "phase_targets": [{"raw": huge}],
                "retry_state_baseline": {"raw": huge},
            },
        }
        original = copy.deepcopy(phase)

        contract = BotController._tactical_phase_contract(phase)

        self.assertIsNotNone(contract)
        assert contract is not None
        self.assertEqual(success_criteria, contract["success_criteria"])
        self.assertEqual(abandon_predicates, contract["abandon_predicates"])
        self.assertIsNot(success_criteria, contract["success_criteria"])
        self.assertIsNot(abandon_predicates, contract["abandon_predicates"])
        self.assertEqual(
            {
                "target": "giant rat",
                "room": 535,
                "use_safe_spots": True,
                "flee_below": 0.7,
                "fight_above_vigor": 80,
                "keep_candidates": ["rat tail", "healing herb"],
                "constraints": {"maximum_price": 200, "allow_pvp": False},
                "farm_recipe": {
                    "target": "giant rat",
                    "room": 535,
                    "buy_food": True,
                },
                "route": {
                    "from": 52,
                    "via": [
                        {"to": 535, "kind": "go", "name": "Rat Warrens"}
                    ],
                },
            },
            contract["context"],
        )
        self.assertNotIn("source_ref", contract)
        self.assertEqual(original, phase)

    def test_safe_ending_projection_keeps_evidence_only_in_compiler_map(self) -> None:
        safe_context = {
            "status": "found",
            "candidates": [
                {
                    "room_id": 100,
                    "name": "First Refuge",
                    "region": "Tos",
                    "flags": ["safe", "inn"],
                    "distance": 1,
                    "basis": "verified map",
                    "source_ref": "map:room:100",
                    "evidence": {"source_ref": "world.json:100", "safe": True},
                },
                {
                    "room_id": 106,
                    "name": "Second Sanctuary",
                    "region": "Barloque",
                    "flags": ["safe", "sanctuary"],
                    "distance": 3,
                    "basis": "verified map",
                    "source_ref": "map:room:106",
                    "evidence": {"source_ref": "world.json:106", "safe": True},
                },
            ],
        }
        original = copy.deepcopy(safe_context)

        prompt_candidates, candidate_map = (
            BotController._tactical_safe_ending_candidates(safe_context)
        )

        self.assertEqual(["safe:100", "safe:106"], list(candidate_map))
        for candidate in prompt_candidates:
            self.assertNotIn("source_ref", candidate)
            self.assertNotIn("evidence", candidate)
        self.assertEqual("map:room:100", candidate_map["safe:100"]["source_ref"])
        self.assertEqual(
            {"source_ref": "world.json:106", "safe": True},
            candidate_map["safe:106"]["evidence"],
        )
        self.assertEqual(original, safe_context)

        compiled = compile_plan_response(
            {
                "request_id": "select-second-safe-room",
                "summary": "Observe current state, then return safely.",
                "steps": [
                    {
                        "id": "observe",
                        "outcome": "Observe current state.",
                        "tool": "look",
                        "verification": "Fresh room state is observed.",
                    }
                ],
                "safe_ending": {
                    "candidate_id": "safe:106",
                    "rationale": "The second verified sanctuary is preferred.",
                },
                "assumptions": [],
                "revision_reason": None,
            },
            candidate_map,
            request_id="select-second-safe-room",
        )

        self.assertEqual(106, compiled["safe_ending"]["room_id"])
        self.assertEqual(compiled["steps"][-1]["id"], compiled["safe_ending"]["step_id"])
        self.assertEqual("travel", compiled["steps"][-1]["tool"])
        self.assertEqual(
            "Travel to source-verified safe room 106 (Second Sanctuary).",
            compiled["steps"][-1]["outcome"],
        )


if __name__ == "__main__":
    unittest.main()
