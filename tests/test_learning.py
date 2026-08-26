from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from meridian_bot.config import LearningConfig
from meridian_bot.learning import GoalDeferredError, GoalLearning
from meridian_bot.simulator import SimulatedBroker
from meridian_bot.storage import Storage
from meridian_bot.utils import json_hash


def campaign_goal(request_id: str, *, title: str = "Defeat two players", after_cursor: int = 0) -> dict[str, object]:
    return {
        "request_id": request_id,
        "title": title,
        "objective": "Defeat two other players, loot their dropped property, and return home.",
        "success_criteria": [
            {"id": "kills", "kind": "event_occurred", "event_kind": "pvp.engagement.completed", "after_cursor": after_cursor},
            {"id": "home", "kind": "location_reached", "location": "Tos Inn", "room_id": 52},
        ],
        "constraints": {"avoid_death": True, "bank_before_hazard": True},
        "priority": 70,
        "activation": "queue",
    }


class GoalLearningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "controller.sqlite3")
        self.corpus = "corpus-a"
        self.learning = GoalLearning(LearningConfig(), self.storage, lambda: self.corpus)
        self.broker = SimulatedBroker()
        self.broker.vitals["health"] = {"current": 25, "max": 25}

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def test_goal_family_ignores_title_ids_cursor_and_explicit_home_criterion(self) -> None:
        first = campaign_goal("one", title="First wording", after_cursor=10)
        second = campaign_goal("two", title="Entirely different wording", after_cursor=999)
        second["success_criteria"][0]["id"] = "renamed"  # type: ignore[index]
        second["success_criteria"][1] = {"id": "bar", "kind": "location_reached", "room_id": 52}  # type: ignore[index]
        self.assertEqual(self.learning.goal_family(first), self.learning.goal_family(second))

    def test_farm_room_scorecard_separates_target_yield_strategy_and_route_risk(self) -> None:
        self.storage.set_runtime(
            "background_farm_history_v1",
            [
                {
                    "observed_at": "2026-08-04T10:00:00Z",
                    "assigned_room": 575,
                    "room": 575,
                    "at_assigned_room": True,
                    "target": "giant rat",
                    "use_safe_spots": True,
                    "require_safe_wall": False,
                    "deltas": {"kills": 4, "withdrawals": 0, "deaths": 0},
                    "kills_by_target": {"giant rat": 1, "baby spider": 3},
                    "unattributed_kills": 0,
                    "healing_supplies_used": 0,
                    "risk_reasons": [],
                    "safe_spot_failure_count": 0,
                },
                {
                    "observed_at": "2026-08-04T10:05:00Z",
                    "assigned_room": 575,
                    "room": 576,
                    "at_assigned_room": False,
                    "target": "giant rat",
                    "use_safe_spots": True,
                    "require_safe_wall": False,
                    "deltas": {"kills": 0, "withdrawals": 1, "deaths": 0},
                    "kills_by_target": {},
                    "unattributed_kills": 0,
                    "healing_supplies_used": 1,
                    "risk_reasons": ["the keeper had to withdraw"],
                    "safe_spot_failure_count": 0,
                },
                {
                    "observed_at": "2026-08-04T10:06:00Z",
                    "assigned_room": 575,
                    "room": 575,
                    "at_assigned_room": True,
                    "target": "giant rat",
                    "use_safe_spots": True,
                    "deltas": {"kills": 0, "withdrawals": 0, "deaths": 0},
                    "kills_by_target": {},
                    "unattributed_kills": 0,
                    "healing_supplies_used": 0,
                    "risk_reasons": [],
                    "safe_spot_failure_count": 1,
                },
            ],
        )
        self.storage.set_runtime(
            "farm_tactic_quarantine_v1",
            {
                "575:giant-rat": {
                    "room": 575,
                    "target": "giant rat",
                    "reasons": ["live journal evidence disproved the safe spot"],
                }
            },
        )

        scorecard = self.learning.farm_room_scorecard()

        open_field = next(row for row in scorecard if row["strategy"] == "open_field")
        self.assertEqual(1, open_field["target_kills"])
        self.assertEqual(3, open_field["other_kills"])
        self.assertEqual(0.25, open_field["target_kill_share"])
        self.assertEqual(1, open_field["withdrawals"])
        self.assertEqual(1, open_field["route_withdrawals"])
        self.assertEqual(1, open_field["healing_supplies_used"])
        self.assertFalse(open_field["quarantined"])
        wall = next(row for row in scorecard if row["strategy"] == "safe_spots")
        self.assertEqual(1, wall["safe_spot_failure_count"])
        self.assertTrue(wall["quarantined"])
        readiness = self.learning.readiness_summary(self.broker.observe())
        self.assertIn("farm_room_scorecard", readiness)
        self.assertEqual(
            "safe_spots",
            readiness["farm_tactic_quarantines"][0]["quarantine_scope"],
        )
        self.assertTrue(
            readiness["farm_tactic_quarantines"][0]["effective_use_safe_spots"]
        )

    def test_productive_assigned_room_recovery_is_not_a_risk_sample(self) -> None:
        self.storage.set_runtime(
            "background_farm_history_v1",
            [
                {
                    "observed_at": "2026-08-04T11:00:00Z",
                    "assigned_room": 535,
                    "room": 535,
                    "at_assigned_room": True,
                    "target": "giant rat",
                    "use_safe_spots": True,
                    "deltas": {"kills": 5, "withdrawals": 1, "deaths": 0},
                    "kills_by_target": {"giant rat": 5},
                    "unattributed_kills": 0,
                    "healing_supplies_used": 0,
                    "recovery_reasons": [
                        "health reached the keeper flee threshold",
                        "the keeper had to withdraw",
                    ],
                    # Legacy records placed these ordinary recovery facts in
                    # risk_reasons; the scorecard must repair that meaning.
                    "risk_reasons": [
                        "health reached the keeper flee threshold",
                        "the keeper had to withdraw",
                    ],
                    "safe_spot_failure_count": 0,
                }
            ],
        )

        row = self.learning.farm_room_scorecard()[0]

        self.assertEqual(5, row["target_kills"])
        self.assertEqual(1, row["withdrawals"])
        self.assertEqual(1, row["recovery_samples"])
        self.assertEqual(0, row["risk_samples"])
        self.assertFalse(row["quarantined"])

    def test_finish_coordinates_are_ignored_only_with_explicit_finish_location(self) -> None:
        self.assertTrue(
            self.learning._is_finish_coordinate(
                {"kind": "state_equals", "path": "status.position.row", "value": 8}
            )
        )
        self.assertTrue(
            self.learning._is_finish_coordinate(
                {"kind": "state_equals", "path": "status.position.row", "value": 7}
            )
        )
        row_only = campaign_goal("row-only")
        row_only["success_criteria"] = [
            {"id": "row", "kind": "state_equals", "path": "status.position.row", "value": 8}
        ]
        row_seven = campaign_goal("row-seven")
        row_seven["success_criteria"] = [
            {"id": "row", "kind": "state_equals", "path": "status.position.row", "value": 7}
        ]
        self.assertNotEqual(
            self.learning.goal_family(row_only), self.learning.goal_family(row_seven)
        )

    def test_combat_lesson_preserves_open_goal_when_capability_changes(self) -> None:
        original = campaign_goal("original")
        created = self.storage.submit_goal(original)["goal"]
        observation = self.broker.observe()
        result = self.learning.defer_goal(
            created,
            observation,
            tool="pvp_engage",
            arguments={"target": "Blackstone"},
            reason="Repeated critical-health disengagement at 25 max HP",
            classification="insufficient_combat_power",
            scope="goal",
        )

        with self.assertRaises(GoalDeferredError) as caught:
            self.learning.require_goal_eligible(campaign_goal("retry-too-soon"), observation)
        self.assertEqual(caught.exception.result["lesson"]["classification"], "insufficient_combat_power")
        self.assertFalse(caught.exception.result["lesson"]["retry_evaluation"]["met"])
        self.assertEqual("active", self.storage.goal(created["id"])["status"])
        self.assertFalse(result["goal_blocked"])

        self.broker.vitals["health"] = {"current": 26, "max": 26}
        observation = self.broker.observe()
        self.learning.refresh_unlocks(observation)
        with self.assertRaises(GoalDeferredError) as ready:
            self.learning.require_goal_eligible(campaign_goal("retry-ready"), observation)
        self.assertEqual("GOAL_ALREADY_OPEN", ready.exception.result["code"])
        self.assertIn("already active", ready.exception.result["message"])
        self.assertEqual([], self.learning.status_summary(observation)["eligible_retries"])
        self.assertEqual(self.storage.goal_lesson(result["lesson"]["id"])["status"], "unlocked")  # type: ignore[index]

    def test_tactic_lesson_survives_goal_id_but_does_not_block_changed_tactic(self) -> None:
        created = self.storage.submit_goal(campaign_goal("route"))["goal"]
        observation = self.broker.observe()
        self.learning.defer_goal(
            created,
            observation,
            tool="travel",
            arguments={"destination": "North Gate"},
            reason="This route has no usable exit from the current room",
            classification="route_unavailable",
            scope="tactic",
            block=False,
        )
        self.assertIsNotNone(self.learning.check_action("travel", {"destination": "North Gate"}, observation))
        self.assertIsNone(self.learning.check_action("travel", {"destination": "South Gate"}, observation))
        self.broker.room = {"num": 101, "name": "Connected Hall"}
        self.assertIsNone(self.learning.check_action("travel", {"destination": "North Gate"}, self.broker.observe()))

    def test_insufficient_funds_lesson_unlocks_when_carried_currency_increases(self) -> None:
        created = self.storage.submit_goal(campaign_goal("shop-funds"))["goal"]
        self.broker.room = {"num": 53, "name": "Frisconar's Mysticals"}
        self.broker.inventory_items.append({"id": 9, "name": "shilling", "amount": 24})
        arguments = {"seller": 170, "buy_ids": [177, 177]}
        before = self.broker.observe()
        lesson = self.learning.defer_goal(
            created,
            before,
            tool="shop",
            arguments=arguments,
            reason='Frisconar says, "Come back when you have enough money for the flask."',
            classification="ineffective_tactic",
            scope="tactic",
            block=False,
        )["lesson"]

        self.assertEqual(24, lesson["failed_state"]["carried_currency"])
        self.assertIsNotNone(self.learning.check_action("shop", arguments, before))
        self.broker.inventory_items[-1]["amount"] = 664
        self.assertIsNone(self.learning.check_action("shop", arguments, self.broker.observe()))
        self.assertEqual("unlocked", self.storage.goal_lesson(lesson["id"])["status"])
        status = self.learning.status_summary(self.broker.observe())
        self.assertEqual([], status["eligible_retries"])
        self.assertEqual([], status["retries_in_progress"])

    def test_inventory_capacity_lesson_unlocks_only_after_inventory_load_changes(self) -> None:
        created = self.storage.submit_goal(campaign_goal("shop-capacity"))["goal"]
        self.broker.room = {"num": 201, "name": "Ye Olde Slasher Salesman"}
        self.broker.inventory_items.extend(
            [
                {"id": 348, "name": "mace", "amount": 3},
                {"id": 401, "name": "damaged shield", "amount": 1},
            ]
        )
        arguments = {"seller": 347, "buy_ids": [348]}
        before = self.broker.observe()
        lesson = self.learning.defer_goal(
            created,
            before,
            tool="shop",
            arguments=arguments,
            reason='Colhorr says, "I\'m unable to give you the mace. Perhaps you carry too much?"',
            classification="ineffective_tactic",
            scope="tactic",
            block=False,
        )["lesson"]

        self.assertEqual(
            "inventory_load_hash",
            lesson["retry_when"]["conditions"][0]["field"],
        )
        self.assertIsNotNone(self.learning.check_action("shop", arguments, before))
        self.broker.inventory_items = [
            item for item in self.broker.inventory_items if item.get("id") != 401
        ]
        self.assertIsNone(self.learning.check_action("shop", arguments, self.broker.observe()))
        self.assertEqual("unlocked", self.storage.goal_lesson(lesson["id"])["status"])

    def test_legacy_generic_shop_lesson_uses_blocked_action_reason_for_currency_migration(self) -> None:
        created = self.storage.submit_goal(campaign_goal("legacy-shop-funds"))["goal"]
        self.broker.room = {"num": 53, "name": "Frisconar's Mysticals"}
        arguments = {"seller": 170, "buy_ids": [177, 177, 177, 177]}
        observation = self.broker.observe()
        lesson = self.learning.defer_goal(
            created,
            observation,
            tool="shop",
            arguments=arguments,
            reason="the exact shop tactic repeatedly made no progress in the same state",
            classification="ineffective_tactic",
            scope="tactic",
            block=False,
        )["lesson"]
        lesson["failed_state"].pop("carried_currency", None)
        self.storage.set_runtime(
            "blocked_actions",
            [
                {
                    "goal_id": created["id"],
                    "signature": "legacy",
                    "tool": "shop",
                    "arguments": arguments,
                    "room": 53,
                    "reason": 'Frisconar says, "Come back when you have enough money for the flask."',
                }
            ],
        )
        self.broker.inventory_items.append({"id": 9, "name": "shilling", "amount": 664})

        evaluation = self.learning.evaluate_retry(lesson, self.broker.observe())

        self.assertTrue(evaluation["met"])
        self.assertTrue(
            any(item["condition"].get("field") == "carried_currency" for item in evaluation["conditions"])
        )

    def test_tactic_lesson_does_not_block_the_active_goal_by_default(self) -> None:
        created = self.storage.submit_goal(campaign_goal("tactic-only"))["goal"]
        result = self.learning.defer_goal(
            created,
            self.broker.observe(),
            tool="escape_underworld",
            arguments={"city": "Tos", "fine": True, "max_steps": 40},
            reason="the portal coordinate could not be reached",
            classification="route_unavailable",
            scope="tactic",
        )

        self.assertFalse(result["goal_blocked"])
        self.assertEqual("active", self.storage.goal(created["id"])["status"])

    def test_route_tactic_family_ignores_fine_movement_and_step_budget(self) -> None:
        observation = self.broker.observe()
        first = self.learning.tactic_family_key(
            "escape_underworld",
            {"city": "Tos", "fine": False, "max_steps": 20},
            observation,
        )
        second = self.learning.tactic_family_key(
            "escape_underworld",
            {"city": "Tos", "fine": True, "max_steps": 80},
            observation,
        )
        self.assertEqual(first, second)

    def test_carried_weapon_is_not_mistaken_for_equipped_weapon(self) -> None:
        observation = self.broker.observe()
        profile = self.learning.profile(observation)
        self.assertEqual("unknown", profile["equipment_state"])
        self.assertEqual(["Rusty sword"], profile["carried_weapons"])
        self.assertEqual([], profile["equipment"])

        observation["inventory"]["items"][0]["can"].append("unuse")
        equipped = self.learning.profile(observation)
        self.assertEqual("known", equipped["equipment_state"])
        self.assertEqual("Rusty sword", equipped["equipment"][0]["name"])

    def test_server_verified_equipment_takes_precedence_over_pack_inference(self) -> None:
        observation = self.broker.observe()
        observation["inventory"]["items"][0]["equipped"] = False
        observation["equipment"] = {
            "known": True,
            "equipped": [{"id": 77, "name": "Working mace"}],
            "wielding": ["Working mace"],
        }

        profile = self.learning.profile(observation)

        self.assertEqual("known", profile["equipment_state"])
        self.assertEqual([{"name": "Working mace", "id": 77, "slot": None}], profile["equipment"])
        self.assertEqual(["Working mace"], profile["wielded_weapons"])

    def test_worn_armor_is_not_mistaken_for_a_wielded_weapon(self) -> None:
        observation = self.broker.observe()
        observation["inventory"]["items"] = [
            {"id": 10, "name": "leather armor", "can": ["unuse"]}
        ]
        observation["equipment"] = {
            "known": True,
            "equipped": [{"id": 10, "name": "leather armor"}],
            "wielding": None,
        }

        readiness = self.learning.readiness_summary(observation)

        self.assertEqual([{"name": "leather armor", "id": 10, "slot": None}], readiness["equipped"])
        self.assertEqual([], readiness["wielded_weapons"])
        self.assertEqual([], readiness["carried_weapons"])

    def test_inventory_equipped_list_is_a_verified_fallback(self) -> None:
        observation = self.broker.observe()
        observation["inventory"]["equipped"] = []

        profile = self.learning.profile(observation)

        self.assertEqual("known", profile["equipment_state"])
        self.assertEqual([], profile["equipment"])

    def test_equipment_hash_stays_compatible_when_only_visibility_becomes_known(self) -> None:
        observation = self.broker.observe()
        unknown = self.learning.profile(observation)
        observation["inventory"]["items"][0]["equipped"] = False
        known_empty = self.learning.profile(observation)

        self.assertEqual(json_hash([]), unknown["equipment_hash"])
        self.assertEqual(unknown["equipment_hash"], known_empty["equipment_hash"])
        self.assertNotEqual(
            unknown["equipment_observation_hash"],
            known_empty["equipment_observation_hash"],
        )

    def test_reconnect_item_id_churn_does_not_unlock_tactic_lesson(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("stable-equipment-id"))["goal"]
        failed = self.broker.observe()
        failed["equipment"] = {
            "known": True,
            "equipped": [{"id": 16283, "name": "Short sword"}],
            "wielding": ["Short sword"],
        }
        deferred = self.learning.defer_goal(
            goal,
            failed,
            tool="autopilot",
            arguments={
                "action": "start",
                "mode": "farm",
                "hunt": "ant",
                "assigned_room": 563,
                "use_safe_spots": True,
            },
            reason="farm tactic made no progress",
            classification="ineffective_tactic",
            scope="tactic",
            block=False,
        )
        reconnected = self.broker.observe()
        reconnected["equipment"] = {
            "known": True,
            "equipped": [{"id": 7861, "name": "Short sword"}],
            "wielding": ["Short sword"],
        }

        before = self.learning.profile(failed)
        after = self.learning.profile(reconnected)

        self.assertEqual(before["equipment_hash"], after["equipment_hash"])
        self.assertEqual(before["capability_hash"], after["capability_hash"])
        self.assertFalse(
            self.learning.evaluate_retry(deferred["lesson"], reconnected)["met"]
        )
        self.assertEqual([], self.learning.refresh_unlocks(reconnected))
        self.assertEqual(
            "deferred", self.storage.goal_lesson(deferred["lesson"]["id"])["status"]
        )

    def test_equipment_loss_after_death_does_not_unlock_farm_tactic(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("death-is-not-upgrade"))["goal"]
        self.broker.vitals["health"] = {"current": 35, "max": 35}
        failed = self.broker.observe()
        failed["equipment"] = {
            "known": True,
            "equipped": [{"id": 16283, "name": "Short sword"}],
            "wielding": ["Short sword"],
        }
        lesson = self.learning.defer_goal(
            goal,
            failed,
            tool="autopilot",
            arguments={
                "action": "start",
                "mode": "farm",
                "hunt": "ant",
                "assigned_room": 584,
                "use_safe_spots": True,
            },
            reason="repeated retreat episodes reached the farm tactic safety limit",
            classification="ineffective_tactic",
            scope="tactic",
            block=False,
        )["lesson"]
        self.storage.update_goal_lesson(lesson["id"], "unlocked")
        self.storage.set_runtime(
            "combat_outcomes_v1",
            [
                {
                    "occurred_at": "2099-01-01T00:00:00Z",
                    "room": {"id": 584, "name": "The Flatlands"},
                    "target": "ant",
                    "died": True,
                }
            ],
        )
        self.broker.vitals["health"] = {"current": 34, "max": 34}
        current = self.broker.observe()
        current["equipment"] = {"known": True, "equipped": [], "wielding": []}

        evaluation = self.learning.evaluate_retry(lesson, current)
        repaired = self.learning.repair_regressive_capability_unlocks(current)

        self.assertFalse(evaluation["met"])
        self.assertEqual([lesson["id"]], [item["id"] for item in repaired])
        self.assertEqual("deferred", self.storage.goal_lesson(lesson["id"])["status"])
        quarantines = self.storage.get_runtime("farm_tactic_quarantine_v1", {})
        quarantine = next(iter(quarantines.values()))
        self.assertEqual("ant", quarantine["target"])
        self.assertTrue(quarantine["use_safe_spots"])
        self.assertEqual(goal["id"], quarantine["goal_id"])

    def test_legacy_farm_survivability_cooldown_cannot_unlock_unchanged_tactic(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("no-safety-timeout"))["goal"]
        observation = self.broker.observe()
        lesson = self.learning.defer_goal(
            goal,
            observation,
            tool="autopilot",
            arguments={
                "action": "start",
                "mode": "farm",
                "hunt": "ant",
                "assigned_room": 584,
                "use_safe_spots": True,
            },
            reason=(
                "Background farming exceeded verified survivability in the assigned "
                "room: repeated retreat episodes reached the farm tactic safety limit"
            ),
            classification="ineffective_tactic",
            scope="tactic",
            block=False,
        )["lesson"]
        lesson["retry_when"] = {
            "mode": "any",
            "conditions": [
                {
                    "kind": "capability_changed",
                    "from": lesson["failed_state"]["capability_hash"],
                },
                {
                    "kind": "cooldown_elapsed",
                    "seconds": 1,
                    "since": "2000-01-01T00:00:00Z",
                },
            ],
        }

        evaluation = self.learning.evaluate_retry(lesson, observation)

        self.assertFalse(evaluation["met"])
        self.assertFalse(
            any(
                item["condition"].get("kind") == "cooldown_elapsed"
                for item in evaluation["conditions"]
            )
        )

    def test_adding_equipment_is_a_monotonic_capability_gain(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("equipment-added"))["goal"]
        failed = self.broker.observe()
        failed["equipment"] = {
            "known": True,
            "equipped": [{"id": 1, "name": "Short sword"}],
            "wielding": ["Short sword"],
        }
        lesson = self.learning.defer_goal(
            goal,
            failed,
            tool="autopilot",
            reason="too weak",
            classification="insufficient_combat_power",
            scope="tactic",
            block=False,
        )["lesson"]
        improved = self.broker.observe()
        improved["equipment"] = {
            "known": True,
            "equipped": [
                {"id": 9, "name": "Short sword"},
                {"id": 10, "name": "Leather armor", "slot": "torso"},
            ],
            "wielding": ["Short sword"],
        }

        self.assertTrue(self.learning.evaluate_retry(lesson, improved)["met"])

    def test_tactic_unlock_event_is_not_reported_as_goal_unlock(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("tactic-unlock-event"))["goal"]
        deferred = self.learning.defer_goal(
            goal,
            self.broker.observe(),
            tool="travel",
            arguments={"to": 54},
            reason="route was unavailable",
            classification="route_unavailable",
            scope="tactic",
            block=False,
        )

        self.storage.update_goal_lesson(deferred["lesson"]["id"], "unlocked")

        events = self.storage.goal_events(
            goal["id"], kinds=["goal.retry_unlocked", "tactic.retry_unlocked"], limit=10
        )
        self.assertEqual(["tactic.retry_unlocked"], [item["kind"] for item in events])
        self.assertEqual(
            "Deferred tactic is eligible for a revised retry", events[0]["summary"]
        )

    def test_healing_supplies_are_readiness_and_retry_evidence(self) -> None:
        original = self.storage.submit_goal(campaign_goal("supplies-origin"))["goal"]
        result = self.learning.defer_goal(
            original,
            self.broker.observe(),
            tool="pvp_engage",
            reason="too weak",
            classification="insufficient_combat_power",
            scope="goal",
        )
        self.broker.inventory_items.append(
            {"id": 2, "name": "Flask", "amount": 4, "can": ["use", "drop"]}
        )
        observation = self.broker.observe()
        profile = self.learning.profile(observation)
        self.assertEqual(4, profile["healing_supply_count"])
        self.assertEqual(4, self.learning.readiness_summary(observation)["healing_supply_count"])
        evaluation = self.learning.evaluate_retry(result["lesson"], observation)
        self.assertTrue(evaluation["met"])
        self.assertTrue(
            any(
                item["met"] and item["condition"].get("field") == "healing_supply_count"
                for item in evaluation["conditions"]
            )
        )

    def test_legacy_combat_lesson_unlocks_on_newly_observed_flask(self) -> None:
        original = self.storage.submit_goal(campaign_goal("legacy-supplies"))["goal"]
        lesson = self.learning.defer_goal(
            original,
            self.broker.observe(),
            tool="pvp_engage",
            reason="too weak",
            classification="insufficient_combat_power",
            scope="goal",
        )["lesson"]
        lesson["retry_when"]["conditions"] = [
            item
            for item in lesson["retry_when"]["conditions"]
            if item.get("field") != "healing_supply_count"
        ]
        lesson["failed_state"].pop("healing_supply_count", None)
        self.broker.inventory_items.append(
            {"id": 2, "name": "Flask", "amount": 1, "can": ["use", "drop"]}
        )
        self.assertTrue(self.learning.evaluate_retry(lesson, self.broker.observe())["met"])

    def test_goal_family_migration_preserves_retry_and_resolution_links(self) -> None:
        original = self.storage.submit_goal(campaign_goal("legacy-family-origin"))["goal"]
        profile = self.learning.profile(self.broker.observe())
        lesson = self.storage.create_goal_lesson(
            {
                "goal_id": original["id"],
                "goal_family": "goal-family:pre-home-coordinate-fix",
                "tactic_key": "tactic:legacy-family",
                "classification": "insufficient_combat_power",
                "scope": "goal",
                "confidence": 0.9,
                "summary": "legacy family identity",
                "failed_state": profile,
                "evidence_event_ids": [],
                "retry_when": {
                    "mode": "any",
                    "conditions": [
                        {"kind": "numeric_increase", "field": "max_health", "from": 24}
                    ],
                },
                "suggested_goals": [],
            }
        )
        self.storage.update_goal_lesson(lesson["id"], "unlocked")
        self.storage.manage_goal(
            {
                "request_id": "close-legacy-family-origin",
                "goal_id": original["id"],
                "expected_version": original["version"],
                "action": "cancel",
                "reason": "exercise retry lineage after the original is no longer open",
            }
        )

        retry = campaign_goal("legacy-family-retry")
        review = self.learning.submission_review(retry, self.broker.observe())
        self.assertEqual(original["id"], review["retry_of_goal_id"])
        self.assertEqual(lesson["id"], review["lesson_id"])

        retry_goal = self.storage.submit_goal(retry, retry_of_goal_id=original["id"])["goal"]
        resolved = self.learning.record_success(retry_goal)
        self.assertEqual([lesson["id"]], [item["id"] for item in resolved])
        self.assertEqual("resolved", self.storage.goal_lesson(lesson["id"])["status"])

    def test_unlocked_lesson_does_not_duplicate_preserved_open_goal(self) -> None:
        original = self.storage.submit_goal(campaign_goal("retry-origin"))["goal"]
        self.learning.defer_goal(
            original,
            self.broker.observe(),
            tool="pvp_engage",
            reason="too weak",
            classification="insufficient_combat_power",
            scope="goal",
        )
        self.broker.vitals["health"] = {"current": 26, "max": 26}
        observation = self.broker.observe()
        self.learning.refresh_unlocks(observation)

        status = self.learning.status_summary(observation)
        self.assertEqual(status["eligible_retries"], [])
        self.assertEqual(status["retries_in_progress"], [])
        with self.assertRaises(GoalDeferredError) as caught:
            self.learning.require_goal_eligible(campaign_goal("retry-duplicate"), observation)
        self.assertEqual("GOAL_ALREADY_OPEN", caught.exception.result["code"])
        self.assertIn("already active", caught.exception.result["message"])

    def test_repeated_tactic_budget_is_scoped_to_recorded_room(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("room-scope"))["goal"]
        arguments = {"destination": "North Gate"}
        for room_num in (100, 101, 102):
            room = {"num": room_num, "name": f"Room {room_num}"}
            event = self.storage.emit_event(
                "action.no_progress",
                "failed route",
                goal_id=goal["id"],
                data={"tool": "travel", "arguments": arguments, "room": room, "reason": "no exit"},
            )
        observation = self.broker.observe()
        self.assertIsNone(
            self.learning.maybe_defer(
                goal,
                observation,
                tool="travel",
                arguments=arguments,
                reason="no exit",
                event=event,
            )
        )

    def test_verified_action_success_resets_failure_budget(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("progress-reset"))["goal"]
        observation = self.broker.observe()
        arguments = {"destination": "North Gate"}

        for _ in range(2):
            self.storage.emit_event(
                "action.no_progress",
                "failed route",
                goal_id=goal["id"],
                data={
                    "tool": "travel",
                    "arguments": arguments,
                    "room": observation["look"]["room"],
                    "reason": "no exit",
                },
            )
        self.storage.emit_event(
            "action.succeeded",
            "verified progress",
            goal_id=goal["id"],
            data={"tool": "look"},
        )
        for _ in range(2):
            event = self.storage.emit_event(
                "action.no_progress",
                "failed route",
                goal_id=goal["id"],
                data={
                    "tool": "travel",
                    "arguments": arguments,
                    "room": observation["look"]["room"],
                    "reason": "no exit",
                },
            )

        self.assertIsNone(
            self.learning.maybe_defer(
                goal,
                observation,
                tool="travel",
                arguments=arguments,
                reason="no exit",
                event=event,
            )
        )

    def test_old_failures_expire_outside_evidence_window(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("failure-expiry"))["goal"]
        observation = self.broker.observe()
        arguments = {"destination": "North Gate"}
        for _ in range(self.learning.config.repeated_tactic_budget):
            event = self.storage.emit_event(
                "action.no_progress",
                "failed route",
                goal_id=goal["id"],
                data={
                    "tool": "travel",
                    "arguments": arguments,
                    "room": observation["look"]["room"],
                    "reason": "no exit",
                },
            )

        future = time.time() + self.learning.config.failure_evidence_window_seconds + 1
        with patch("meridian_bot.learning.time.time", return_value=future):
            deferred = self.learning.maybe_defer(
                goal,
                observation,
                tool="travel",
                arguments=arguments,
                reason="no exit",
                event=event,
            )

        self.assertIsNone(deferred)

    def test_unlocked_farm_lesson_releases_matching_quarantine(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("farm-unlock-release"))["goal"]
        observation = self.broker.observe()
        self.storage.set_runtime(
            "farm_tactic_quarantine_v1",
            {
                "535": {
                    "room": 535,
                    "assigned_room": 535,
                    "target": "centipede",
                    "use_safe_spots": True,
                    "goal_id": goal["id"],
                    "reasons": ["repeated retreat episodes reached the safety limit"],
                }
            },
        )
        self.storage.set_runtime(
            "farm_tactic_retreat_incidents_v1",
            {
                "exact": {
                    "goal_id": goal["id"],
                    "assigned_room": 535,
                    "target": "centipede",
                    "use_safe_spots": True,
                    "incidents": [{"at": time.time()}],
                }
            },
        )
        deferred = self.learning.defer_goal(
            goal,
            observation,
            tool="autopilot",
            arguments={
                "action": "start",
                "mode": "farm",
                "hunt": "centipede",
                "assigned_room": 535,
                "use_safe_spots": True,
            },
            reason="repeated retreat episodes reached the safety limit",
            classification="ineffective_tactic",
            scope="tactic",
            block=False,
            retry_when={
                "mode": "any",
                "conditions": [
                    {
                        "kind": "numeric_increase",
                        "field": "max_health",
                        "from": 25,
                    }
                ],
            },
        )

        self.broker.room = {"num": 101, "name": "Another Room"}
        self.assertEqual([], self.learning.refresh_unlocks(self.broker.observe()))
        self.assertIn(
            "535", self.storage.get_runtime("farm_tactic_quarantine_v1", {})
        )

        self.broker.vitals["health"] = {"current": 26, "max": 26}
        unlocked = self.learning.refresh_unlocks(self.broker.observe())

        self.assertEqual([deferred["lesson"]["id"]], [item["id"] for item in unlocked])
        self.assertEqual(
            {}, self.storage.get_runtime("farm_tactic_quarantine_v1", {})
        )
        self.assertEqual(
            {}, self.storage.get_runtime("farm_tactic_retreat_incidents_v1", {})
        )

    def test_aggregate_bank_failures_remain_tactic_scoped(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("bank-preparation-budget"))["goal"]
        observation = self.broker.observe()
        last_event = None
        for amount in range(1, self.learning.config.no_progress_budget + 1):
            last_event = self.storage.emit_event(
                "action.no_progress",
                "deposit made no progress",
                goal_id=goal["id"],
                data={
                    "tool": "bank",
                    "arguments": {"action": "deposit", "amount": amount},
                    "room": observation["look"]["room"],
                    "reason": "deposit needs a positive amount",
                },
            )

        deferred = self.learning.maybe_defer(
            goal,
            observation,
            tool="bank",
            arguments={"action": "deposit", "amount": 999},
            reason="deposit needs a positive amount",
            event=last_event,
        )

        self.assertIsNotNone(deferred)
        self.assertEqual("tactic", deferred["lesson"]["scope"])
        self.assertFalse(deferred["goal_blocked"])
        self.assertEqual("active", self.storage.goal(goal["id"])["status"])

    def test_aggregate_inventory_drop_failures_remain_tactic_scoped(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("drop-preparation-budget"))["goal"]
        observation = self.broker.observe()
        last_event = None
        for target in range(1, self.learning.config.no_progress_budget + 1):
            last_event = self.storage.emit_event(
                "action.no_progress",
                "drop made no progress",
                goal_id=goal["id"],
                data={
                    "tool": "act",
                    "arguments": {"verb": "drop", "target": target},
                    "room": observation["look"]["room"],
                    "reason": "the requested stack quantity was refused",
                },
            )

        deferred = self.learning.maybe_defer(
            goal,
            observation,
            tool="act",
            arguments={"verb": "drop", "target": 99},
            reason="the requested stack quantity was refused",
            event=last_event,
        )

        self.assertIsNotNone(deferred)
        self.assertEqual("tactic", deferred["lesson"]["scope"])
        self.assertIn("Preparation failure budget", deferred["lesson"]["summary"])
        self.assertFalse(deferred["goal_blocked"])
        self.assertEqual("active", self.storage.goal(goal["id"])["status"])

    def test_broken_equipment_tactic_does_not_unlock_after_moving_rooms(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("broken-equipment"))["goal"]
        observation = self.broker.observe()
        deferred = self.learning.defer_goal(
            goal,
            observation,
            tool="act",
            arguments={"verb": "use", "target": 7048},
            reason="You can't use the mace--it's broken.",
            classification="missing_capability",
            scope="tactic",
            block=False,
        )
        self.broker.room = {"num": 101, "name": "Another Room"}

        evaluation = self.learning.evaluate_retry(
            deferred["lesson"], self.broker.observe()
        )

        self.assertFalse(evaluation["met"])
        self.assertFalse(
            any(
                item["condition"].get("kind") == "tactic_location_changed"
                for item in evaluation["conditions"]
            )
        )

    def test_legacy_preparation_goal_lesson_is_resolved_without_requeueing_goal(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("legacy-bank-gate"))["goal"]
        deferred = self.learning.defer_goal(
            goal,
            self.broker.observe(),
            tool="bank",
            arguments={"action": "deposit", "amount": 30},
            reason="Failure budget exhausted without verified goal progress: deposit needs a positive amount",
            classification="ineffective_tactic",
            scope="goal",
        )
        self.assertEqual("active", self.storage.goal(goal["id"])["status"])
        self.storage.update_goal_lesson(deferred["lesson"]["id"], "unlocked")

        repaired = self.learning.repair_preparation_goal_lessons()

        self.assertEqual([deferred["lesson"]["id"]], [item["id"] for item in repaired])
        self.assertEqual("resolved", self.storage.goal_lesson(deferred["lesson"]["id"])["status"])
        self.assertEqual("active", self.storage.goal(goal["id"])["status"])
        self.assertEqual([], self.storage.goals(["queued"]))

    def test_legacy_inventory_drop_goal_lesson_is_repaired(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("legacy-drop-gate"))["goal"]
        deferred = self.learning.defer_goal(
            goal,
            self.broker.observe(),
            tool="act",
            arguments={"verb": "drop", "target": 7053},
            reason="Failure budget exhausted without verified goal progress: stack quantity refused",
            classification="ineffective_tactic",
            scope="goal",
        )
        self.storage.update_goal_lesson(deferred["lesson"]["id"], "unlocked")

        repaired = self.learning.repair_preparation_goal_lessons()

        self.assertEqual([deferred["lesson"]["id"]], [item["id"] for item in repaired])
        self.assertEqual(
            "resolved", self.storage.goal_lesson(deferred["lesson"]["id"])["status"]
        )

    def test_legacy_lesson_recovers_failed_tactic_from_runtime_memory(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("legacy-tactic"))["goal"]
        observation = self.broker.observe()
        arguments = {"agent": "primary", "city": "Tos"}
        profile = self.learning.profile(observation)
        lesson = self.storage.create_goal_lesson(
            {
                "goal_id": goal["id"],
                "goal_family": self.learning.goal_family(goal),
                "tactic_key": self.learning.tactic_key("escape_underworld", arguments, observation),
                "classification": "route_unavailable",
                "scope": "tactic",
                "confidence": 0.9,
                "summary": "could not get next to the shifting portal",
                "failed_state": profile,
                "evidence_event_ids": [],
                "retry_when": {"mode": "any", "conditions": [{"kind": "location_changed", "from": profile["room"]}]},
                "suggested_goals": [],
            }
        )
        self.storage.set_runtime(
            "blocked_actions",
            [{"tool": "escape_underworld", "arguments": arguments, "room": 100, "reason": "blocked"}],
        )
        public = self.learning.public_lesson(lesson)
        self.assertEqual(public["failed_tactic"]["arguments"]["city"], "Tos")

    def test_invalid_reference_requires_new_corpus_not_time_or_hp(self) -> None:
        created = self.storage.submit_goal(campaign_goal("invalid"))["goal"]
        observation = self.broker.observe()
        lesson = self.learning.defer_goal(
            created,
            observation,
            tool="knowledge_search",
            arguments={"query": "Silverfall"},
            reason="authoritative lookup returned no matches",
            classification="invalid_reference",
            scope="goal",
        )["lesson"]
        self.broker.vitals["health"] = {"current": 100, "max": 100}
        self.assertFalse(self.learning.evaluate_retry(lesson, self.broker.observe())["met"])
        self.corpus = "corpus-b"
        self.assertTrue(self.learning.evaluate_retry(lesson, self.broker.observe())["met"])

    def test_planner_lookup_miss_is_classified_as_a_tactic_failure(self) -> None:
        classification, scope, confidence = GoalLearning.classify(
            "map",
            "authoritative lookup returned no matches",
        )
        self.assertEqual("invalid_reference", classification)
        self.assertEqual("tactic", scope)
        self.assertGreater(confidence, 0.9)

    def test_pvp_patrol_route_failure_is_not_combat_readiness_failure(self) -> None:
        classification, scope, confidence = GoalLearning.classify(
            "pvp_seek",
            "patrol route could not reach room 574: no floor anywhere on the west boundary",
            event_kind="pvp.search.failed",
        )
        self.assertEqual("route_unavailable", classification)
        self.assertEqual("tactic", scope)
        self.assertGreater(confidence, 0.9)

    def test_creation_cast_failure_is_not_combat_readiness_evidence(self) -> None:
        classification, scope, confidence = GoalLearning.classify(
            "cast",
            "the exact cast tactic repeatedly made no progress in the same state",
            event_kind="action.retry_suppressed",
        )

        self.assertEqual("ineffective_tactic", classification)
        self.assertEqual("tactic", scope)
        self.assertGreater(confidence, 0.7)

    def test_silent_go_reply_is_dependency_failure_not_route_evidence(self) -> None:
        classification, scope, confidence = GoalLearning.classify(
            "travel",
            (
                "sent go and the server answered nothing at all — no room change and no "
                "refusal, which is not a door problem but a lost packet or a reply that "
                "did not arrive inside 4s"
            ),
        )
        self.assertEqual("dependency_failure", classification)
        self.assertEqual("tactic", scope)
        self.assertGreaterEqual(confidence, 0.9)

    def test_success_resolves_all_lessons_for_family(self) -> None:
        created = self.storage.submit_goal(campaign_goal("success"))["goal"]
        lesson = self.learning.defer_goal(
            created,
            self.broker.observe(),
            tool="pvp_engage",
            reason="too weak",
            classification="insufficient_combat_power",
            scope="goal",
            block=False,
        )["lesson"]
        self.learning.record_success(created)
        self.assertEqual(self.storage.goal_lesson(lesson["id"])["status"], "resolved")  # type: ignore[index]

    def test_redeferring_a_false_unlock_clears_unlock_timestamp(self) -> None:
        created = self.storage.submit_goal(campaign_goal("migration-repair"))["goal"]
        lesson = self.learning.defer_goal(
            created,
            self.broker.observe(),
            tool="pvp_engage",
            reason="too weak",
            classification="insufficient_combat_power",
            scope="goal",
            block=False,
        )["lesson"]
        unlocked = self.storage.update_goal_lesson(lesson["id"], "unlocked")
        self.assertIsNotNone(unlocked["unlocked_at"])
        deferred = self.storage.update_goal_lesson(
            lesson["id"],
            "deferred",
            evidence={"reason": "migration-induced unlock was invalid"},
        )
        self.assertIsNone(deferred["unlocked_at"])
        self.assertEqual("deferred", deferred["status"])

    def test_backfill_turns_prior_invalid_block_into_a_durable_lesson(self) -> None:
        goal = self.storage.submit_goal(campaign_goal("backfill"))["goal"]
        self.storage.block_goal(goal["id"], reason="bad place", blocked_reason="invalid_game_reference")
        created = self.learning.backfill(self.broker.observe())
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["classification"], "invalid_reference")
        self.assertEqual(self.learning.status_summary(self.broker.observe())["deferred_count"], 1)


if __name__ == "__main__":
    unittest.main()
