from __future__ import annotations

import copy
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from meridian_bot.controller import BotController
from meridian_bot.model import ModelError
from meridian_bot.pvp import PVP_SEEK_TOOL_NAME, PVP_TOOL_NAME, PvpCoordinator
from meridian_bot.simulator import SimulatedBroker

from .helpers import config, goal_payload


def direct_pvp_goal(request_id: str = "direct-pvp") -> dict[str, Any]:
    return goal_payload(
        request_id=request_id,
        title="Fresh local PvP opportunity vs Rival",
        objective="Engage Rival with pvp_engage, loot, and resume progression.",
        success_criteria=[
            {
                "id": "phase",
                "kind": "event_occurred",
                "event_kind": "pvp.phase.completed",
                "after_cursor": 0,
            }
        ],
        constraints={
            "operator_notes": (
                "Rival is present in the current fresh local observation. Use pvp_engage only "
                "against Rival; do not use who, pvp_seek, camp, patrol, or substitute another "
                "target if Rival leaves. If the peer disappears before a server-accepted swing, "
                "cancel this goal and resume progression."
            )
        },
    )


class PvpBroker(SimulatedBroker):
    def __init__(self, *, target_visible: bool = True, vanish_after: int | None = None) -> None:
        super().__init__()
        self.target_visible = target_visible
        self.vanish_after = vanish_after
        self.attacks = 0
        self.health_value = 100
        self.health_after_attacks: list[int] = []
        self.target_distance = 3

    def observe(self) -> dict[str, Any]:
        observation = super().observe()
        health = {"current": self.health_value, "max": 100}
        observation["look"]["vitals"]["health"] = copy.deepcopy(health)
        observation["status"]["vitals"]["health"] = copy.deepcopy(health)
        return observation

    def _look(self) -> dict[str, Any]:
        objects: list[dict[str, Any]] = []
        if self.target_visible:
            objects.extend(
                [
                    {
                        "id": 700,
                        "name": "Rival",
                        "is_player": True,
                        "relation": "enemy",
                        "safety_on": False,
                        "distance": self.target_distance,
                        "col": 12,
                        "row": 10,
                        "can": ["attack", "look", "offer"],
                    },
                    {
                        "id": 701,
                        "name": "Rivalry",
                        "is_player": True,
                        "relation": "neutral",
                        "safety_on": True,
                        "distance": 1,
                        "col": 11,
                        "row": 10,
                        "can": ["attack", "look", "offer"],
                    },
                ]
            )
        return {
            "room": copy.deepcopy(self.room),
            "you": {"object_id": 600, "col": 10, "row": 10},
            "vitals": {
                "health": {"value": self.health_value, "max": 100, "pct": self.health_value},
                "mana": {"value": 50, "max": 50, "pct": 100},
            },
            "objects": objects,
            "exits": [
                {
                    "kind": "go",
                    "to": 101,
                    "to_name": "Safe Room",
                    "steps_away": 2,
                    "reachable": True,
                }
            ],
        }

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float = 180,
        mutation: bool = False,
    ) -> Any:
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "look":
            return self._look()
        if name == "approach":
            self.target_distance = 1
            return {"in_position": True, "distance": 1, "target": arguments["target"]}
        if name == "attack":
            self.attacks += 1
            if self.health_after_attacks:
                self.health_value = self.health_after_attacks.pop(0)
            if self.vanish_after is not None and self.attacks >= self.vanish_after:
                self.target_visible = False
            return {
                "target": arguments["target"],
                "swings": [{"swing": 1, "messages": ["You attack Rival."]}],
                "vitals": {"health": {"value": self.health_value, "max": 100}},
            }
        if name == "loot":
            return {"picked_up": [{"id": 900, "name": "Rival's sword"}]}
        if name == "go_through":
            self.room = {"num": 101, "name": "Safe Room"}
            self.target_visible = False
            return {"left": True, "now": {"room": copy.deepcopy(self.room)}}
        if name == "cast":
            return {"cast": True, "spell": arguments["spell"], "targets": [arguments["target"]] if "target" in arguments else []}
        if name in {"autopilot", "converse", "safety", "equip_best"}:
            return {"ok": True, "tool": name, **arguments}
        return {"ok": True, "tool": name}


