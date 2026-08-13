from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from meridian_bot.campaign import CampaignCoordinator
from meridian_bot.criteria import CriteriaEvaluator
from meridian_bot.storage import IdempotencyConflict, InvalidTransition, Storage

from .helpers import goal_payload


class StorageTests(unittest.TestCase):
    def test_compact_campaign_attempt_preserves_verified_failure_reason(self) -> None:
        compact = CampaignCoordinator._compact_attempt(
            {
                "id": "attempt-1",
                "semantic_action": "sell",
                "status": "failed",
                "result": {"messages": ["merchant dialogue omitted from compact context"]},
                "verification": {
                    "no_progress": True,
                    "reason": 'Pritchett tells you, "Whyfore dost you offer me that?"',
                },
            }
        )

        self.assertEqual(
            {
                "no_progress": True,
                "reason": 'Pritchett tells you, "Whyfore dost you offer me that?"',
            },
            (compact or {}).get("verification"),
        )

    def test_inventory_not_full_uses_known_broker_capacity_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                evaluator = CriteriaEvaluator(storage)
                goal = {
                    "id": "inventory-capacity-compatibility",
                    "success_criteria": [
                        {
                            "id": "capacity",
                            "kind": "state_equals",
                            "path": "inventory.full",
                            "value": False,
                        }
                    ],
                }

                available = evaluator.evaluate(
                    goal,
                    {
                        "inventory": {
                            "carry": {
                                "known": True,
                                "room_for": {"weight": 2566, "bulk": 2414},
                            }
                        }
                    },
                )
                full = evaluator.evaluate(
                    goal,
                    {
                        "inventory": {
                            "carry": {
                                "known": True,
                                "room_for": {"weight": 0, "bulk": 2414},
                            }
                        }
                    },
                )
                unknown = evaluator.evaluate(
                    goal,
                    {
                        "inventory": {
                            "carry": {
                                "known": False,
                                "room_for": {"weight": 2566, "bulk": 2414},
                            }
                        }
                    },
                )

                self.assertTrue(available["all_met"])
                self.assertFalse(full["all_met"])
                self.assertFalse(unknown["all_met"])

    def test_higher_priority_preemption_requeues_and_automatically_resumes_goal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                interrupted = storage.submit_goal(
                    goal_payload(
                        "priority-boundary-active",
                        title="Long progression goal",
                        priority=40,
                    )
                )["goal"]
                run = storage.ensure_campaign_run(interrupted)
                higher = storage.submit_goal(
                    goal_payload(
                        "priority-boundary-higher",
                        title="Learn Mace Fighting",
                        priority=50,
                    )
                )["goal"]
                equal = storage.submit_goal(
                    goal_payload(
                        "priority-boundary-equal",
                        title="Equal-priority queued work",
                        priority=40,
                    )
                )["goal"]

                result = storage.preempt_for_higher_priority(
                    interrupted["id"],
                    reason="completed a bounded phase in safe room 202",
                    phase_id="phase-33-hp",
                )

                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(higher["id"], result["activated_goal"]["id"])
                self.assertEqual("active", storage.goal(higher["id"])["status"])
                self.assertEqual("queued", storage.goal(interrupted["id"])["status"])
                self.assertEqual("queued", storage.goal(equal["id"])["status"])
                self.assertEqual(run["id"], storage.campaign_run(interrupted["id"])["id"])

                storage.set_goal_completion(
                    higher["id"],
                    {
                        "all_met": True,
                        "percent_estimate": 100,
                        "summary": "verified",
                        "criteria": [],
                        "evidence_event_ids": [],
                    },
                    terminal="succeeded",
                    reason="test goal completed",
                )

                self.assertEqual(
                    interrupted["id"], storage.active_goal()["id"]
                )
                self.assertEqual(
                    run["id"], storage.campaign_run(interrupted["id"])["id"]
                )
                event = storage.goal_events(
                    interrupted["id"], kinds=["goal.priority_preempted"], limit=1
                )[0]
                self.assertEqual(higher["id"], event["data"]["activated_goal_id"])
                self.assertEqual("phase-33-hp", event["data"]["phase_id"])

    def test_equal_or_lower_priority_does_not_preempt_active_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                active = storage.submit_goal(
                    goal_payload("priority-no-preempt-active", priority=50)
                )["goal"]
                storage.submit_goal(
                    goal_payload("priority-no-preempt-equal", priority=50)
                )
                storage.submit_goal(
                    goal_payload("priority-no-preempt-lower", priority=40)
                )

                result = storage.preempt_for_higher_priority(
                    active["id"], reason="safe boundary"
                )

                self.assertIsNone(result)
                self.assertEqual(active["id"], storage.active_goal()["id"])

    def test_blocking_strategic_goal_closes_active_campaign_and_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(goal_payload())["goal"]
                run = storage.ensure_campaign_run(goal)
                phase = storage.create_campaign_phase(
                    run,
                    {
                        "kind": "free_inventory_capacity",
                        "objective": "Create carried inventory capacity.",
                        "success_criteria": [
                            {
                                "id": "capacity",
                                "kind": "state_equals",
                                "path": "inventory.full",
                                "value": False,
                            }
                        ],
                    },
                    mode="start",
                )

                blocked = storage.block_goal(
                    goal["id"],
                    reason="verified external prerequisite is unavailable",
                    blocked_reason="prerequisite_not_met",
                )

                self.assertEqual("blocked", blocked["status"])
                self.assertIsNone(storage.campaign_run(goal["id"]))
                terminal = storage.campaign_run(goal["id"], include_terminal=True)
                self.assertEqual("blocked", terminal["status"])
                self.assertIsNone(terminal["active_phase_id"])
                self.assertEqual(
                    "superseded",
                    next(
                        item["status"]
                        for item in storage.campaign_phases(run["id"])
                        if item["id"] == phase["id"]
                    ),
                )

    def test_resuming_legacy_blocked_goal_closes_stale_campaign_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(goal_payload())["goal"]
                run = storage.ensure_campaign_run(goal)
                phase = storage.create_campaign_phase(
                    run,
                    {
                        "kind": "free_inventory_capacity",
                        "objective": "Create carried inventory capacity.",
                        "success_criteria": [
                            {
                                "kind": "state_equals",
                                "path": "inventory.full",
                                "value": False,
                            }
                        ],
                    },
                    mode="start",
                )
                # Reproduce the pre-fix mismatch without calling block_goal,
                # which now repairs the campaign atomically.
                with storage.transaction() as connection:
                    row = connection.execute(
                        "SELECT * FROM goals WHERE id=?", (goal["id"],)
                    ).fetchone()
                    storage._transition_in_tx(
                        connection,
                        row,
                        "blocked",
                        "legacy failure budget",
                        "controller",
                        blocked_reason="prerequisite_not_met",
                    )

                storage.manage_goal(
                    {
                        "request_id": "resume-legacy-stale-campaign",
                        "goal_id": goal["id"],
                        "action": "resume",
                        "reason": "retry after controller repair",
                    }
                )

                self.assertEqual("active", storage.goal(goal["id"])["status"])
                self.assertIsNone(storage.campaign_run(goal["id"]))
                self.assertEqual(
                    "blocked",
                    storage.campaign_run(goal["id"], include_terminal=True)["status"],
                )
                self.assertEqual(
                    "superseded",
                    next(
                        item["status"]
                        for item in storage.campaign_phases(run["id"])
                        if item["id"] == phase["id"]
                    ),
                )

    def test_campaign_phase_stack_survives_restart_and_resumes_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "bot.sqlite3"
            with Storage(database) as storage:
                goal = storage.submit_goal(goal_payload())["goal"]
                run = storage.ensure_campaign_run(goal)
                parent = storage.create_campaign_phase(
                    run,
                    {
                        "kind": "farm",
                        "objective": "Raise max HP to the next local milestone.",
                        "success_criteria": [
                            {
                                "id": "hp-101",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "value": 101,
                            }
                        ],
                    },
                    mode="start",
                )
                child = storage.create_campaign_phase(
                    run,
                    {
                        "kind": "free_inventory_capacity",
                        "objective": "Create carried inventory capacity.",
                        "success_criteria": [
                            {
                                "id": "capacity",
                                "kind": "state_equals",
                                "path": "inventory.full",
                                "value": False,
                            }
                        ],
                    },
                    mode="push",
                )
                self.assertEqual("paused", storage.campaign_phases(run["id"])[0]["status"])
                storage.transition_campaign_phase(
                    child["id"], "succeeded", reason="capacity verified", resume_parent=True
                )
                self.assertEqual(parent["id"], storage.active_campaign_phase(run["id"])["id"])

            with Storage(database) as reopened:
                persisted = reopened.campaign_run(goal["id"])
                self.assertIsNotNone(persisted)
                self.assertEqual(parent["id"], reopened.active_campaign_phase(persisted["id"])["id"])

    def test_campaign_breaker_fails_only_phase_and_preserves_strategic_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(goal_payload())["goal"]
                coordinator = CampaignCoordinator(storage, CriteriaEvaluator(storage))
                run = storage.ensure_campaign_run(goal)
                phase = storage.create_campaign_phase(
                    run,
                    {
                        "kind": "liquidate_inventory",
                        "objective": "Sell ordinary loot to create capacity.",
                        "success_criteria": [
                            {
                                "id": "empty",
                                "kind": "state_equals",
                                "path": "inventory.items",
                                "value": [],
                            }
                        ],
                    },
                    mode="start",
                )
                observation = {
                    "look": {"room": {"num": 52}},
                    "inventory": {"items": [{"id": 7, "name": "mushroom"}]},
                }
                for attempt_number in (1, 2):
                    attempt_id, tripped_signature = coordinator.prepare_attempt(
                        phase,
                        tool="sell",
                        arguments={"seller": 9, "item": 7},
                        observation=observation,
                        expected_effect={"inventory": "reduced"},
                    )
                    self.assertIsNone(tripped_signature)
                    result = coordinator.finish_attempt(
                        goal,
                        run,
                        phase,
                        attempt_id,
                        status="failed",
                        reason="merchant refused the item",
                    )
                    self.assertEqual(attempt_number == 2, result["breaker_tripped"])

                self.assertEqual("active", storage.goal(goal["id"])["status"])
                self.assertIsNone(storage.active_campaign_phase(run["id"]))
                self.assertEqual("failed", storage.campaign_phases(run["id"])[0]["status"])

    def test_campaign_breaker_signature_ignores_cache_freshness_noise(self) -> None:
        phase = {"kind": "prepare_combat"}
        first = {
            "look": {"room": {"num": 52}},
            "inventory": {"items": [{"id": 7, "name": "mace"}]},
            "equipment": {
                "known": True,
                "wielding": None,
                "equipped": [{"id": 8, "name": "leather armor"}],
                "fresh_ms": 100,
                "changed_ms": 200,
                "source": "server list",
            },
        }
        second = {
            **first,
            "equipment": {
                **first["equipment"],
                "fresh_ms": 12_000,
                "changed_ms": 12_100,
                "source": "refreshed server list",
            },
        }
        coordinator = CampaignCoordinator.__new__(CampaignCoordinator)
        first_signature = coordinator.action_signature(
            phase, "equip_best", {}, first, {"equipment.wielding": "mace"}
        )
        second_signature = coordinator.action_signature(
            phase, "equip_best", {}, second, {"equipment.wielding": "mace"}
        )
        self.assertEqual(first_signature, second_signature)

    def test_prepare_combat_phase_can_drop_broken_gear_with_act(self) -> None:
        selected = CampaignCoordinator.tools_for_phase(
            {"kind": "prepare_combat"},
            [
                {"name": "inventory"},
                {"name": "act"},
                {"name": "equip_best"},
                {"name": "merchants"},
                {"name": "shop"},
                {"name": "sell"},
                {"name": "sell_all"},
                {"name": "fight"},
            ],
        )

        self.assertEqual(
            {
                "inventory",
                "act",
                "equip_best",
                "merchants",
                "shop",
                "sell",
                "sell_all",
            },
            {tool["name"] for tool in selected},
        )

    def test_research_progression_phase_can_move_to_collect_local_evidence(self) -> None:
        selected = CampaignCoordinator.tools_for_phase(
            {"kind": "research_progression"},
            [
                {"name": "look"},
                {"name": "map"},
                {"name": "travel"},
                {"name": "hunting_grounds"},
                {"name": "fight"},
                {"name": "autopilot"},
            ],
        )

        self.assertEqual(
            {"look", "map", "travel", "hunting_grounds"},
            {tool["name"] for tool in selected},
        )

    def test_max_health_research_compiles_only_executable_recipe_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(
                    goal_payload(
                        request_id="typed-hp-research-adapter",
                        objective="Raise maximum HP to at least 100.",
                        success_criteria=[
                            {
                                "id": "max-hp-100",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 100,
                            }
                        ],
                    )
                )["goal"]
                coordinator = CampaignCoordinator(
                    storage, CriteriaEvaluator(storage)
                )
                run = storage.ensure_campaign_run(goal)
                phase = coordinator.apply_manager_decision(
                    run,
                    goal,
                    {
                        "decision": "start_phase",
                        "phase": {
                            "kind": "research_progression",
                            "objective": "Find an executable farm recipe.",
                            "targets": [
                                {
                                    "id": "recipe",
                                    "type": "phase_action_succeeded",
                                    "tools": [
                                        "prey",
                                        "knowledge_search",
                                        "hunting_grounds",
                                    ],
                                }
                            ],
                            "abandon_predicates": [],
                        },
                    },
                )

                self.assertEqual(
                    ["hunting_grounds"],
                    phase["success_criteria"][0]["tools"],
                )

                storage.transition_campaign_phase(
                    phase["id"], "failed", reason="exercise invalid target"
                )
                with self.assertRaisesRegex(
                    ValueError, "must require exactly.*hunting_grounds"
                ):
                    coordinator.apply_manager_decision(
                        run,
                        goal,
                        {
                            "decision": "start_phase",
                            "phase": {
                                "kind": "research_progression",
                                "objective": "Use an incompatible evidence alias.",
                                "targets": [
                                    {
                                        "id": "alias-only",
                                        "type": "phase_action_succeeded",
                                        "tools": ["prey"],
                                    }
                                ],
                                "abandon_predicates": [],
                            },
                        },
                    )

    def test_purchase_and_training_phases_allow_guarded_go_exit_recovery(self) -> None:
        tools = [
            {"name": "map"},
            {"name": "travel"},
            {"name": "go_through"},
            {"name": "shop"},
            {"name": "autopilot"},
        ]

        for phase_kind in ("acquire_item", "train_ability"):
            with self.subTest(phase_kind=phase_kind):
                selected = CampaignCoordinator.tools_for_phase(
                    {"kind": phase_kind}, tools
                )
                self.assertEqual(
                    {"map", "travel", "go_through", "shop"}
                    | ({"autopilot"} if phase_kind == "train_ability" else set()),
                    {tool["name"] for tool in selected},
                )

        training = CampaignCoordinator.tools_for_phase(
            {"kind": "train_ability"},
            [
                {"name": "prey"},
                {"name": "hunting_grounds"},
                {"name": "rest_up"},
            ],
        )
        self.assertEqual(
            {"prey", "hunting_grounds", "rest_up"},
            {tool["name"] for tool in training},
        )

    def test_return_home_phase_can_use_the_one_way_raza_exit(self) -> None:
        selected = CampaignCoordinator.tools_for_phase(
            {"kind": "return_home"},
            [
                {"name": "map"},
                {"name": "travel"},
                {"name": "leave_raza"},
                {"name": "autopilot"},
            ],
        )

        self.assertEqual(
            {"map", "travel", "leave_raza"},
            {tool["name"] for tool in selected},
        )

    def test_internal_phase_rejects_human_confirmation_criterion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(goal_payload())["goal"]
                coordinator = CampaignCoordinator(storage, CriteriaEvaluator(storage))
                run = storage.ensure_campaign_run(goal)

                with self.assertRaisesRegex(
                    ValueError, "internal campaign phases cannot require operator_confirmed"
                ):
                    coordinator.apply_manager_decision(
                        run,
                        goal,
                        {
                            "decision": "start_phase",
                            "phase": {
                                "kind": "research_progression",
                                "objective": "Find a usable regional farm.",
                                "success_criteria": [
                                    {
                                        "id": "human-check",
                                        "kind": "operator_confirmed",
                                    }
                                ],
                                "abandon_predicates": [],
                                "budget": {"max_actions": 20, "max_minutes": 45},
                            },
                        },
                    )

    def test_legacy_research_confirmation_migrates_to_successful_action_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(goal_payload())["goal"]
                coordinator = CampaignCoordinator(storage, CriteriaEvaluator(storage))
                run = storage.ensure_campaign_run(goal)
                phase = storage.create_campaign_phase(
                    run,
                    {
                        "kind": "research_progression",
                        "objective": "Find a usable regional farm.",
                        "success_criteria": [
                            {"id": "farm-found", "kind": "operator_confirmed"}
                        ],
                        "abandon_predicates": [],
                        "budget": {"max_actions": 20, "max_minutes": 45},
                    },
                    mode="start",
                )
                observation = {"look": {"room": {"num": 1012, "name": "Raza"}}}

                pending = coordinator.evaluate_phase(goal, run, phase, observation)
                self.assertFalse(pending.completed)
                migrated = storage.active_campaign_phase(run["id"])
                self.assertEqual(
                    "phase_action_succeeded",
                    migrated["success_criteria"][0]["kind"],
                )

                attempt_id = storage.create_phase_attempt(
                    phase["id"],
                    semantic_action="hunting_grounds",
                    signature="regional-farm-evidence",
                    expected_effect={"farm_candidate": True},
                )
                storage.update_phase_attempt(
                    attempt_id,
                    "succeeded",
                    result={"best_room": 1016, "creature": "mummy"},
                )

                completed = coordinator.evaluate_phase(
                    goal,
                    run,
                    storage.active_campaign_phase(run["id"]),
                    observation,
                )
                self.assertTrue(completed.completed)
                self.assertEqual("succeeded", completed.phase["status"])

    def test_progression_fallback_uses_controller_evidence_not_human_confirmation(self) -> None:
        goal = goal_payload(
            objective="Raise maximum HP to 25.",
            success_criteria=[
                {
                    "id": "hp-25",
                    "kind": "numeric_threshold",
                    "metric": "status.vitals.health.max",
                    "operator": ">=",
                    "value": 25,
                }
            ],
        )
        fallback = CampaignCoordinator.fallback_phase(
            goal,
            {"status": {"vitals": {"health": {"max": 20}}}},
        )

        self.assertEqual("research_progression", fallback["kind"])
        self.assertEqual(
            "phase_action_succeeded",
            fallback["success_criteria"][0]["kind"],
        )

    def test_liquidate_inventory_can_drop_junk_and_buy_replacement_gear(self) -> None:
        selected = CampaignCoordinator.tools_for_phase(
            {"kind": "liquidate_inventory"},
            [
                {"name": "act"},
                {"name": "shop"},
                {"name": "sell_all"},
                {"name": "fight"},
            ],
        )

        self.assertEqual(
            {"act", "shop", "sell_all"},
            {tool["name"] for tool in selected},
        )

    def test_campaign_manager_budget_floor_and_phase_tool_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(goal_payload())["goal"]
                coordinator = CampaignCoordinator(storage, CriteriaEvaluator(storage))
                run = storage.ensure_campaign_run(goal)
                phase = coordinator.apply_manager_decision(
                    run,
                    goal,
                    {
                        "decision": "start_phase",
                        "phase": {
                            "kind": "liquidate_inventory",
                            "objective": "Sell ordinary loot to create capacity.",
                            "success_criteria": [
                                {
                                    "id": "fewer-items",
                                    "kind": "numeric_threshold",
                                    "metric": "inventory.carry.items",
                                    "operator": "<=",
                                    "value": 5,
                                }
                            ],
                            "abandon_predicates": [],
                            "budget": {"max_actions": 1, "max_minutes": 1},
                        },
                    },
                )

                self.assertEqual({"max_actions": 8, "max_minutes": 30}, phase["budget"])
                selected = coordinator.tools_for_phase(
                    phase,
                    [
                        {"name": "sell"},
                        {"name": "sell_all"},
                        {"name": "knowledge_search"},
                        {"name": "fight"},
                        {"name": "autopilot"},
                    ],
                )
                self.assertEqual(
                    {"sell", "sell_all", "knowledge_search"},
                    {tool["name"] for tool in selected},
                )

    def test_campaign_manager_compiles_typed_currency_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(goal_payload())[
                    "goal"
                ]
                coordinator = CampaignCoordinator(
                    storage, CriteriaEvaluator(storage)
                )
                run = storage.ensure_campaign_run(goal)

                phase = coordinator.apply_manager_decision(
                    run,
                    goal,
                    {
                        "decision": "start_phase",
                        "phase": {
                            "kind": "liquidate_inventory",
                            "objective": "Raise enough cash for supplies.",
                            "targets": [
                                {
                                    "id": "cash-for-flasks",
                                    "type": "carried_currency_at_least",
                                    "amount": 168,
                                }
                            ],
                            "abandon_predicates": [],
                            "budget": {"max_actions": 20, "max_minutes": 45},
                        },
                    },
                )

                self.assertEqual(
                    {
                        "id": "cash-for-flasks",
                        "kind": "numeric_threshold",
                        "metric": "carried_currency",
                        "operator": ">=",
                        "value": 168,
                    },
                    phase["success_criteria"][0],
                )
                outcome = coordinator.evaluate_phase(
                    goal,
                    run,
                    phase,
                    {
                        "inventory": {
                            "items": [
                                {
                                    "id": 7098,
                                    "name": "shilling",
                                    "quantity": 2944,
                                }
                            ]
                        }
                    },
                )
                self.assertTrue(outcome.completed)

    def test_campaign_manager_rejects_invented_observation_metric(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(goal_payload())[
                    "goal"
                ]
                coordinator = CampaignCoordinator(
                    storage, CriteriaEvaluator(storage)
                )
                run = storage.ensure_campaign_run(goal)

                with self.assertRaisesRegex(
                    ValueError, "unsupported internal phase numeric metric"
                ):
                    coordinator.apply_manager_decision(
                        run,
                        goal,
                        {
                            "decision": "start_phase",
                            "phase": {
                                "kind": "liquidate_inventory",
                                "objective": "Raise enough cash for supplies.",
                                "success_criteria": [
                                    {
                                        "id": "bad-cash-path",
                                        "kind": "numeric_threshold",
                                        "metric": "inventory.items.gold.amount",
                                        "operator": ">=",
                                        "value": 168,
                                    }
                                ],
                                "abandon_predicates": [],
                            },
                        },
                    )

    def test_campaign_manager_rejects_launch_only_farm_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(goal_payload())["goal"]
                coordinator = CampaignCoordinator(
                    storage, CriteriaEvaluator(storage)
                )
                run = storage.ensure_campaign_run(goal)

                with self.assertRaisesRegex(
                    ValueError, "farm phase cannot complete merely because an action launched"
                ):
                    coordinator.apply_manager_decision(
                        run,
                        goal,
                        {
                            "decision": "start_phase",
                            "phase": {
                                "kind": "farm",
                                "objective": "Launch the keeper toward a farm.",
                                "targets": [
                                    {
                                        "id": "keeper-launched",
                                        "type": "phase_action_succeeded",
                                        "tools": ["autopilot"],
                                    }
                                ],
                                "abandon_predicates": [],
                                "context": {
                                    "room": 557,
                                    "target": "groundworm larva",
                                    "use_safe_spots": True,
                                },
                            },
                        },
                    )

    def test_campaign_manager_rejects_action_only_mutating_combat_preparation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(goal_payload())["goal"]
                coordinator = CampaignCoordinator(
                    storage, CriteriaEvaluator(storage)
                )
                run = storage.ensure_campaign_run(goal)

                with self.assertRaisesRegex(
                    ValueError, "mutating action success alone.*cast"
                ):
                    coordinator.apply_manager_decision(
                        run,
                        goal,
                        {
                            "decision": "start_phase",
                            "phase": {
                                "kind": "prepare_combat",
                                "objective": "Create food for later combat.",
                                "targets": [
                                    {
                                        "id": "cast-returned",
                                        "type": "phase_action_succeeded",
                                        "tools": ["cast"],
                                    }
                                ],
                                "abandon_predicates": [],
                            },
                        },
                    )

    def test_legacy_shilling_array_metric_migrates_and_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(goal_payload())[
                    "goal"
                ]
                coordinator = CampaignCoordinator(
                    storage, CriteriaEvaluator(storage)
                )
                run = storage.ensure_campaign_run(goal)
                phase = storage.create_campaign_phase(
                    run,
                    {
                        "kind": "liquidate_inventory",
                        "objective": "Raise enough cash for supplies.",
                        "success_criteria": [
                            {
                                "id": "cash-for-flasks",
                                "kind": "numeric_threshold",
                                "metric": "inventory.items.shilling.amount",
                                "operator": ">=",
                                "value": 168,
                            }
                        ],
                    },
                    mode="start",
                )

                outcome = coordinator.evaluate_phase(
                    goal,
                    run,
                    phase,
                    {
                        "inventory": {
                            "items": [
                                {
                                    "id": 7098,
                                    "name": "shilling",
                                    "quantity": 2944,
                                }
                            ]
                        }
                    },
                )

                self.assertTrue(outcome.completed)
                self.assertEqual(
                    "carried_currency",
                    outcome.phase["success_criteria"][0]["metric"],
                )

    def test_campaign_manager_ignores_untyped_optional_abandonment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(
                    goal_payload(request_id="campaign-untyped-abandon")
                )["goal"]
                coordinator = CampaignCoordinator(storage, CriteriaEvaluator(storage))
                run = storage.ensure_campaign_run(goal)
                phase = coordinator.apply_manager_decision(
                    run,
                    goal,
                    {
                        "decision": "start_phase",
                        "phase": {
                            "kind": "prepare_combat",
                            "objective": "Confirm combat equipment.",
                            "success_criteria": [
                                {
                                    "kind": "state_equals",
                                    "path": "equipment.known",
                                    "value": True,
                                }
                            ],
                            "abandon_predicates": [
                                {
                                    "condition": "no_weapon_available",
                                    "description": "No usable weapon could be found.",
                                }
                            ],
                            "budget": {"max_actions": 10, "max_minutes": 5},
                            "context": {},
                        },
                    },
                )

                self.assertEqual([], phase["abandon_predicates"])
                self.assertEqual(
                    {"max_actions": 10, "max_minutes": 30}, phase["budget"]
                )
                self.assertEqual(
                    "no_weapon_available",
                    phase["context"]["ignored_invalid_abandon_predicates"][0][
                        "condition"
                    ],
                )

    def test_campaign_phase_ignores_abandonment_true_before_first_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(
                    goal_payload(request_id="campaign-initially-true-abandon")
                )["goal"]
                coordinator = CampaignCoordinator(storage, CriteriaEvaluator(storage))
                run = storage.ensure_campaign_run(goal)
                phase = storage.create_campaign_phase(
                    run,
                    {
                        "kind": "prepare_combat",
                        "objective": "Equip a working mace.",
                        "success_criteria": [
                            {
                                "kind": "state_equals",
                                "path": "equipment.wielding",
                                "value": "mace",
                            }
                        ],
                        "abandon_predicates": [
                            {
                                "id": "empty-hand",
                                "kind": "state_equals",
                                "path": "equipment.wielding",
                                "value": None,
                            }
                        ],
                    },
                    mode="start",
                )

                outcome = coordinator.evaluate_phase(
                    goal,
                    run,
                    phase,
                    {"equipment": {"known": True, "wielding": None}},
                )

                self.assertFalse(outcome.completed)
                self.assertFalse(outcome.failed)
                persisted = storage.active_campaign_phase(run["id"])
                self.assertEqual([], persisted["abandon_predicates"])
                self.assertEqual(
                    ["mace"], persisted["success_criteria"][0]["value"]
                )
                self.assertEqual(
                    "empty-hand",
                    persisted["context"][
                        "ignored_initially_true_abandon_predicates"
                    ][0]["id"],
                )

    def test_unchanged_goal_completion_does_not_churn_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(goal_payload())["goal"]
                completion = {
                    "percent_estimate": 0,
                    "summary": "0 of 1 criteria verified",
                    "criteria": [
                        {
                            "id": "gone",
                            "kind": "state_equals",
                            "met": False,
                            "detail": "observed one item",
                        }
                    ],
                    "all_met": False,
                    "evidence_event_ids": [],
                }

                first = storage.set_goal_completion(goal["id"], completion)
                same = storage.set_goal_completion(goal["id"], dict(completion))
                detail_only = storage.set_goal_completion(
                    goal["id"],
                    {
                        **completion,
                        "criteria": [
                            {
                                **completion["criteria"][0],
                                "detail": "observed a different transient room",
                            }
                        ],
                    },
                )
                changed = storage.set_goal_completion(
                    goal["id"], {**completion, "percent_estimate": 1}
                )

                self.assertEqual(first["version"], same["version"])
                self.assertEqual(first["updated_at"], same["updated_at"])
                self.assertEqual(first["version"], detail_only["version"])
                self.assertEqual(first["updated_at"], detail_only["updated_at"])
                self.assertIn(
                    "different transient room",
                    detail_only["completion"]["criteria"][0]["detail"],
                )
                self.assertEqual(first["version"] + 1, changed["version"])

    def test_proposal_decision_event_includes_control_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                proposal = storage.create_proposal(
                    {
                        "title": "Future expedition",
                        "objective": "Visit a new area later.",
                        "success_criteria": [{"kind": "location_reached", "location": "Tos"}],
                    },
                    "Optional follow-up.",
                )
                storage.decide_proposal(
                    {
                        "request_id": "reject-future-expedition",
                        "proposal_id": proposal["id"],
                        "action": "reject",
                        "reason": "Duplicate created during controller maintenance.",
                    }
                )

                event = storage.events(kinds=["proposal.rejected"])["events"][0]
                self.assertIn("by control request", event["summary"])
                self.assertEqual("Duplicate created during controller maintenance.", event["data"]["reason"])

    def test_pending_proposals_with_same_normalized_title_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                first = storage.create_proposal(
                    {
                        "title": "Raise TestHero's max HP",
                        "objective": "Raise max HP to 25.",
                        "success_criteria": [
                            {"kind": "numeric_threshold", "metric": "status.vitals.health.max", "value": 25}
                        ],
                    },
                    "Useful follow-up.",
                )
                same = storage.create_proposal(
                    {
                        "title": "  RAISE   TESTHERO'S MAX HP  ",
                        "objective": "Increase max health to at least 25.",
                        "success_criteria": [
                            {"kind": "numeric_threshold", "metric": "status.vitals.health.max", "value": 25}
                        ],
                    },
                    "Equivalent follow-up.",
                )

                self.assertEqual(first["id"], same["id"])
                self.assertEqual(1, len(storage.proposals()))
                events = storage.events(kinds=["proposal.created"])["events"]
                self.assertEqual(1, len(events))

    def test_goal_is_idempotent_and_single_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                first = storage.submit_goal(goal_payload())
                same = storage.submit_goal(goal_payload())
                second = storage.submit_goal(goal_payload("request-2", title="Second"))
                self.assertEqual(first, same)
                self.assertEqual(first["goal"]["status"], "active")
                self.assertEqual(second["goal"]["status"], "queued")
                self.assertEqual(1, len(storage.goals(["active"])))
                with self.assertRaises(IdempotencyConflict):
                    storage.submit_goal(goal_payload("request-1", title="Different"))

    def test_event_criteria_are_anchored_to_current_cursor_and_goal_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                storage.emit_event("setup.one", "setup")
                anchor = storage.current_event_cursor()
                submitted = storage.submit_goal(
                    goal_payload(
                        "future-cursor",
                        success_criteria=[
                            {
                                "id": "phase",
                                "kind": "event_occurred",
                                "event_kind": "pvp.phase.completed",
                                "after_cursor": 5000,
                            }
                        ],
                    )
                )
                goal = submitted["goal"]

                self.assertEqual(anchor, goal["success_criteria"][0]["after_cursor"])
                self.assertEqual("EVENT_CURSOR_ANCHORED", submitted["warnings"][0]["code"])
                storage.emit_event("pvp.phase.completed", "wrong goal", goal_id="someone-else")
                incomplete = CriteriaEvaluator(storage).evaluate(goal, {})
                self.assertFalse(incomplete["all_met"])
                storage.emit_event("pvp.phase.completed", "right goal", goal_id=goal["id"])
                complete = CriteriaEvaluator(storage).evaluate(goal, {})
                self.assertTrue(complete["all_met"])

    def test_live_legacy_pvp_goal_criteria_upgrade_to_correlated_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(
                    goal_payload(
                        "legacy-pvp",
                        success_criteria=[
                            {"id": "engage", "kind": "event_occurred", "event_kind": "pvp.engagement.completed", "after_cursor": 5000},
                            {"id": "loot", "kind": "event_occurred", "event_kind": "property.transaction", "after_cursor": 5000},
                            {"id": "home", "kind": "location_reached", "room_id": 52},
                        ],
                    )
                )["goal"]

                upgraded = storage.upgrade_legacy_pvp_goal_criteria()

                self.assertEqual([goal["id"]], [item["id"] for item in upgraded])
                current = storage.goal(goal["id"])
                event_criteria = [item for item in current["success_criteria"] if item["kind"] == "event_occurred"]
                self.assertEqual(["pvp.phase.completed"], [item["event_kind"] for item in event_criteria])
                self.assertLess(event_criteria[0]["after_cursor"], 5000)

    def test_live_legacy_raza_confirmation_upgrades_to_exit_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(
                    goal_payload(
                        "legacy-raza-exit",
                        title="Increase Max HP to 25 and Leave Raza",
                        objective="Raise maximum HP to 25, then travel out of Raza.",
                        success_criteria=[
                            {
                                "id": "hp_25",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 25,
                            },
                            {"id": "left_raza", "kind": "operator_confirmed"},
                        ],
                    )
                )["goal"]

                upgraded = storage.upgrade_legacy_raza_exit_goal_criteria()

                self.assertEqual([goal["id"]], [item["id"] for item in upgraded])
                current = storage.goal(goal["id"])
                self.assertEqual(
                    {
                        "id": "left_raza",
                        "kind": "event_occurred",
                        "event_kind": "raza.left",
                        "after_cursor": storage.goal_event_anchor(goal["id"]),
                    },
                    current["success_criteria"][1],
                )
                self.assertEqual([], storage.upgrade_legacy_raza_exit_goal_criteria())

    def test_confirmation_only_for_operator_criterion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                goal = storage.submit_goal(goal_payload())["goal"]
                with self.assertRaises(InvalidTransition):
                    storage.manage_goal({"request_id": "confirm-1", "goal_id": goal["id"], "action": "confirm_complete"})
                operator = storage.submit_goal(goal_payload("request-2", title="Human check", success_criteria=[{"id": "human", "kind": "operator_confirmed"}]))["goal"]
                storage.manage_goal({"request_id": "pause-1", "goal_id": goal["id"], "action": "cancel"})
                operator = storage.goal(operator["id"])
                result = storage.manage_goal({"request_id": "confirm-2", "goal_id": operator["id"], "expected_version": operator["version"], "action": "confirm_complete"})
                self.assertEqual("active", result["goal"]["status"])
                self.assertTrue(result["confirmation_recorded"])
                self.assertEqual(
                    1,
                    len(
                        storage.events(
                            kinds=["goal.operator_confirmed"],
                            goal_id=operator["id"],
                        )["events"]
                    ),
                )

    def test_action_attempt_insert_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                attempt = storage.create_action_attempt(None, None, "rest", {}, "recover", "policy", "correlation")
                storage.update_action_attempt(attempt, "succeeded", result={"ok": True})
                self.assertTrue(attempt)

    def test_unknown_mutation_fields_and_unverified_observable_confirmation_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                with self.assertRaises(ValueError):
                    storage.submit_goal(goal_payload(debug_override=True))
                mixed = storage.submit_goal(
                    goal_payload(
                        "mixed",
                        success_criteria=[
                            {"id": "observed", "kind": "state_equals", "path": "status.done", "value": True},
                            {"id": "human", "kind": "operator_confirmed"},
                        ],
                    )
                )["goal"]
                with self.assertRaises(InvalidTransition):
                    storage.manage_goal({"request_id": "mixed-confirm", "goal_id": mixed["id"], "action": "confirm_complete"})

    def test_persona_contract_accepts_documented_shape_and_rejects_wrong_types(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                result = storage.set_persona(
                    {
                        "request_id": "persona-1",
                        "expected_version": 0,
                        "persona": {
                            "name": "Sable",
                            "character_voice": "A pragmatic wanderer with dry humor.",
                            "traits": ["curious", "wry"],
                            "speech_style": ["brief in danger"],
                            "values": ["self-preservation"],
                            "taboos": ["out-of-game system details"],
                            "relationship_defaults": "Warm slowly and remember favors.",
                            "max_reply_characters": 360,
                        },
                    }
                )
                self.assertEqual(1, result["version"])
                self.assertEqual("Sable", result["name"])
                with self.assertRaisesRegex(ValueError, "array of strings"):
                    storage.set_persona(
                        {"request_id": "persona-2", "persona": {"name": "Sable", "traits": "curious"}}
                    )

    def test_goal_contract_accepts_each_documented_criterion_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Storage(Path(temporary) / "bot.sqlite3") as storage:
                result = storage.submit_goal(
                    goal_payload(
                        "all-criteria",
                        success_criteria=[
                            {"id": "state", "kind": "state_equals", "path": "status.ready", "value": True},
                            {"id": "threshold", "kind": "numeric_threshold", "metric": "status.vitals.health.value", "operator": ">=", "value": 50},
                            {"id": "delta", "kind": "numeric_delta", "metric": "status.vitals.health.max", "operator": ">=", "value": 1, "baseline": 20},
                            {"id": "item", "kind": "inventory_contains", "item": "bread", "count": 1},
                            {"id": "place", "kind": "location_reached", "location": "Cibilo Creek Inn"},
                            {"id": "event", "kind": "event_occurred", "event_kind": "conversation.responded", "after_cursor": 0},
                            {"id": "all", "kind": "composite_all", "criteria": ["state", "threshold"]},
                            {"id": "any", "kind": "composite_any", "criterion_ids": ["item", "place"]},
                            {"id": "human", "kind": "operator_confirmed"},
                        ],
                        constraints={"avoid_death": True, "bank_before_hazard": True, "operator_notes": "Prefer variety."},
                    )
                )
                self.assertEqual("active", result["goal"]["status"])
                with self.assertRaisesRegex(ValueError, "unknown constraint field"):
                    storage.submit_goal(goal_payload("bad-constraint", constraints={"private_override": True}))
                with self.assertRaisesRegex(ValueError, "unknown inventory_contains criterion field"):
                    storage.submit_goal(
                        goal_payload(
                            "bad-criterion",
                            success_criteria=[{"kind": "inventory_contains", "item": "bread", "debug": True}],
                        )
                    )
                with self.assertRaisesRegex(ValueError, "Supported kinds: state_equals, numeric_threshold"):
                    storage.submit_goal(
                        goal_payload("bad-kind", success_criteria=[{"kind": "vital", "value": 50}])
                    )
                with self.assertRaisesRegex(ValueError, "unsupported event_occurred.event_kind: combat.kill"):
                    storage.submit_goal(
                        goal_payload(
                            "bad-event-kind",
                            success_criteria=[
                                {"kind": "event_occurred", "event_kind": "combat.kill"}
                            ],
                        )
                    )


if __name__ == "__main__":
    unittest.main()