class PatrolBroker(PvpBroker):
    def __init__(self) -> None:
        super().__init__(target_visible=False, vanish_after=1)

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float = 180,
        mutation: bool = False,
    ) -> Any:
        if name == "who":
            self.calls.append((name, copy.deepcopy(arguments)))
            return {
                "players": [
                    {"id": 600, "name": "TestHero"},
                    {"id": 700, "name": "Rival"},
                ],
                "here": [{"id": 600, "name": "TestHero"}],
            }
        if name == "travel":
            self.calls.append((name, copy.deepcopy(arguments)))
            room_id = int(arguments["to"])
            self.room = {"num": room_id, "name": f"Room {room_id}"}
            self.target_visible = room_id == 61
            return {"arrived": True, "room": copy.deepcopy(self.room)}
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)


class RouteFailureBroker(PatrolBroker):
    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float = 180,
        mutation: bool = False,
    ) -> Any:
        if name == "travel":
            self.calls.append((name, copy.deepcopy(arguments)))
            return {
                "arrived": False,
                "reason": "no floor anywhere on the west boundary",
                "log": [
                    {
                        "from": "Cor Noth",
                        "to": "Main gate to Cor Noth",
                        "via": "edge",
                        "ok": False,
                        "reason": "no floor anywhere on the west boundary",
                    }
                ],
            }
        return super().call_tool(
            name, arguments, timeout=timeout, mutation=mutation
        )


class PvpCoordinatorTests(unittest.TestCase):
    def test_planner_tool_requires_explicit_target_and_hides_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            coordinator = PvpCoordinator(cfg, lambda: PvpBroker())

            tool = coordinator.planner_tool()

            self.assertEqual(PVP_TOOL_NAME, tool["name"])
            self.assertNotIn("agent", tool["input_schema"]["properties"])
            self.assertEqual(["target"], tool["input_schema"]["required"])
            self.assertIn("deterministic", tool["description"])

    def test_target_must_be_visible_before_safety_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            broker = PvpBroker(target_visible=False)
            coordinator = PvpCoordinator(cfg, lambda: broker)

            result = coordinator.engage({"agent": "primary", "target": "Rival"})

            self.assertEqual("target_not_visible", result["outcome"])
            self.assertFalse(result["engaged"])
            self.assertFalse(any(name == "attack" for name, _ in broker.calls))
            self.assertFalse(any(name == "safety" and args.get("on") is False for name, args in broker.calls))
            self.assertEqual(["stop", "start"], [args["action"] for name, args in broker.calls if name == "autopilot"])
            self.assertEqual([], [args["action"] for name, args in broker.calls if name == "converse"])

    def test_seek_patrols_multiple_rooms_and_engages_from_fresh_local_view(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            broker = PatrolBroker()
            coordinator = PvpCoordinator(cfg, lambda: broker)

            result = coordinator.seek(
                {
                    "agent": "primary",
                    "target": "Rival",
                    "rooms": [50, 61],
                    "dwell_seconds": 0,
                    "max_rounds": 2,
                }
            )

            self.assertEqual("target_left_or_defeated", result["outcome"])
            self.assertTrue(result["engaged"])
            self.assertEqual([50, 61], result["search"]["route"])
            self.assertEqual([50, 61], [args["to"] for name, args in broker.calls if name == "travel"])
            self.assertEqual([50, 61], [item["room_id"] for item in result["search"]["rooms_visited"]])
            self.assertEqual(1, result["engagement"]["accepted_swings"])
            self.assertEqual(["stop", "start"], [args["action"] for name, args in broker.calls if name == "autopilot"])

    def test_seek_filters_city_rooms_and_uses_verified_wilderness_route(self) -> None:
        class EligiblePatrolBroker(PatrolBroker):
            def call_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
                result = super().call_tool(name, arguments, **kwargs)
                if name == "travel":
                    self.target_visible = int(arguments["to"]) == 574
                return result

        policies = {
            50: {"name": "The Streets of Tos", "flags": ["ROOM_GUILD_PK_ONLY"]},
            61: {"name": "East Ende", "flags": ["ROOM_GUILD_PK_ONLY"]},
            52: {"name": "Familiars", "flags": ["ROOM_NO_COMBAT"]},
            72: {"name": "The Adventurer's Hall of Tos", "flags": ["ROOM_NO_COMBAT"]},
            54: {"name": "First Royal Bank of Tos", "flags": ["ROOM_NO_COMBAT"]},
            51: {"name": "The Freelance Merchant and Menders Shop", "flags": ["ROOM_NO_COMBAT"]},
            53: {"name": "Frisconar's Mysticals", "flags": ["ROOM_NO_COMBAT"]},
            575: {"name": "The King's Way", "flags": []},
            574: {"name": "Main gate to Cor Noth", "flags": []},
            583: {"name": "Outskirts of Barloque", "flags": []},
            593: {"name": "Main gate of Barloque", "flags": []},
            603: {"name": "The Queen's Way", "flags": []},
        }
        with tempfile.TemporaryDirectory() as temporary:
            broker = EligiblePatrolBroker()
            coordinator = PvpCoordinator(
                config(Path(temporary)),
                lambda: broker,
                room_policy=lambda room_id: policies.get(room_id),
                guild_eligible=lambda: False,
            )

            result = coordinator.seek(
                {
                    "agent": "primary",
                    "target": "Rival",
                    "rooms": [50, 61],
                    "dwell_seconds": 0,
                    "max_rounds": 1,
                }
            )

            self.assertEqual([575, 574, 583, 593, 603], result["search"]["route"])
            self.assertEqual([50, 61], [item["room_id"] for item in result["search"]["skipped_rooms"]])
            self.assertFalse(result["search"]["guild_eligibility_verified"])
            self.assertEqual([575, 574], [args["to"] for name, args in broker.calls if name == "travel"])
            self.assertEqual("target_left_or_defeated", result["outcome"])

    def test_seek_aborts_on_first_failed_travel_and_preserves_route_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = RouteFailureBroker()
            coordinator = PvpCoordinator(config(Path(temporary)), lambda: broker)

            result = coordinator.seek(
                {
                    "agent": "primary",
                    "target": "Rival",
                    "rooms": [574, 583],
                    "dwell_seconds": 0,
                }
            )

            self.assertEqual("route_unavailable", result["outcome"])
            self.assertFalse(result["search"]["completed_patrol"])
            self.assertEqual([574], [args["to"] for name, args in broker.calls if name == "travel"])
            failure = result["route_failure"]
            self.assertEqual(574, failure["requested_room_id"])
            self.assertEqual(100, failure["actual_room_id"])
            self.assertEqual("Cor Noth", failure["failed_hop"]["from"])
            self.assertIn("west boundary", result["reason"])
            visit = result["search"]["rooms_visited"][0]
            self.assertEqual(574, visit["requested_room_id"])
            self.assertEqual(100, visit["room_id"])
            self.assertFalse(visit["arrived"])
            self.assertEqual(
                ["stop", "start"],
                [args["action"] for name, args in broker.calls if name == "autopilot"],
            )

    def test_guild_only_server_refusal_stops_after_one_unaccepted_swing(self) -> None:
        class GuildRefusalBroker(PvpBroker):
            def call_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
                if name == "attack":
                    self.calls.append((name, copy.deepcopy(arguments)))
                    return {
                        "swings": [
                            {
                                "swing": 1,
                                "messages": ["Only those in guilds may attack each other here."],
                            }
                        ]
                    }
                return super().call_tool(name, arguments, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            broker = GuildRefusalBroker()
            coordinator = PvpCoordinator(config(Path(temporary)), lambda: broker)

            result = coordinator.engage(
                {"agent": "primary", "target": "Rival", "max_rounds": 6}
            )

            self.assertEqual("guild_required", result["outcome"])
            self.assertEqual(0, result["accepted_swings"])
            self.assertEqual(1, len([1 for name, _ in broker.calls if name == "attack"]))
            self.assertNotIn("loot", result)

    def test_direct_engagement_is_prevented_by_indexed_guild_only_room_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = PvpBroker()
            coordinator = PvpCoordinator(
                config(Path(temporary)),
                lambda: broker,
                room_policy=lambda room_id: {
                    "room_id": room_id,
                    "name": "Guild-only city street",
                    "flags": ["ROOM_GUILD_PK_ONLY"],
                },
                guild_eligible=lambda: False,
            )

            result = coordinator.engage({"agent": "primary", "target": "Rival"})

            self.assertEqual("guild_required", result["outcome"])
            self.assertFalse(result["engaged"])
            self.assertFalse(any(name == "attack" for name, _ in broker.calls))
            self.assertFalse(any(name == "safety" and args.get("on") is False for name, args in broker.calls))

    def test_server_refusal_is_not_counted_as_an_accepted_attack_or_loot_phase(self) -> None:
        class EscapingBroker(PvpBroker):
            def call_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
                if name == "attack":
                    self.calls.append((name, copy.deepcopy(arguments)))
                    self.target_visible = False
                    return {"swings": [{"swing": 1, "result": "target is no longer here"}]}
                return super().call_tool(name, arguments, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            broker = EscapingBroker()
            coordinator = PvpCoordinator(config(Path(temporary)), lambda: broker)

            result = coordinator.engage({"agent": "primary", "target": "Rival"})

            self.assertEqual("target_escaped_before_attack", result["outcome"])
            self.assertEqual(0, result["accepted_swings"])
            self.assertNotIn("loot", result)

    def test_engagement_uses_exact_player_and_restores_background_systems(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            broker = PvpBroker(vanish_after=1)
            coordinator = PvpCoordinator(cfg, lambda: broker)

            result = coordinator.engage(
                {"agent": "primary", "target": "Rival", "max_rounds": 3, "swings_per_round": 1}
            )

            self.assertEqual("target_left_or_defeated", result["outcome"])
            self.assertTrue(result["engaged"])
            attack = next(args for name, args in broker.calls if name == "attack")
            self.assertEqual(700, attack["target"])
            self.assertFalse(any(name == "fight" for name, _ in broker.calls))
            self.assertIn("loot", result)
            safety_values = [args["on"] for name, args in broker.calls if name == "safety"]
            self.assertEqual([False, True], safety_values)
            self.assertTrue(result["cleanup"]["safety_restored"])
            self.assertNotIn("conversation_restored", result["cleanup"])
            self.assertTrue(result["cleanup"]["autopilot_restored"])
            restored = [args for name, args in broker.calls if name == "autopilot"][-1]
            self.assertEqual("", restored["hunt"])
            self.assertIsNone(restored["assigned_room"])
            self.assertFalse(restored["break_out_via_logoff"])

    def test_low_health_after_a_swing_triggers_spell_and_room_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            broker = PvpBroker()
            broker.health_after_attacks = [60]
            coordinator = PvpCoordinator(cfg, lambda: broker)

            result = coordinator.engage(
                {
                    "agent": "primary",
                    "target": "Rival",
                    "max_rounds": 3,
                    "disengage_at": 0.7,
                    "escape_spell": "blink",
                }
            )

            self.assertEqual("disengaged_low_health", result["outcome"])
            self.assertEqual("blink", result["disengagement"]["spell"]["spell"])
            self.assertTrue(result["disengagement"]["exit"]["result"]["left"])
            names = [name for name, _ in broker.calls]
            self.assertLess(names.index("attack"), names.index("cast"))
            self.assertLess(names.index("cast"), names.index("go_through"))

    def test_controller_exposes_dispatches_and_audits_pvp_composite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = PvpBroker(vanish_after=1)
                controller.broker = broker
                goal = controller.storage.submit_goal(goal_payload())["goal"]

                available = {tool["name"] for tool in controller._planner_tools()}
                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": PVP_TOOL_NAME,
                        "arguments": {"target": "Rival", "max_rounds": 2},
                        "rationale": "Engage the explicitly named nearby opponent.",
                    },
                )

                self.assertIn(PVP_TOOL_NAME, available)
                self.assertIn(PVP_SEEK_TOOL_NAME, available)
                self.assertEqual(PVP_TOOL_NAME, result["action"])
                self.assertEqual("target_left_or_defeated", result["result"]["outcome"])
                consequence = controller.storage.recent_consequences()[0]
                self.assertEqual("player_combat", consequence["action_class"])
                self.assertEqual("executed", consequence["status"])
                self.assertEqual(1, len(controller.storage.events(kinds=["pvp.engagement.completed"])["events"]))
                transaction = controller.storage.events(kinds=["property.transaction"])["events"]
                self.assertEqual(1, len(transaction))
                self.assertFalse(transaction[0]["data"]["approval_required"])
                self.assertEqual(1, len(controller.storage.events(kinds=["pvp.phase.completed"])["events"]))
            finally:
                controller.storage.close()

    def test_empty_loot_sweep_is_not_a_property_transaction(self) -> None:
        class EmptyLootBroker(PvpBroker):
            def call_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
                if name == "loot":
                    self.calls.append((name, copy.deepcopy(arguments)))
                    return {"taken": [], "note": "nothing to get"}
                return super().call_tool(name, arguments, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = EmptyLootBroker(vanish_after=1)
                controller.broker = broker
                goal = controller.storage.submit_goal(goal_payload())["goal"]
                controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": PVP_TOOL_NAME,
                        "arguments": {"target": "Rival", "max_rounds": 2},
                        "rationale": "Engage a freshly visible opponent.",
                    },
                )

                self.assertEqual([], controller.storage.events(kinds=["property.transaction"])["events"])
                sweep = controller.storage.events(kinds=["pvp.loot.completed"])["events"]
                self.assertEqual(0, sweep[0]["data"]["items_taken_count"])
                self.assertEqual(1, len(controller.storage.events(kinds=["pvp.phase.completed"])["events"]))
            finally:
                controller.storage.close()

    def test_pvp_history_does_not_override_an_explicit_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = PvpBroker(target_visible=True)
                controller.broker = broker
                observation = broker.observe()
                observation["look"]["objects"] = broker._look()["objects"]
                controller.last_observation = observation
                goal = controller.storage.submit_goal(goal_payload())["goal"]
                for target in ("Claude One", "Claude Two"):
                    controller.storage.emit_event(
                        "pvp.phase.completed",
                        f"Observed qualifying PvP phase against {target}",
                        goal_id=goal["id"],
                        data={"target": {"id": target, "name": target}, "accepted_swings": 1},
                    )

                patrol_blockers = controller._combat_preflight(
                    PVP_SEEK_TOOL_NAME,
                    {"rooms": [575, 574]},
                    observation,
                    goal,
                )
                self.assertFalse(
                    any(item["kind"] == "daily_pvp_initiation_cap_reached" for item in patrol_blockers)
                )

                observation["look"]["objects"][0]["attacking_self"] = True
                defense_blockers = controller._combat_preflight(
                    PVP_TOOL_NAME,
                    {"target": "Rival"},
                    observation,
                    goal,
                )
                self.assertFalse(
                    any(item["kind"] == "daily_pvp_initiation_cap_reached" for item in defense_blockers)
                )
            finally:
                controller.storage.close()

    def test_direct_opportunity_plan_rejects_patrol_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                controller.broker = PvpBroker(target_visible=True)
                goal = controller.storage.submit_goal(direct_pvp_goal())["goal"]
                observation = PvpBroker(target_visible=True).observe()
                observation["observed_at"] = time.time()
                observation["look"]["objects"] = PvpBroker(target_visible=True)._look()["objects"]
                controller.last_observation = observation
                grounding = controller.knowledge.validate_goal(goal)

                with self.assertRaisesRegex(ModelError, "pvp_engage only"):
                    controller._store_execution_plan(
                        goal,
                        {
                            "summary": "Patrol for Rival.",
                            "steps": [
                                {
                                    "id": "seek-rival",
                                    "outcome": "Search public rooms for Rival.",
                                    "tool": PVP_SEEK_TOOL_NAME,
                                    "verification": "A patrol finds Rival or completes.",
                                }
                            ],
                            "assumptions": [],
                        },
                        grounding=grounding,
                        revision=False,
                    )
            finally:
                controller.storage.close()

    def test_direct_opportunity_ends_immediately_when_target_is_no_longer_local(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = PvpBroker(target_visible=False)
                original_observe = broker.observe

                def fresh_observe() -> dict[str, Any]:
                    value = original_observe()
                    value["observed_at"] = time.time()
                    value["look"]["objects"] = []
                    return value

                broker.observe = fresh_observe  # type: ignore[method-assign]
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    direct_pvp_goal("direct-pvp-expired")
                )["goal"]

                result = controller.turn()

                self.assertTrue(result["opportunity_ended"])
                self.assertEqual("cancelled", controller.storage.goal(goal["id"])["status"])
                assessment = result["cancellation_assessment"]
                self.assertTrue(assessment["verified"]["opportunity_ended"])
                self.assertEqual(
                    1,
                    len(controller.storage.events(kinds=["pvp.opportunity.ended"])["events"]),
                )
                self.assertFalse(any(name == PVP_SEEK_TOOL_NAME for name, _ in broker.calls))
            finally:
                controller.storage.close()

    def test_direct_opportunity_cannot_cancel_as_ended_while_target_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = PvpBroker(target_visible=True)
                observation = broker.observe()
                observation["observed_at"] = time.time()
                observation["look"]["objects"] = broker._look()["objects"]
                controller.last_observation = observation
                goal = controller.storage.submit_goal(
                    direct_pvp_goal("direct-pvp-still-visible")
                )["goal"]

                with self.assertRaisesRegex(Exception, "GOAL_COMMITMENT_GUARD"):
                    controller.manage_goal(
                        {
                            "request_id": "not-ended",
                            "goal_id": goal["id"],
                            "expected_version": goal["version"],
                            "action": "cancel",
                            "cause": "opportunity_ended",
                        }
                    )
            finally:
                controller.storage.close()

    def test_controller_records_failed_patrol_as_route_lesson_not_completed_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = RouteFailureBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="route-failed-patrol")
                )["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": PVP_SEEK_TOOL_NAME,
                        "arguments": {
                            "target": "Rival",
                            "rooms": [574, 583],
                            "dwell_seconds": 0,
                        },
                        "rationale": "Run the explicitly requested hunt patrol.",
                    },
                )

                self.assertTrue(result["tactic_deferred"])
                self.assertEqual(
                    [], controller.storage.events(kinds=["pvp.search.completed"])["events"]
                )
                failed = controller.storage.events(kinds=["pvp.search.failed"])["events"]
                self.assertEqual(1, len(failed))
                self.assertEqual(574, failed[0]["data"]["route_failure"]["requested_room_id"])
                lessons = controller.storage.goal_lessons(goal_id=goal["id"])
                self.assertEqual("route_unavailable", lessons[0]["classification"])
                self.assertEqual("tactic", lessons[0]["scope"])
            finally:
                controller.storage.close()


if __name__ == "__main__":
    unittest.main()
