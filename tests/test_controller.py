from __future__ import annotations

import copy
import re
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from meridian_bot.broker import Tool, ToolCallError
from meridian_bot.contracts import CRITERION_KINDS
from meridian_bot.controller import BotController
from meridian_bot.criteria import CriteriaEvaluator
from meridian_bot.mcp import TOOLS
from meridian_bot.model import ModelError, PLANNER_SYSTEM
from meridian_bot.persona import PERSONA_FIELDS
from meridian_bot.simulator import SimulatedBroker
from meridian_bot.config import OnboardingConfig

from .helpers import config, goal_payload


def source_verify_safe_rooms(controller: BotController, *room_ids: int) -> None:
    """Give focused controller tests explicit source-derived room facts."""

    original = controller._pvp_room_policy
    safe = {int(room_id) for room_id in room_ids}

    def policy(room_id: int) -> dict[str, object] | None:
        if int(room_id) in safe:
            return {
                "known": True,
                "room_id": int(room_id),
                "name": f"Safe staging {int(room_id)}",
                "flags": ["ROOM_NO_COMBAT"],
                "evidence": {
                    "source_tier": "source-derived",
                    "source_ref": "test fixture",
                    "corpus_version": "test",
                },
            }
        return original(room_id)

    controller._pvp_room_policy = policy  # type: ignore[method-assign]


def with_safe_ending(
    plan: dict[str, object], room_id: int, *, step_id: str = "finish-safe"
) -> dict[str, object]:
    """Attach the required model-selected final safe travel to a test plan."""

    steps = [
        dict(step)
        for step in plan.get("steps", [])
        if isinstance(step, dict) and str(step.get("id") or "") != step_id
    ]
    steps.append(
        {
            "id": step_id,
            "outcome": f"Finish in source-verified safe room {room_id}.",
            "tool": "travel",
            "verification": f"Current room id is {room_id}.",
        }
    )
    return {
        **plan,
        "steps": steps,
        "safe_ending": {
            "room_id": room_id,
            "step_id": step_id,
            "rationale": "This safe room fits the test persona and verified route.",
        },
    }


class FixedModel:
    def manage_campaign(self, **kwargs: object) -> dict[str, object]:
        goal = kwargs.get("goal")
        constraints = (
            goal.get("constraints")
            if isinstance(goal, dict) and isinstance(goal.get("constraints"), dict)
            else {}
        )
        notes = str(constraints.get("operator_notes") or "")
        assigned_match = re.search(r"assigned_room\s*=\s*(\d+)", notes)
        hunt_match = re.search(r"hunt\s*=\s*([^;]+)", notes)
        if assigned_match and hunt_match:
            phase = {
                "kind": "farm",
                "objective": "Run the exact grounded farm recipe.",
                "success_criteria": list(goal.get("success_criteria", [])),
                "abandon_predicates": [],
                "budget": {"max_actions": 40, "max_minutes": 90},
                "context": {
                    "target": hunt_match.group(1).strip(),
                    "room": int(assigned_match.group(1)),
                    "use_safe_spots": "use_safe_spots=false" not in notes.casefold(),
                    "flee_below": 0.60,
                    "fight_above_vigor": 100,
                },
                "rationale": "Use the operator's grounded farm recipe.",
            }
        else:
            phase = {
                "kind": "general",
                "objective": str(goal.get("objective") if isinstance(goal, dict) else "Advance."),
                "success_criteria": list(goal.get("success_criteria", [])) if isinstance(goal, dict) else [],
                "abandon_predicates": [],
                "budget": {"max_actions": 24, "max_minutes": 45},
                "context": {},
                "rationale": "Use the compatibility test phase.",
            }
        return {
            "decision": "start_phase",
            "phase": phase,
            "rationale": "Select a bounded test phase.",
            "evidence": [],
        }

    def plan(self, **kwargs: object) -> dict[str, object]:
        grounded = kwargs.get("grounded_knowledge")
        safe_context = (
            grounded.get("safe_ending_candidates")
            if isinstance(grounded, dict)
            else None
        )
        candidates = (
            safe_context.get("candidates")
            if isinstance(safe_context, dict)
            else None
        )
        ending = (
            candidates[0]
            if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict)
            else {"room_id": 100}
        )
        ending_room = int(ending.get("room_id") or 100)
        if kwargs.get("execution_plan") is None:
            campaign = kwargs.get("campaign_context")
            active_phase = (
                campaign.get("active_phase")
                if isinstance(campaign, dict)
                else None
            )
            farm = isinstance(active_phase, dict) and active_phase.get("kind") == "farm"
            phase_context = (
                active_phase.get("context")
                if isinstance(active_phase, dict)
                and isinstance(active_phase.get("context"), dict)
                else {}
            )
            active_goal = kwargs.get("goal")
            constraints = (
                active_goal.get("constraints")
                if isinstance(active_goal, dict)
                and isinstance(active_goal.get("constraints"), dict)
                else {}
            )
            notes = str(constraints.get("operator_notes") or "")
            assigned_match = re.search(r"assigned_room\s*=\s*(\d+)", notes)
            hunt_match = re.search(r"hunt\s*=\s*([^;]+)", notes)
            if assigned_match and hunt_match:
                farm = True
                phase_context = {
                    **phase_context,
                    "room": int(assigned_match.group(1)),
                    "target": hunt_match.group(1).strip(),
                }
            work_step = (
                {
                    "id": "launch-goal-keeper",
                    "outcome": (
                        f"Launch the farm for {phase_context.get('target')} in assigned "
                        f"room {phase_context.get('room')}."
                    ),
                    "tool": "autopilot",
                    "verification": "Keeper reports the requested goal-owned farm.",
                }
                if farm
                else {
                    "id": "drop-item",
                    "outcome": "The requested item is dropped.",
                    "tool": "act",
                    "verification": "The item is absent from inventory.",
                }
            )
            return {
                "decision": "plan",
                "tool": None,
                "arguments": {},
                "rationale": "Plan before mutation.",
                "expected_observation": {},
                "proposal": None,
                "execution_plan": with_safe_ending({
                    "summary": "Drop the requested item and verify its absence.",
                    "steps": [work_step],
                    "assumptions": [],
                    "revision_reason": None,
                }, ending_room),
            }
        if isinstance(grounded, dict) and any(
            isinstance(grounded.get(key), dict)
            for key in ("goal_outcome_checkpoint", "phase_outcome_checkpoint")
        ):
            return {
                "decision": "act",
                "tool": "travel",
                "arguments": {"to": ending_room},
                "rationale": "Withdraw to the verified safe ending.",
                "expected_observation": {"room_id": ending_room},
                "proposal": None,
                "plan_step_id": "finish-safe",
            }
        return {"decision": "act", "tool": "act", "arguments": {"verb": "drop", "target": 1}, "rationale": "The goal explicitly requires the drop.", "expected_observation": {"inventory": "item absent"}, "proposal": None, "plan_step_id": "drop-item"}


class InvalidProposalModel:
    def plan(self, **_: object) -> dict[str, object]:
        return {
            "decision": "propose_goal",
            "tool": None,
            "arguments": {},
            "rationale": "Try a follow-up.",
            "expected_observation": {},
            "proposal": {"objective": "Do something later.", "success_criteria": []},
        }


class SafeDestinationModel(FixedModel):
    def plan(self, **kwargs: object) -> dict[str, object]:
        if kwargs.get("execution_plan") is None:
            return {
                "decision": "plan",
                "tool": None,
                "arguments": {},
                "rationale": "The safe destination is also the public goal.",
                "expected_observation": {},
                "proposal": None,
                "execution_plan": with_safe_ending(
                    {"summary": "Reach the requested safe destination.", "steps": []},
                    100,
                ),
            }
        return {
            "decision": "act",
            "tool": "travel",
            "arguments": {"to": 100},
            "rationale": "Reach the requested source-verified safe room.",
            "expected_observation": {"room_id": 100},
            "proposal": None,
            "plan_step_id": "finish-safe",
        }


class MissingProposalModel:
    def plan(self, **_: object) -> dict[str, object]:
        return {
            "decision": "propose_goal",
            "tool": None,
            "arguments": {},
            "rationale": "Use the existing proposal.",
            "expected_observation": {},
            "proposal": None,
        }


class WaitingModel:
    def __init__(self) -> None:
        self.feedback: list[object] = []

    def plan(self, **kwargs: object) -> dict[str, object]:
        self.feedback.append(kwargs.get("planner_feedback"))
        return {
            "decision": "wait",
            "tool": None,
            "arguments": {},
            "rationale": "A proposal is pending.",
            "expected_observation": {},
            "proposal": None,
        }


class GoalDraftModel:
    def __init__(self, drafts: list[dict[str, object]]) -> None:
        self.drafts = list(drafts)
        self.calls: list[dict[str, object]] = []

    def draft_goal(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.drafts.pop(0)


class InvalidRevisionModel:
    def plan(self, **_: object) -> dict[str, object]:
        return {
            "decision": "plan",
            "tool": None,
            "arguments": {},
            "rationale": "Replace the working plan.",
            "expected_observation": {},
            "proposal": None,
            "execution_plan": {
                "summary": "An invalid empty replacement.",
                "steps": [],
                "assumptions": [],
                "revision_reason": "No fresh invalidation.",
            },
        }


class SocialModel:
    def __init__(self) -> None:
        self.greetings: list[dict[str, object]] = []
        self.responses: list[dict[str, object]] = []

    def greet(self, **kwargs: object) -> dict[str, object]:
        self.greetings.append(kwargs)
        encounter = kwargs["encounter"]
        assert isinstance(encounter, dict)
        return {"reply": f"Ahoy, {encounter['name']}!", "ignore": False, "reason": ""}

    def respond(self, **kwargs: object) -> dict[str, object]:
        self.responses.append(kwargs)
        return {"reply": "Arrr, what'll ye have?", "ignore": False, "reason": ""}


class SocialBroker(SimulatedBroker):
    def __init__(self) -> None:
        super().__init__()
        self.objects = [
            {"id": 700, "name": "Blackstone", "is_player": True, "distance": 2},
            {"id": 800, "name": "Tos Barkeep", "is_player": False, "distance": 1},
        ]
        self.messages: list[dict[str, object]] = []

    def look(self) -> dict[str, object]:
        return {
            "room": {"num": 200, "name": "Tos Inn"},
            "self": {"name": "TestHero"},
            "vitals": {"health": {"value": 21, "max": 21}},
            "objects": [dict(item) for item in self.objects],
        }

    def call_tool(self, name: str, arguments: dict[str, object], *, timeout: float = 180, mutation: bool = False) -> object:
        self.calls.append((name, dict(arguments)))
        if name == "look":
            return self.look()
        if name == "inbox" and arguments.get("action") == "read":
            return {"messages": [dict(item) for item in self.messages]}
        if name == "inbox" and arguments.get("action") in {"reply", "resolve"}:
            message_id = arguments.get("id")
            self.messages = [item for item in self.messages if item.get("id") != message_id]
            return {"replied": arguments.get("action") == "reply"}
        if name == "say":
            return {"echoed": arguments.get("text")}
        return {"ok": True, "tool": name}


class NoProgressBroker(SimulatedBroker):
    def __init__(self) -> None:
        super().__init__()
        self.tools["travel"] = Tool(
            "travel",
            "Travel to a destination.",
            {
                "type": "object",
                "properties": {"agent": {"type": "string"}, "destination": {"type": "string"}},
                "required": ["agent", "destination"],
            },
        )

    def call_tool(self, name: str, arguments: dict[str, object], *, timeout: float = 180, mutation: bool = False) -> object:
        if name == "travel":
            self.calls.append((name, dict(arguments)))
            return {
                "arrived": False,
                "reason": "no route from Training Hall to Mausoleum in the graph",
                "now": {"room": {"num": 100, "name": "Training Hall"}},
            }
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)


class UnwieldableWeaponBroker(SimulatedBroker):
    def __init__(self) -> None:
        super().__init__()
        self.tools["equip_best"] = Tool(
            "equip_best",
            "Equip the best carried weapon.",
            {
                "type": "object",
                "properties": {"agent": {"type": "string"}},
                "required": ["agent"],
            },
        )

    def call_tool(self, name: str, arguments: dict[str, object], *, timeout: float = 180, mutation: bool = False) -> object:
        if name == "equip_best":
            self.calls.append((name, dict(arguments)))
            return {
                "wielding": None,
                "verified": False,
                "known_broken": 2,
                "note": "nothing wieldable in the pack; broken weapons are excluded",
            }
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)


class ShopBroker(SimulatedBroker):
    def __init__(self) -> None:
        super().__init__()
        self.tools["shop"] = Tool(
            "shop",
            "Buy an exact quoted item.",
            {
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "seller": {"type": "integer"},
                    "buy_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
                "required": ["agent", "seller", "buy_ids"],
            },
        )

    def call_tool(self, name: str, arguments: dict[str, object], *, timeout: float = 180, mutation: bool = False) -> object:
        if name == "shop":
            self.calls.append((name, dict(arguments)))
            self.inventory_items.append(
                {"id": 99, "name": "mace", "amount": 1, "can": ["use", "drop"]}
            )
            return {
                "seller": arguments["seller"],
                "bought": list(arguments["buy_ids"]),
                "got": ["mace (id 99) [get]"],
            }
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)


class EmptyMapBroker(SimulatedBroker):
    def __init__(self) -> None:
        super().__init__()
        self.tools["map"] = Tool(
            "map",
            "Search known rooms.",
            {
                "type": "object",
                "properties": {"agent": {"type": "string"}, "search": {"type": "string"}},
                "required": ["agent", "search"],
            },
        )

    def call_tool(self, name: str, arguments: dict[str, object], *, timeout: float = 180, mutation: bool = False) -> object:
        if name == "map":
            self.calls.append((name, dict(arguments)))
            return {"matches": []}
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)


class CatalogBroker(SimulatedBroker):
    def __init__(self) -> None:
        super().__init__()
        self.tools["merchants"] = Tool(
            "merchants",
            "Search merchant catalogs.",
            {
                "type": "object",
                "properties": {"search": {"type": "string"}},
                "required": ["search"],
            },
        )

    def call_tool(self, name: str, arguments: dict[str, object], *, timeout: float = 180, mutation: bool = False) -> object:
        if name == "merchants":
            self.calls.append((name, dict(arguments)))
            return {"matches": [{"merchant": "TosBlacksmith", "room": None, "sells": ["ChainArmor"]}]}
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)


class CombatBroker(SimulatedBroker):
    def __init__(self) -> None:
        super().__init__()
        self.tools["fight"] = Tool(
            "fight",
            "Fight a visible non-player creature.",
            {
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "target": {"type": "string"},
                    "rounds": {"type": "integer"},
                    "swings_per_round": {"type": "integer"},
                    "disengage_at": {"type": "number"},
                    "equip": {"type": "boolean"},
                    "loot": {"type": "boolean"},
                },
                "required": ["agent", "target"],
            },
        )


class PositionRefreshBroker(CombatBroker):
    def __init__(self) -> None:
        super().__init__()
        self.position_known = False
        self.tools["travel"] = Tool(
            "travel",
            "Travel to a room.",
            {
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "to": {"type": "integer"},
                },
                "required": ["agent", "to"],
            },
        )

    def call_tool(self, name: str, arguments: dict[str, object], *, timeout: float = 180, mutation: bool = False) -> object:
        self.calls.append((name, dict(arguments)))
        if name == "rest":
            self.position_known = False
            return {"standing": True}
        if name == "look":
            self.position_known = True
            return self.observe()["look"]
        if name == "travel":
            if not self.position_known:
                return {"arrived": False, "reason": "own position unknown — call look first"}
            self.room = {"num": int(arguments["to"]), "name": "First Royal Bank of Tos"}
            return {"arrived": True, "room": dict(self.room)}
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)


class LiveForegroundStatusBroker(CombatBroker):
    def call_tool(self, name: str, arguments: dict[str, object], *, timeout: float = 180, mutation: bool = False) -> object:
        if name == "status":
            self.calls.append((name, dict(arguments)))
            return {
                "character": "Simone",
                "where": dict(self.room),
                "position": {"col": 12, "row": 34},
                "vitals": dict(self.vitals),
            }
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)


class FlakyPositionRefreshBroker(PositionRefreshBroker):
    def __init__(self) -> None:
        super().__init__()
        self.look_count = 0

    def call_tool(self, name: str, arguments: dict[str, object], *, timeout: float = 180, mutation: bool = False) -> object:
        if name == "look":
            self.calls.append((name, dict(arguments)))
            self.look_count += 1
            self.position_known = self.look_count >= 2
            return self.observe()["look"]
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)


class BackgroundFarmBroker(SimulatedBroker):
    def __init__(self) -> None:
        super().__init__()
        self.farm_running = True
        self.farm_mode = "farm"
        self.farm_activity = "hunting: giant rat"
        self.farm_stalled = False
        self.farm_error: str | None = None
        self.last_death: dict[str, object] | None = None
        self.farm_did: dict[str, int] = {"kills": 0, "deaths": 0, "withdrawals": 0}
        self.farm_room = 586
        self.farm_hunt = "giant rat"
        self.farm_placement: dict[str, object] | None = None
        self.farm_safe_spot: object = False
        self.farm_journal: list[object] = []
        self.farm_flee_below = 0.75
        self.farm_fight_above_vigor = 100
        self.farm_inert: dict[str, object] | None = None
        self.soft_stop_inert = False

    def call_tool(self, name: str, arguments: dict[str, object], *, timeout: float = 180, mutation: bool = False) -> object:
        if name == "autopilot" and arguments.get("action") == "status":
            self.calls.append((name, dict(arguments)))
            return {
                "running": self.farm_running,
                "inert": self.farm_inert,
                "mode": self.farm_mode,
                "activity": self.farm_activity,
                "stalled": self.farm_stalled,
                "last_error": self.farm_error,
                "did": dict(self.farm_did),
                "placement": self.farm_placement or {"assigned_room": self.farm_room},
                "safe_spot": self.farm_safe_spot,
                "journal": list(self.farm_journal),
                "last_death": self.last_death,
                "policy": {
                    "hunt": self.farm_hunt,
                    "fleeBelow": self.farm_flee_below,
                    "fightAboveVigor": self.farm_fight_above_vigor,
                },
            }
        if name == "autopilot" and arguments.get("action") == "stop":
            self.calls.append((name, dict(arguments)))
            if self.soft_stop_inert:
                self.farm_inert = {
                    "inert": True,
                    "why": "asked to stop",
                }
                return {"running": True, "inert": dict(self.farm_inert)}
            self.farm_running = False
            return {"running": False, "stopped": True}
        if name == "autopilot" and arguments.get("action") == "start":
            self.calls.append((name, dict(arguments)))
            self.farm_running = True
            self.farm_inert = None
            self.farm_mode = str(arguments.get("mode") or self.farm_mode)
            return {"running": True, "mode": self.farm_mode}
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)


class DeathReconciliationBroker(CombatBroker):
    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False

    def call_tool(self, name: str, arguments: dict[str, object], *, timeout: float = 180, mutation: bool = False) -> object:
        if name == "fight" and not self.failed_once:
            self.failed_once = True
            self.calls.append((name, dict(arguments)))
            self.joined = False
            raise ToolCallError("agent primary is not in game")
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)

    def ensure_joined(self) -> dict[str, object]:
        self.joined = True
        self.room = {"num": 666, "name": "The Underworld"}
        self.vitals = {"health": {"current": 1, "max": 100}, "mana": {"current": 0, "max": 50}}
        return {"joined": True}


class OnboardingModel:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def plan_character(self, **kwargs: object) -> dict[str, str]:
        self.requests.append(kwargs)
        return {
            "stats": "caster",
            "loadout": "selfSufficient",
            "rationale": "The curious mystic persona favors a self-sufficient caster.",
        }


class OnboardingBroker(SimulatedBroker):
    def __init__(self, current_name: str) -> None:
        super().__init__()
        self.current_name = current_name
        self.tools["reroll"] = Tool(
            "reroll",
            "Plan or create a character.",
            {
                "type": "object",
                "properties": {
                    "action": {"enum": ["plan", "reroll"]},
                    "agent": {"type": "string"},
                    "name": {"type": "string"},
                    "stats": {"type": "string"},
                    "loadout": {"type": "string"},
                    "confirm": {"type": "boolean"},
                },
                "required": ["action"],
            },
        )

    def observe(self) -> dict[str, object]:
        value = super().observe()
        value["look"]["self"]["name"] = self.current_name
        value["status"]["character"] = self.current_name
        return value

    def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        timeout: float = 180,
        mutation: bool = False,
    ) -> object:
        if name == "reroll":
            self.calls.append((name, dict(arguments)))
            if arguments.get("action") == "plan":
                return {"ok": True, "name": arguments.get("name")}
            self.current_name = str(arguments["name"])
            return {"done": True, "stats_as_asked": True}
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)


class ControllerTests(unittest.TestCase):
    def test_onboarding_uses_llm_build_after_persona_and_then_waits_for_goals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = replace(
                config(Path(temporary)), onboarding=OnboardingConfig(enabled=True)
            )
            controller = BotController(value)
            try:
                broker = OnboardingBroker("User123456")
                model = OnboardingModel()
                controller.broker = broker
                controller.model = model  # type: ignore[assignment]

                persona = controller.set_persona(
                    {
                        "request_id": "onboard-sable",
                        "expected_version": 0,
                        "persona": {
                            "name": "Sable",
                            "character_voice": "A curious mystic.",
                        },
                    }
                )
                self.assertEqual("pending", persona["onboarding"]["status"])

                result = controller.turn()

                self.assertTrue(result["idle"])
                self.assertEqual("ready", result["onboarding"]["status"])
                self.assertTrue(result["onboarding"]["ready_for_goals"])
                self.assertEqual("Sable", broker.current_name)
                self.assertEqual(1, len(model.requests))
                rerolls = [call for call in broker.calls if call[0] == "reroll"]
                self.assertEqual(["plan", "reroll"], [call[1]["action"] for call in rerolls])
                self.assertEqual([], controller.storage.goals(["active", "queued"]))
                self.assertEqual(
                    1,
                    len(controller.storage.events(kinds=["onboarding.completed"])["events"]),
                )
            finally:
                controller.close()

    def test_goal_submission_is_rejected_until_onboarding_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = replace(
                config(Path(temporary)), onboarding=OnboardingConfig(enabled=True)
            )
            controller = BotController(value)
            try:
                with self.assertRaisesRegex(ValueError, "awaiting_persona"):
                    controller.submit_goal(goal_payload(request_id="too-early"))
                proposal = controller.storage.create_proposal(
                    {
                        "title": "Future work",
                        "objective": "Observe one later conversation.",
                        "success_criteria": [
                            {
                                "kind": "event_occurred",
                                "event_kind": "conversation.responded",
                            }
                        ],
                    },
                    "Wait for onboarding.",
                )
                with self.assertRaisesRegex(ValueError, "awaiting_persona"):
                    controller.decide_proposal(
                        {
                            "request_id": "accept-too-early",
                            "proposal_id": proposal["id"],
                            "action": "accept",
                        }
                    )
                self.assertEqual([], controller.storage.goals(["active", "queued"]))
            finally:
                controller.close()

    def test_onboarding_preserves_established_character_without_explicit_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = replace(
                config(Path(temporary)), onboarding=OnboardingConfig(enabled=True)
            )
            controller = BotController(value)
            try:
                broker = OnboardingBroker("EstablishedHero")
                controller.broker = broker
                controller.model = OnboardingModel()  # type: ignore[assignment]
                controller.set_persona(
                    {
                        "request_id": "preserve-existing",
                        "expected_version": 0,
                        "persona": {"name": "Sable"},
                    }
                )

                result = controller.turn()

                self.assertEqual(
                    "awaiting_existing_character_confirmation",
                    result["onboarding"]["status"],
                )
                self.assertEqual("EstablishedHero", broker.current_name)
                self.assertFalse(any(name == "reroll" for name, _ in broker.calls))
            finally:
                controller.close()

    def test_onboarding_replaces_established_character_only_after_explicit_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = replace(
                config(Path(temporary)), onboarding=OnboardingConfig(enabled=True)
            )
            controller = BotController(value)
            try:
                broker = OnboardingBroker("EstablishedHero")
                model = OnboardingModel()
                controller.broker = broker
                controller.model = model  # type: ignore[assignment]
                controller.set_persona(
                    {
                        "request_id": "preserve-first",
                        "expected_version": 0,
                        "persona": {"name": "Sable"},
                    }
                )
                controller.turn()

                controller.set_persona(
                    {
                        "request_id": "replace-explicitly",
                        "expected_version": 1,
                        "persona": {"name": "Sable"},
                        "replace_existing_character": True,
                    }
                )
                result = controller.turn()

                self.assertEqual("ready", result["onboarding"]["status"])
                self.assertEqual("Sable", broker.current_name)
                self.assertEqual(1, len(model.requests))
            finally:
                controller.close()

    def test_persona_rejects_unknown_or_non_boolean_onboarding_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                with self.assertRaisesRegex(ValueError, "unknown persona fields"):
                    controller.set_persona(
                        {
                            "request_id": "bad-field",
                            "expected_version": 0,
                            "persona": {"name": "Sable"},
                            "unexpected": True,
                        }
                    )
                with self.assertRaisesRegex(ValueError, "must be a boolean"):
                    controller.set_persona(
                        {
                            "request_id": "bad-flag",
                            "expected_version": 0,
                            "persona": {"name": "Sable"},
                            "replace_existing_character": "yes",
                        }
                    )
            finally:
                controller.close()

    def test_idle_turn_completes_satisfied_paused_goal_without_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                controller.broker = SimulatedBroker()
                controller.last_observation = controller.broker.observe()
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="inactive-completion",
                        title="Reach 33 max HP",
                        objective="Raise maximum HP to at least 33.",
                        success_criteria=[
                            {
                                "id": "max-hp-33",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 33,
                            }
                        ],
                    )
                )["goal"]
                controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {"summary": "Finish at the selected safe room.", "steps": []},
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )
                controller.storage.manage_goal(
                    {
                        "request_id": "pause-inactive-completion",
                        "goal_id": goal["id"],
                        "expected_version": goal["version"],
                        "action": "pause",
                    }
                )

                result = controller.turn()

                current = controller.storage.goal(goal["id"])
                self.assertTrue(result["idle"])
                self.assertEqual([goal["id"]], result["reconciled_goal_ids"])
                self.assertEqual("succeeded", current["status"])
                criterion = current["completion"]["criteria"][0]
                self.assertTrue(criterion["met"])
                self.assertIn("100 >= 33", criterion["detail"])
                evidence_events = [
                    item
                    for item in controller.storage.events(
                        kinds=["goal.inactive_completion_reconciled"]
                    )["events"]
                    if item.get("data", {}).get("reconciled_from") == "paused"
                ]
                self.assertEqual(1, len(evidence_events))
                self.assertFalse(evidence_events[0]["data"]["model_used"])
            finally:
                controller.storage.close()

    def test_idle_turn_leaves_unmet_paused_goal_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                controller.broker = SimulatedBroker()
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="inactive-incomplete",
                        title="Reach 101 max HP",
                        objective="Raise maximum HP to at least 101.",
                        success_criteria=[
                            {
                                "id": "max-hp-101",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 101,
                            }
                        ],
                    )
                )["goal"]
                controller.storage.manage_goal(
                    {
                        "request_id": "pause-inactive-incomplete",
                        "goal_id": goal["id"],
                        "expected_version": goal["version"],
                        "action": "pause",
                    }
                )

                result = controller.turn()

                self.assertTrue(result["idle"])
                self.assertEqual([], result["reconciled_goal_ids"])
                self.assertEqual("paused", controller.storage.goal(goal["id"])["status"])
            finally:
                controller.storage.close()

    def test_character_progress_emits_hp_learning_and_five_point_milestones(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                baseline = {
                    "status": {"vitals": {"health": {"current": 21, "max": 21}}},
                    "abilities": {
                        "skills": [],
                        "spells": [{"name": "Blink", "ability": 5}],
                    },
                }
                controller._record_character_progress(baseline)
                self.assertEqual([], controller.storage.events()["events"])

                changed = {
                    "status": {"vitals": {"health": {"current": 22, "max": 22}}},
                    "abilities": {
                        "skills": [{"name": "Dodge", "ability": 1}],
                        "spells": [{"name": "Blink", "ability": 6}],
                    },
                }
                controller._record_character_progress(changed)
                kinds = [item["kind"] for item in controller.storage.events()["events"]]
                self.assertEqual({"progress.hp_gained", "progress.skill_learned"}, set(kinds))

                milestones = {
                    "status": {"vitals": {"health": {"current": 22, "max": 22}}},
                    "abilities": {
                        "skills": [{"name": "Dodge", "ability": 5}],
                        "spells": [{"name": "Blink", "ability": 10}],
                    },
                }
                controller._record_character_progress(milestones)
                controller._record_character_progress(milestones)
                events = controller.storage.events()["events"]
                self.assertEqual(1, sum(item["kind"] == "progress.skill_milestone" for item in events))
                self.assertEqual(1, sum(item["kind"] == "progress.spell_milestone" for item in events))
            finally:
                controller.storage.close()

    @staticmethod
    def _set_social_persona(controller: BotController) -> None:
        controller.storage.set_persona(
            {
                "request_id": "social-persona",
                "expected_version": 0,
                "persona": {
                    "name": "TestHero",
                    "character_voice": "A swaggering pirate.",
                    "speech_style": ["brief pirate banter"],
                    "max_reply_characters": 220,
                },
            }
        )

    def test_listener_sends_all_player_and_npc_speech_to_model_tier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                controller.broker = broker

                controller._start_conversation_listener()

                arguments = next(arguments for name, arguments in broker.calls if name == "converse")
                self.assertFalse(arguments["ack"])
                self.assertFalse(arguments["small_talk"])
                self.assertFalse(arguments["face_speaker"])
                self.assertTrue(arguments["escalate"])
                self.assertEqual(20, arguments["replies_per_min"])
                self.assertEqual(12, arguments["per_speaker_per_min"])
            finally:
                controller.close()

    def test_visible_player_gets_one_proactive_greeting_but_silent_npc_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SocialBroker()
                model = SocialModel()
                controller.broker = broker
                controller.model = model  # type: ignore[assignment]
                self._set_social_persona(controller)

                controller._greeting_turn(broker.look())
                controller._greeting_turn(broker.look())

                self.assertEqual(20, controller.config.controller.greetings_per_minute)
                self.assertEqual(1, len(model.greetings))
                self.assertEqual("Blackstone", model.greetings[0]["encounter"]["name"])
                say_calls = [arguments for name, arguments in broker.calls if name == "say"]
                self.assertEqual(["Ahoy, Blackstone!"], [call["text"] for call in say_calls])
            finally:
                controller.close()

    def test_npc_speech_is_answered_with_per_speaker_conversation_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SocialBroker()
                model = SocialModel()
                controller.broker = broker
                controller.model = model  # type: ignore[assignment]
                self._set_social_persona(controller)
                broker.messages = [
                    {
                        "id": "primary:1",
                        "from": {"name": "Tos Barkeep", "object_id": 800, "is_peer": False},
                        "channel": "say",
                        "utterance": "What'll it be?",
                    }
                ]

                controller._conversation_turn(look=broker.look())
                broker.messages = [
                    {
                        "id": "primary:2",
                        "from": {"name": "Tos Barkeep", "object_id": 800, "is_peer": False},
                        "channel": "say",
                        "utterance": "Rum or ale?",
                    }
                ]
                controller._conversation_turn(look=broker.look())

                self.assertEqual("npc", model.responses[0]["message"]["speaker_kind"])
                second_history = model.responses[1]["history"]
                self.assertEqual(["speaker", "assistant"], [entry["role"] for entry in second_history])
                replies = [arguments for name, arguments in broker.calls if name == "inbox" and arguments.get("action") == "reply"]
                self.assertEqual(2, len(replies))
                self.assertTrue(all(arguments["text"].startswith("Arrr") for arguments in replies))
            finally:
                controller.close()

    def test_character_name_uses_harness_status_character(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                controller.broker = broker
                observation = broker.observe()
                observation["status"]["character"] = "TestHero"
                controller.last_observation = observation

                self.assertEqual("TestHero", controller.status()["game"]["character_name"])
                self.assertEqual("TestHero", controller.public_game_context()["character"])
            finally:
                controller.storage.close()

    def test_planner_prompt_documents_goal_criterion_fields(self) -> None:
        self.assertIn("never propose a materially equivalent", PLANNER_SYSTEM)
        self.assertIn("Proposals are inert optional future goals", PLANNER_SYSTEM)
        self.assertIn("A pending proposal is never a reason to wait", PLANNER_SYSTEM)
        self.assertIn("Prefer exact numeric room ids", PLANNER_SYSTEM)
        self.assertIn("event_occurred=[after_cursor, event_kind, id, kind]", PLANNER_SYSTEM)
        self.assertIn("Never invent combat.kill", PLANNER_SYSTEM)
        self.assertIn("Count the steps before returning JSON", PLANNER_SYSTEM)
        self.assertIn("`detail`, `met`, and other evaluation-result fields are never inputs", PLANNER_SYSTEM)
        self.assertIn("never add a no_cheating constraint", PLANNER_SYSTEM)
        self.assertIn("progression recommendations as eligibility evidence", PLANNER_SYSTEM)
        self.assertIn("rounds=1, swings_per_round=1", PLANNER_SYSTEM)
        self.assertIn("Below 30 max HP, assume player combat is unavailable", PLANNER_SYSTEM)
        self.assertIn("financial_context to decide whether banking belongs", PLANNER_SYSTEM)
        self.assertIn("value never block travel or combat", PLANNER_SYSTEM)
        self.assertIn("grounded_knowledge.hunt_room_options", PLANNER_SYSTEM)
        self.assertIn("farm_room_scorecard", PLANNER_SYSTEM)
        self.assertIn("safe_spot_evidence", PLANNER_SYSTEM)
        self.assertIn("Keeper banking is optional", PLANNER_SYSTEM)
        self.assertIn("cap is filled by", PLANNER_SYSTEM)
        self.assertIn("durable goal's explicit use_safe_spots value", PLANNER_SYSTEM)
        self.assertIn("disproves that square, not the entire room", PLANNER_SYSTEM)
        self.assertIn("effective_use_safe_spots", PLANNER_SYSTEM)
        self.assertIn("does not condemn separately evidenced open-field farming", PLANNER_SYSTEM)
        self.assertIn("`pvp_engage only` defines a closed, expiring local opportunity", PLANNER_SYSTEM)
        self.assertIn("If pvp_seek reports route_unavailable or travel_error", PLANNER_SYSTEM)
        self.assertIn("phase_outcome_checkpoint", PLANNER_SYSTEM)

    def test_exactly_six_mcp_tools(self) -> None:
        self.assertEqual({"status", "submit_goal", "manage_goal", "proposals", "persona", "events"}, {tool["name"] for tool in TOOLS})

    def test_persona_mcp_schema_documents_the_validated_shape(self) -> None:
        tool = next(tool for tool in TOOLS if tool["name"] == "persona")
        schema = tool["inputSchema"]
        persona = schema["properties"]["persona"]
        self.assertEqual(PERSONA_FIELDS, set(persona["properties"]))
        self.assertEqual(["name"], persona["required"])
        self.assertFalse(persona["additionalProperties"])
        self.assertIn("Do not send a top-level version field", schema["description"])

    def test_every_mcp_schema_is_closed_and_documented_recursively(self) -> None:
        self.assertEqual(set(CRITERION_KINDS), CriteriaEvaluator.SUPPORTED)
        for tool in TOOLS:
            self.assertGreater(len(tool["description"]), 40, tool["name"])
            self._assert_documented_schema(tool["inputSchema"], tool["name"])

    def _assert_documented_schema(self, schema: dict[str, object], path: str) -> None:
        schema_type = schema.get("type")
        if schema_type == "object":
            properties = schema.get("properties")
            self.assertIsInstance(properties, dict, path)
            self.assertTrue(properties, path)
            self.assertFalse(schema.get("additionalProperties", True), path)
            for name, child in properties.items():
                self.assertIsInstance(child, dict, f"{path}.{name}")
                self.assertTrue(child.get("description"), f"{path}.{name}")
                self._assert_documented_schema(child, f"{path}.{name}")
        if schema_type == "array":
            items = schema.get("items")
            self.assertIsInstance(items, dict, f"{path}[]")
            self.assertTrue(items, f"{path}[]")
            self._assert_documented_schema(items, f"{path}[]")

    def test_consequential_action_executes_without_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                simulator = SimulatedBroker()
                controller.broker = simulator
                controller.model = FixedModel()  # type: ignore[assignment]
                goal = controller.storage.submit_goal(goal_payload())["goal"]
                planned = controller.turn()
                self.assertTrue(planned["planned"])
                result = controller.turn()
                self.assertEqual("act", result["action"])
                self.assertEqual([], simulator.inventory_items)
                self.assertEqual("succeeded", controller.storage.goal(goal["id"])["status"])
                consequences = controller.storage.recent_consequences()
                self.assertEqual("executed", consequences[0]["status"])
                self.assertFalse(any(name in {"approve", "permission"} for name, _ in simulator.calls))
            finally:
                controller.storage.close()

    def test_goal_outcome_is_latched_until_model_selected_safe_ending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                simulator = SimulatedBroker()
                simulator.room = {"num": 200, "name": "Unsafe test room"}
                controller.broker = simulator
                controller.model = FixedModel()  # type: ignore[assignment]
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="safe-ending-latch")
                )["goal"]

                planned = controller.turn()
                self.assertEqual(100, planned["execution_plan"]["safe_ending"]["room_id"])

                action = controller.turn()
                self.assertEqual("act", action["action"])
                current = controller.storage.goal(goal["id"])
                self.assertEqual("active", current["status"])
                self.assertTrue(controller.status()["goal"]["outcome_latched"])

                returned = controller.turn()
                self.assertEqual("travel", returned["action"])
                self.assertEqual(100, simulator.room["num"])
                self.assertEqual("succeeded", controller.storage.goal(goal["id"])["status"])
            finally:
                controller.storage.close()

    def test_campaign_phase_outcome_is_latched_until_safe_ending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                simulator = SimulatedBroker()
                simulator.room = {"num": 200, "name": "Unsafe test room"}
                controller.broker = simulator
                controller.model = FixedModel()  # type: ignore[assignment]
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="safe-phase-ending-latch")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                phase = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "general",
                        "objective": "Verify the bounded phase, then withdraw safely.",
                        "success_criteria": [
                            {
                                "id": "healthy-enough",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 1,
                            }
                        ],
                        "abandon_predicates": [],
                        "budget": {"max_actions": 8, "max_minutes": 30},
                    },
                    mode="start",
                )
                controller.last_observation = simulator.observe()
                retained_plan = controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {
                            "summary": "Finish the verified phase safely.",
                            "steps": [],
                        },
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )

                action = controller.turn()

                self.assertEqual("travel", action["action"])
                self.assertEqual(100, simulator.room["num"])
                self.assertEqual(
                    phase["id"],
                    controller.storage.active_campaign_phase(run["id"])["id"],
                )
                self.assertEqual(
                    retained_plan["safe_ending"],
                    controller._execution_plan(goal)["safe_ending"],
                )

                completed = controller.turn()

                self.assertTrue(completed["campaign_phase_completed"])
                self.assertEqual(
                    "succeeded",
                    controller.storage.campaign_phases(run["id"])[0]["status"],
                )
                self.assertIsNone(controller._execution_plan(goal))
            finally:
                controller.storage.close()

    def test_execution_plan_rejects_missing_or_unverified_safe_ending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                controller.broker = SimulatedBroker()
                controller.last_observation = controller.broker.observe()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="safe-ending-validation")
                )["goal"]
                base = {
                    "summary": "Drop the item, then withdraw safely.",
                    "steps": [
                        {
                            "id": "drop-item",
                            "outcome": "Drop the requested item.",
                            "tool": "act",
                            "verification": "The item is absent.",
                        }
                    ],
                }

                with self.assertRaisesRegex(ModelError, "safe_ending is required"):
                    controller._store_execution_plan(
                        goal,
                        base,
                        grounding=controller.knowledge.validate_goal(goal),
                        revision=False,
                    )
                with self.assertRaisesRegex(ModelError, "not source-verified"):
                    controller._store_execution_plan(
                        goal,
                        with_safe_ending(base, 999),
                        grounding=controller.knowledge.validate_goal(goal),
                        revision=False,
                    )
                nonfinal = with_safe_ending(base, 100)
                nonfinal["steps"].append(
                    {
                        "id": "work-after-safety",
                        "outcome": "Do more work after the claimed ending.",
                        "tool": "act",
                        "verification": "More work happened.",
                    }
                )
                with self.assertRaisesRegex(ModelError, "final actionable"):
                    controller._store_execution_plan(
                        goal,
                        nonfinal,
                        grounding=controller.knowledge.validate_goal(goal),
                        revision=False,
                    )
                nontravel = {
                    **base,
                    "safe_ending": {
                        "room_id": 100,
                        "step_id": "drop-item",
                        "rationale": "Claim the current safe room without final travel.",
                    },
                }
                with self.assertRaisesRegex(ModelError, "must use the travel tool"):
                    controller._store_execution_plan(
                        goal,
                        nontravel,
                        grounding=controller.knowledge.validate_goal(goal),
                        revision=False,
                    )

                accepted = controller._store_execution_plan(
                    goal,
                    with_safe_ending(base, 100),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )
                self.assertEqual(100, accepted["safe_ending"]["room_id"])
                self.assertEqual("finish-safe", accepted["steps"][-1]["id"])
            finally:
                controller.storage.close()

    def test_safe_ending_travel_may_also_satisfy_the_public_location_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                simulator = SimulatedBroker()
                simulator.room = {"num": 200, "name": "Starting room"}
                controller.broker = simulator
                controller.model = SafeDestinationModel()  # type: ignore[assignment]
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="safe-destination-is-goal",
                        objective="Reach safe room 100.",
                        success_criteria=[
                            {
                                "id": "at-safe-room",
                                "kind": "location_reached",
                                "location": "Safe room 100",
                                "room_id": 100,
                            }
                        ],
                    )
                )["goal"]

                self.assertTrue(controller.turn()["planned"])
                result = controller.turn()

                self.assertEqual("travel", result["action"])
                self.assertEqual("succeeded", controller.storage.goal(goal["id"])["status"])
            finally:
                controller.storage.close()

    def test_execution_plan_drops_toolless_monitor_step_when_qwen_returns_nine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                broker = SimulatedBroker()
                controller.broker = broker
                controller.last_observation = broker.observe()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="nine-step-plan")
                )["goal"]
                steps = [
                    {
                        "id": f"action-{index}",
                        "outcome": f"Complete bounded action {index}.",
                        "tool": "act",
                        "verification": "Observe the requested inventory change.",
                    }
                    for index in range(7)
                ]
                steps.append(
                    {
                        "id": "wait-for-result",
                        "outcome": "Wait for the controller to observe completion.",
                        "tool": None,
                        "verification": "The controller will monitor criteria.",
                    }
                )

                stored = controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {"summary": "Execute the bounded goal.", "steps": steps},
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )

                self.assertEqual(8, len(stored["steps"]))
                self.assertEqual(
                    "removed_controller_owned_monitoring_steps",
                    stored["normalizations"][0]["kind"],
                )
            finally:
                controller.storage.close()

    def test_execution_plan_drops_toolless_monitor_step_when_total_is_eight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                broker = SimulatedBroker()
                controller.broker = broker
                controller.last_observation = broker.observe()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="eight-step-plan-with-monitor")
                )["goal"]
                steps = [
                    {
                        "id": f"action-{index}",
                        "outcome": f"Complete bounded action {index}.",
                        "tool": "act",
                        "verification": "Observe the requested inventory change.",
                    }
                    for index in range(6)
                ]
                steps.append(
                    {
                        "id": "wait-for-result",
                        "outcome": "Wait for the controller to observe completion.",
                        "tool": None,
                        "verification": "The controller will monitor criteria.",
                    }
                )

                stored = controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {"summary": "Execute the bounded goal.", "steps": steps},
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )

                self.assertEqual(7, len(stored["steps"]))
                self.assertTrue(all(step["tool"] is not None for step in stored["steps"]))
                self.assertEqual(
                    "removed_controller_owned_monitoring_steps",
                    stored["normalizations"][0]["kind"],
                )
            finally:
                controller.storage.close()

    def test_farm_execution_plan_drops_later_return_home_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 52)
                broker = SimulatedBroker()
                broker.tools["walk_to"] = Tool(
                    "walk_to",
                    "Walk to one square in the current room.",
                    {"type": "object", "properties": {}},
                )
                controller.broker = broker
                broker.room = {"num": 52, "name": "Familiars"}
                controller.last_observation = broker.observe()
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="farm-plan-with-later-return",
                        title="Raise maximum HP",
                        objective="Raise maximum HP to 101.",
                        success_criteria=[
                            {
                                "id": "hp-101",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 101,
                            }
                        ],
                    )
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "farm",
                        "objective": "Run the bounded farm phase.",
                        "success_criteria": list(goal["success_criteria"]),
                        "context": {
                            "target": "ant",
                            "room": 26,
                            "use_safe_spots": False,
                        },
                    },
                    mode="start",
                )

                stored = controller._store_execution_plan(
                    goal,
                    with_safe_ending({
                        "summary": "Farm now and return home after the strategic goal.",
                        "steps": [
                            {
                                "id": "launch-farm",
                                "outcome": "Launch the bounded ant farm in assigned room 26.",
                                "tool": "autopilot",
                                "verification": "Keeper reports the requested farm policy.",
                            },
                            {
                                "id": "finish-at-tos-bar",
                                "outcome": "Walk to the Tos Inn bar after completion.",
                                "tool": "walk_to",
                                "verification": "TestHero is beside the bar.",
                            },
                        ],
                    }, 52),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )

                self.assertEqual(
                    ["launch-farm", "finish-safe"],
                    [step["id"] for step in stored["steps"]],
                )
                self.assertEqual(
                    "removed_out_of_phase_steps",
                    stored["normalizations"][0]["kind"],
                )
                self.assertEqual("walk_to", stored["normalizations"][0]["removed"][0]["tool"])
            finally:
                controller.storage.close()

    def test_execution_plan_drops_zero_currency_deposit_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 52)
                broker = SimulatedBroker()
                broker.tools["travel"] = Tool(
                    "travel", "Travel to a room.", {"type": "object", "properties": {}}
                )
                broker.tools["bank"] = Tool(
                    "bank", "Manage carried currency.", {"type": "object", "properties": {}}
                )
                controller.broker = broker
                controller.last_observation = broker.observe()
                payload = goal_payload(request_id="zero-currency-plan")
                payload["constraints"] = {
                    **payload.get("constraints", {}),
                    "bank_before_hazard": True,
                }
                goal = controller.storage.submit_goal(payload)["goal"]

                stored = controller._store_execution_plan(
                    goal,
                    {
                        "summary": "Prepare in Tos and execute the bounded goal.",
                        "steps": [
                            {
                                "id": "travel-to-tos",
                                "outcome": "Travel safely to Tos.",
                                "tool": "travel",
                                "verification": "TestHero is in Tos.",
                            },
                            {
                                "id": "deposit-before-danger",
                                "outcome": "Deposit carried currency before hazardous travel.",
                                "tool": "bank",
                                "verification": "Carried currency is zero.",
                            },
                            {
                                "id": "travel-home",
                                "outcome": "Travel to the Tos Inn.",
                                "tool": "travel",
                                "verification": "TestHero is in room 52.",
                            },
                        ],
                        "safe_ending": {
                            "room_id": 52,
                            "step_id": "travel-home",
                            "rationale": "The quiet inn fits the test persona.",
                        },
                    },
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )

                self.assertEqual(
                    ["travel-to-tos", "travel-home"],
                    [step["id"] for step in stored["steps"]],
                )
                self.assertEqual(
                    "removed_already_satisfied_zero_currency_deposit",
                    stored["normalizations"][0]["kind"],
                )
            finally:
                controller.storage.close()

    def test_existing_plan_repairs_zero_currency_deposit_and_toolless_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                broker = SimulatedBroker()
                broker.tools["bank"] = Tool(
                    "bank", "Manage carried currency.", {"type": "object", "properties": {}}
                )
                controller.broker = broker
                broker.inventory_items.append(
                    {"id": 2, "name": "shilling", "amount": 10, "can": []}
                )
                controller.last_observation = broker.observe()
                payload = goal_payload(request_id="legacy-zero-currency-plan")
                payload["constraints"] = {
                    **payload.get("constraints", {}),
                    "bank_before_hazard": True,
                }
                goal = controller.storage.submit_goal(payload)["goal"]
                stored = controller._store_execution_plan(
                    goal,
                    with_safe_ending({
                        "summary": "Deposit money, act, then wait for verification.",
                        "steps": [
                            {
                                "id": "deposit",
                                "outcome": "Deposit carried currency before hazardous travel.",
                                "tool": "bank",
                                "verification": "Currency is deposited.",
                            },
                            {
                                "id": "act",
                                "outcome": "Perform the bounded action.",
                                "tool": "act",
                                "verification": "The action is observed.",
                            },
                        ],
                    }, 100),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )
                values = controller.storage.get_runtime("goal_execution_plans_v1", {})
                values[goal["id"]] = {
                    **stored,
                    "steps": [
                        *stored["steps"],
                        {
                            "id": "wait",
                            "outcome": "Wait for completion.",
                            "tool": None,
                            "verification": "The controller monitors completion.",
                        },
                    ],
                }
                controller.storage.set_runtime("goal_execution_plans_v1", values)
                broker.inventory_items = [
                    item for item in broker.inventory_items if item["name"] != "shilling"
                ]
                controller.last_observation = broker.observe()
                controller._set_planner_feedback(
                    goal,
                    "The selected action tool did not match the declared execution_plan step tool.",
                )

                repaired = controller._execution_plan(goal)

                self.assertEqual(
                    ["act", "finish-safe"],
                    [step["id"] for step in repaired["steps"]],
                )
                self.assertEqual(
                    "repaired_legacy_controller_owned_steps",
                    repaired["normalizations"][-1]["kind"],
                )
                self.assertIsNone(controller._planner_feedback(goal))
            finally:
                controller.storage.close()

    def test_goal_drafter_repairs_invalid_model_schema_before_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            model = GoalDraftModel(
                [
                    {
                        "title": "Unverifiable draft",
                        "objective": "Reach the bank.",
                        "success_criteria": [],
                        "constraints": {},
                        "priority": 50,
                        "activation": "queue",
                    },
                    {
                        "title": "Reach the bank",
                        "objective": "Reach the bank.",
                        "success_criteria": [
                            {"id": "done", "kind": "operator_confirmed"}
                        ],
                        "constraints": {"avoid_death": True},
                        "priority": 70,
                        "activation": "queue",
                    },
                ]
            )
            controller.model = model  # type: ignore[assignment]
            try:
                result = controller.draft_goal({"prompt": "Reach the bank safely."})

                self.assertEqual("Reach the bank", result["goal"]["title"])
                self.assertEqual(70, result["goal"]["priority"])
                self.assertEqual(2, len(model.calls))
                feedback = model.calls[1]["validation_feedback"]
                self.assertIsInstance(feedback, list)
                self.assertEqual("INVALID_GOAL_SCHEMA", feedback[0]["code"])
            finally:
                controller.storage.close()

    def test_submit_goal_returns_existing_equivalent_open_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                controller.last_observation = SimulatedBroker().observe()
                first = controller.submit_goal(
                    goal_payload(request_id="canonical-goal-one")
                )
                duplicate = controller.submit_goal(
                    goal_payload(
                        request_id="canonical-goal-two",
                        title="Different wording for the same outcome",
                    )
                )

                self.assertTrue(duplicate["deduplicated"])
                self.assertEqual(first["goal"]["id"], duplicate["goal"]["id"])
                self.assertEqual(
                    "GOAL_ALREADY_IN_PROGRESS", duplicate["warnings"][0]["code"]
                )
                self.assertEqual(1, len(controller.storage.goals(["active"])))
            finally:
                controller.storage.close()

    def test_resuming_goal_retires_stale_plan_and_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="resume-fresh-plan")
                )["goal"]
                controller.manage_goal(
                    {
                        "request_id": "pause-before-fresh-plan",
                        "goal_id": goal["id"],
                        "action": "pause",
                    }
                )
                controller.storage.set_runtime(
                    "goal_execution_plans_v1",
                    {
                        goal["id"]: {
                            "summary": "Repeat the disproved drop.",
                            "steps": [],
                        }
                    },
                )
                controller._set_planner_feedback(
                    goal,
                    "The prior drop was refused; do not repeat it unchanged.",
                )

                controller.manage_goal(
                    {
                        "request_id": "resume-with-fresh-plan",
                        "goal_id": goal["id"],
                        "action": "resume",
                    }
                )

                self.assertNotIn(
                    goal["id"],
                    controller.storage.get_runtime("goal_execution_plans_v1", {}),
                )
                self.assertIsNone(controller._planner_feedback(goal))
            finally:
                controller.storage.close()

    def test_open_goal_repair_collapses_equivalent_paused_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                first = controller.storage.submit_goal(
                    goal_payload(request_id="duplicate-open-one")
                )["goal"]
                controller.storage.manage_goal(
                    {
                        "request_id": "pause-duplicate-one",
                        "goal_id": first["id"],
                        "action": "pause",
                    }
                )
                second = controller.storage.submit_goal(
                    goal_payload(
                        request_id="duplicate-open-two",
                        title="Same outcome, newer retry",
                    )
                )["goal"]

                repaired = controller._repair_open_goal_contracts()

                self.assertEqual([first["id"]], repaired["duplicates"])
                self.assertEqual("cancelled", controller.storage.goal(first["id"])["status"])
                self.assertEqual("active", controller.storage.goal(second["id"])["status"])
            finally:
                controller.storage.close()

    def test_purchase_mutation_requires_exact_fresh_quote_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="purchase-quote-binding",
                        objective="Buy leather armor.",
                        success_criteria=[
                            {"kind": "inventory_contains", "item": "leather armor"}
                        ],
                        constraints={
                            "purchase_plan": {
                                "item": "leather armor",
                                "merchant_class": "CorNothSergeant",
                                "room_id": 154,
                                "maximum_price": 300,
                            }
                        },
                    )
                )["goal"]
                controller.storage.set_runtime(
                    "purchase_preflights_v1",
                    {
                        goal["id"]: {
                            "goal_id": goal["id"],
                            "status": "live_verified",
                            "live_verified": True,
                            "checked_unix": time.time(),
                            "seller_id": 421,
                            "authorized_buy_ids": [425],
                            "minimum_price": 200,
                        }
                    },
                )
                observation = {
                    "look": {"room": {"num": 154, "name": "Cor Noth barracks"}},
                    "inventory": {"items": [{"name": "shillings", "amount": 250}]},
                }

                wrong_item = controller._purchase_action_blockers(
                    goal,
                    observation,
                    "shop",
                    {"seller": 421, "buy_ids": [999]},
                )
                self.assertEqual("purchase_item_mismatch", wrong_item[0]["kind"])
                allowed = controller._purchase_action_blockers(
                    goal,
                    observation,
                    "shop",
                    {"seller": 421, "buy_ids": [425]},
                )
                self.assertEqual([], allowed)
            finally:
                controller.storage.close()

    def test_replacement_purchase_requires_transaction_event_despite_carried_copy(self) -> None:
        goal = {
            "constraints": {
                "purchase_plan": {
                    "offering_kind": "item",
                    "item": "mace",
                    "merchant_class": "CorNothSergeant",
                    "room_id": 154,
                    "maximum_price": 200,
                }
            },
            "success_criteria": [
                {
                    "id": "mace-purchased",
                    "kind": "event_occurred",
                    "event_kind": "property.transaction",
                    "after_cursor": 100,
                },
                {
                    "id": "has-mace",
                    "kind": "inventory_contains",
                    "item": "mace",
                },
            ],
        }

        before_purchase = {
            "criteria": [
                {"id": "mace-purchased", "met": False},
                {"id": "has-mace", "met": True},
            ]
        }
        self.assertFalse(BotController._purchase_result_met(goal, before_purchase))

        after_purchase = {
            "criteria": [
                {"id": "mace-purchased", "met": True},
                {"id": "has-mace", "met": True},
            ]
        }
        self.assertTrue(BotController._purchase_result_met(goal, after_purchase))

    def test_successful_shop_emits_property_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = ShopBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="shop-property-transaction")
                )["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "shop",
                        "arguments": {"seller": 421, "buy_ids": [424]},
                        "rationale": "Buy the grounded replacement mace.",
                    },
                )

                self.assertEqual("shop", result["action"])
                events = controller.storage.events(
                    kinds=["property.transaction"], goal_id=goal["id"]
                )["events"]
                self.assertEqual(1, len(events))
                self.assertEqual("shop_buy", events[0]["data"]["transaction"])
                self.assertEqual(["mace (id 99) [get]"], events[0]["data"]["items_acquired"])
                self.assertFalse(events[0]["data"]["approval_required"])
            finally:
                controller.storage.close()

    def test_reconciles_successful_shop_after_missing_transaction_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="reconcile-shop-property-transaction",
                        success_criteria=[
                            {
                                "id": "purchase",
                                "kind": "event_occurred",
                                "event_kind": "property.transaction",
                                "after_cursor": 0,
                            },
                            {
                                "id": "mace",
                                "kind": "inventory_contains",
                                "item": "mace",
                                "count": 2,
                            },
                        ],
                        constraints={
                            "purchase_plan": {
                                "offering_kind": "item",
                                "item": "mace",
                                "merchant_class": "CorNothSergeant",
                                "room_id": 154,
                                "maximum_price": 200,
                            }
                        },
                    )
                )["goal"]
                source = controller.storage.emit_event(
                    "action.succeeded",
                    "Action succeeded: shop",
                    goal_id=goal["id"],
                    data={
                        "tool": "shop",
                        "result": {
                            "seller": 421,
                            "bought": [424],
                            "got": ["mace (id 17318) [get]"],
                        },
                    },
                )

                controller._reconcile_purchase_transaction(goal)
                controller._reconcile_purchase_transaction(goal)

                events = controller.storage.events(
                    kinds=["property.transaction"], goal_id=goal["id"]
                )["events"]
                self.assertEqual(1, len(events))
                self.assertEqual(source["id"], events[0]["data"]["recovered_from_event_id"])
            finally:
                controller.storage.close()

    def test_paid_training_prepares_funds_before_visiting_teacher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="fund-paid-training",
                        objective="Learn mace fighting from Rook.",
                        success_criteria=[
                            {
                                "kind": "numeric_threshold",
                                "metric": "ability.skill.mace fighting",
                                "operator": ">=",
                                "value": 1,
                            },
                            {
                                "kind": "location_reached",
                                "location": "Tos Inn",
                                "room_id": 52,
                            },
                            {
                                "kind": "state_equals",
                                "path": "status.position.col",
                                "value": 8,
                            },
                            {
                                "kind": "state_equals",
                                "path": "status.position.row",
                                "value": 8,
                            },
                        ],
                        constraints={
                            "purchase_plan": {
                                "offering_kind": "skill",
                                "item": "mace fighting",
                                "merchant_class": "CorNothSergeant",
                                "room_id": 154,
                                "maximum_price": 500,
                            }
                        },
                    )
                )["goal"]
                incomplete = {
                    "criteria": [
                        {"id": "criterion_1", "met": False},
                        {"id": "criterion_2", "met": False},
                        {"id": "criterion_3", "met": False},
                        {"id": "criterion_4", "met": False},
                    ],
                    "all_met": False,
                }

                needs_bank = controller._structured_purchase_preparation_action(
                    goal,
                    {"look": {"room": {"num": 52}}, "inventory": {"items": []}},
                    incomplete,
                    {"status": "travel_required"},
                )
                self.assertEqual("travel", needs_bank["tool"])
                self.assertEqual(54, needs_bank["arguments"]["to"])

                withdraw = controller._structured_purchase_preparation_action(
                    goal,
                    {
                        "look": {"room": {"num": 54}},
                        "inventory": {"items": [{"name": "shillings", "amount": 125}]},
                    },
                    incomplete,
                    {"status": "travel_required"},
                )
                self.assertEqual("bank", withdraw["tool"])
                self.assertEqual(
                    {"action": "withdraw", "amount": 375}, withdraw["arguments"]
                )

                visit_teacher = controller._structured_purchase_preparation_action(
                    goal,
                    {
                        "look": {"room": {"num": 54}},
                        "inventory": {"items": [{"name": "shillings", "amount": 500}]},
                    },
                    incomplete,
                    {"status": "travel_required"},
                )
                self.assertEqual("travel", visit_teacher["tool"])
                self.assertEqual(154, visit_teacher["arguments"]["to"])

                buy_training = controller._structured_purchase_preparation_action(
                    goal,
                    {
                        "look": {"room": {"num": 154}},
                        "inventory": {"items": [{"name": "shillings", "amount": 500}]},
                    },
                    incomplete,
                    {
                        "status": "live_verified",
                        "seller_id": 421,
                        "authorized_buy_ids": [3740],
                    },
                )
                self.assertEqual("shop", buy_training["tool"])
                self.assertEqual(
                    {"seller": 421, "buy_ids": [3740]}, buy_training["arguments"]
                )

                capacity_observation = {
                    "look": {"room": {"num": 154}},
                    "inventory": {
                        "items": [
                            {"name": "shillings", "amount": 500},
                            {"id": 80, "name": "bulky loot", "amount": 1},
                        ]
                    },
                }
                controller._record_blocked_action(
                    goal,
                    capacity_observation,
                    "shop",
                    {"agent": "primary", "seller": 421, "buy_ids": [3740]},
                    'Rook says, "I cannot give you that. Perhaps you carry too much?"',
                )
                capacity_blocked = controller._structured_purchase_preparation_action(
                    goal,
                    capacity_observation,
                    incomplete,
                    {
                        "status": "live_verified",
                        "seller_id": 421,
                        "authorized_buy_ids": [3740],
                    },
                )
                self.assertIsNone(capacity_blocked)

                complete = {
                    "criteria": [
                        {"id": "criterion_1", "met": True},
                        {"id": "criterion_2", "met": False},
                        {"id": "criterion_3", "met": False},
                        {"id": "criterion_4", "met": False},
                    ],
                    "all_met": False,
                }
                return_home = controller._structured_purchase_preparation_action(
                    goal,
                    {"look": {"room": {"num": 154}}},
                    complete,
                    {"status": "live_verified"},
                )
                self.assertEqual("travel", return_home["tool"])
                self.assertEqual(52, return_home["arguments"]["to"])

                controller.storage.set_runtime(
                    "blocked_actions",
                    [
                        {
                            "goal_id": goal["id"],
                            "tool": "travel",
                            "arguments": {"agent": "primary", "to": 52},
                            "room": 154,
                            "reason": "stood on the exit square and nothing happened",
                        }
                    ],
                )
                recover_exit = controller._structured_purchase_preparation_action(
                    goal,
                    {
                        "look": {
                            "room": {"num": 154},
                            "exits": [
                                {
                                    "kind": "go",
                                    "to": 150,
                                    "reachable": True,
                                    "steps_away": 0,
                                }
                            ],
                        }
                    },
                    complete,
                    None,
                )
                self.assertEqual("act", recover_exit["tool"])
                self.assertEqual({"verb": "go"}, recover_exit["arguments"])

                controller.storage.set_runtime(
                    "blocked_actions",
                    [
                        {
                            "goal_id": goal["id"],
                            "tool": "travel",
                            "arguments": {"agent": "primary", "to": 52},
                            "room": 150,
                            "reason": "no floor anywhere on the west boundary",
                        }
                    ],
                )
                controller.storage.emit_event(
                    "action.no_progress",
                    "Action made no progress: travel",
                    goal_id=goal["id"],
                    data={
                        "tool": "travel",
                        "arguments": {"agent": "primary", "to": 52},
                        "room": {"num": 150, "name": "Cor Noth"},
                        "result": {
                            "log": [
                                {
                                    "from": "Cor Noth",
                                    "to": "Main gate to Cor Noth",
                                    "via": "edge",
                                    "ok": False,
                                }
                            ]
                        },
                    },
                )
                recover_route_hop = controller._structured_purchase_preparation_action(
                    goal,
                    {
                        "look": {
                            "room": {"num": 150},
                            "exits": [
                                {
                                    "kind": "go",
                                    "to": 574,
                                    "to_name": "Main gate to Cor Noth",
                                    "stand_on": {"col": 39, "row": 1},
                                    "reachable": True,
                                    "steps_away": 31,
                                }
                            ],
                        }
                    },
                    complete,
                    None,
                )
                self.assertEqual("go_through", recover_route_hop["tool"])
                self.assertEqual(
                    {"to": 574, "col": 39, "row": 1},
                    recover_route_hop["arguments"],
                )

                controller.storage.set_runtime(
                    "blocked_actions",
                    [
                        {
                            "goal_id": goal["id"],
                            "tool": "go_through",
                            "arguments": {
                                "agent": "primary",
                                "to": 574,
                                "col": 39,
                                "row": 1,
                            },
                            "room": 150,
                            "reason": "stood on the exit square and nothing happened",
                        }
                    ],
                )
                activate_alternate_exit = (
                    controller._structured_purchase_preparation_action(
                        goal,
                        {
                            "look": {
                                "room": {"num": 150},
                                "exits": [
                                    {
                                        "kind": "go",
                                        "to": 574,
                                        "to_name": "Main gate to Cor Noth",
                                        "stand_on": {"col": 39, "row": 1},
                                        "reachable": True,
                                        "steps_away": 0,
                                    }
                                ],
                            }
                        },
                        complete,
                        None,
                    )
                )
                self.assertEqual("act", activate_alternate_exit["tool"])
                self.assertEqual(
                    {"verb": "go"}, activate_alternate_exit["arguments"]
                )

                finish_at_bar = controller._structured_purchase_preparation_action(
                    goal,
                    {
                        "look": {"room": {"num": 52}},
                        "status": {"position": {"col": 1, "row": 3}},
                    },
                    complete,
                    None,
                )
                self.assertEqual("walk_to", finish_at_bar["tool"])
                self.assertEqual({"col": 8, "row": 8}, finish_at_bar["arguments"])

                self.assertIsNone(
                    controller._structured_purchase_preparation_action(
                        goal,
                        {
                            "look": {"room": {"num": 52}},
                            "status": {"position": {"col": 8, "row": 8}},
                        },
                        complete,
                        None,
                    )
                )
            finally:
                controller.storage.close()

    def test_combined_purchase_and_farm_plan_includes_both_phases_within_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 54)
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="combined-purchase-farm-plan",
                        objective="Buy a mace and reach 31 max HP.",
                        success_criteria=[
                            {"kind": "inventory_contains", "item": "mace"},
                            {
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 31,
                            },
                        ],
                        constraints={
                            "purchase_plan": {
                                "item": "mace",
                                "merchant_class": "CorNothSergeant",
                                "room_id": 154,
                                "maximum_price": 100,
                            },
                            "operator_notes": (
                                "hunt=groundworm larva; assigned_room=557; "
                                "use_safe_spots=true; fight_above_vigor=100"
                            ),
                        },
                    )
                )["goal"]
                controller.broker = BackgroundFarmBroker()
                for tool_name in (
                    "travel",
                    "bank",
                    "shop",
                    "act",
                    "go_through",
                    "walk_to",
                    "autopilot",
                ):
                    controller.broker.tools.setdefault(
                        tool_name,
                        Tool(
                            tool_name,
                            f"Test {tool_name} tool.",
                            {"type": "object", "properties": {}},
                        ),
                    )
                controller.last_observation = controller.broker.observe()
                controller.last_observation["look"]["room"] = {
                    "num": 54,
                    "name": "First Royal Bank of Tos",
                }
                controller.last_observation["look"]["vitals"]["health"] = {
                    "current": 30,
                    "max": 30,
                }
                controller.last_observation["status"]["vitals"]["health"] = {
                    "current": 30,
                    "max": 30,
                }

                selected_plan = with_safe_ending(
                    {
                        "summary": "Choose a safe ending after the work.",
                        "steps": [],
                        "assumptions": [],
                    },
                    54,
                )
                plan = controller._structured_purchase_controller_plan(
                    goal, selected_plan=selected_plan
                )

                self.assertLessEqual(len(plan["steps"]), 8)
                self.assertIn(
                    "launch-goal-keeper",
                    {step["id"] for step in plan["steps"]},
                )
                stored = controller._store_execution_plan(
                    goal,
                    plan,
                    grounding={
                        "valid": True,
                        "corpus": {"corpus_version": "test"},
                        "purchase_verification": {"static_verified": True},
                    },
                    revision=False,
                )
                self.assertEqual("verified", stored["verification"]["status"])
                self.assertEqual(
                    5,
                    len(
                        controller._structured_farm_controller_plan(
                            goal, selected_plan=stored
                        )["steps"]
                    ),
                )

                controller.last_observation["look"]["vitals"]["health"] = {
                    "current": 31,
                    "max": 31,
                }
                controller.last_observation["status"]["vitals"]["health"] = {
                    "current": 31,
                    "max": 31,
                }
                completion = controller.criteria.evaluate(
                    goal, controller.last_observation
                )
                return_plan = controller._structured_purchase_controller_plan(
                    goal, completion, selected_plan=stored
                )
                return_step_ids = {step["id"] for step in return_plan["steps"]}
                self.assertNotIn("launch-goal-keeper", return_step_ids)
                self.assertNotIn("finish-purchase-at-goal-location", return_step_ids)
                self.assertNotIn("finish-purchase-at-goal-position", return_step_ids)
                revised = controller._store_execution_plan(
                    goal,
                    return_plan,
                    grounding={
                        "valid": True,
                        "corpus": {"corpus_version": "test"},
                        "purchase_verification": {"static_verified": True},
                    },
                    revision=True,
                )
                self.assertEqual("verified", revised["verification"]["status"])
            finally:
                controller.storage.close()

    def test_completed_purchase_has_no_implicit_home_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="purchase-without-finish",
                        objective="Learn mace fighting from Rook.",
                        success_criteria=[
                            {
                                "kind": "numeric_threshold",
                                "metric": "ability.skill.mace fighting",
                                "operator": ">=",
                                "value": 1,
                            }
                        ],
                        constraints={
                            "purchase_plan": {
                                "offering_kind": "skill",
                                "item": "mace fighting",
                                "merchant_class": "CorNothSergeant",
                                "room_id": 154,
                                "maximum_price": 500,
                            }
                        },
                    )
                )["goal"]
                completion = {
                    "criteria": [{"id": "criterion_1", "met": True}],
                    "all_met": True,
                }

                self.assertIsNone(
                    controller._structured_purchase_preparation_action(
                        goal,
                        {"look": {"room": {"num": 154}}},
                        completion,
                        None,
                    )
                )
                step_ids = {
                    step["id"]
                    for step in controller._structured_purchase_controller_plan(goal)[
                        "steps"
                    ]
                }
                self.assertNotIn("finish-purchase-at-goal-location", step_ids)
                self.assertNotIn("finish-purchase-at-goal-position", step_ids)
            finally:
                controller.storage.close()

    def test_completed_purchase_uses_non_tos_goal_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="purchase-with-explicit-finish",
                        success_criteria=[
                            {"kind": "inventory_contains", "item": "mace"},
                            {
                                "kind": "location_reached",
                                "location": "Jasper Tavern",
                                "room_id": 371,
                            },
                            {
                                "kind": "state_equals",
                                "path": "status.position.col",
                                "value": 4,
                            },
                            {
                                "kind": "state_equals",
                                "path": "status.position.row",
                                "value": 6,
                            },
                        ],
                        constraints={
                            "purchase_plan": {
                                "item": "mace",
                                "merchant_class": "CorNothSergeant",
                                "room_id": 154,
                                "maximum_price": 100,
                            }
                        },
                    )
                )["goal"]
                completion = {
                    "criteria": [
                        {"id": "criterion_1", "met": True},
                        {"id": "criterion_2", "met": False},
                        {"id": "criterion_3", "met": False},
                        {"id": "criterion_4", "met": False},
                    ],
                    "all_met": False,
                }

                travel = controller._structured_purchase_preparation_action(
                    goal,
                    {"look": {"room": {"num": 154}}},
                    completion,
                    None,
                )
                self.assertEqual({"to": 371}, travel["arguments"])
                walk = controller._structured_purchase_preparation_action(
                    goal,
                    {
                        "look": {"room": {"num": 371}},
                        "status": {"position": {"col": 1, "row": 1}},
                    },
                    completion,
                    None,
                )
                self.assertEqual({"col": 4, "row": 6}, walk["arguments"])
            finally:
                controller.storage.close()

    def test_character_tool_ignores_planner_agent_and_binds_configured_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                simulator = SimulatedBroker()
                controller.broker = simulator
                goal = controller.storage.submit_goal(goal_payload())["goal"]

                controller._execute(
                    goal,
                    simulator.observe(),
                    {
                        "tool": "act",
                        "arguments": {"agent": "TestHero", "verb": "drop", "target": 1},
                        "rationale": "Drop it.",
                    },
                )

                act_call = next(arguments for name, arguments in simulator.calls if name == "act")
                self.assertEqual("primary", act_call["agent"])
            finally:
                controller.storage.close()

    def test_global_tool_discards_planner_agent_without_reinjecting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                simulator = SimulatedBroker()
                controller.broker = simulator
                goal = controller.storage.submit_goal(goal_payload())["goal"]

                result = controller._execute(
                    goal,
                    simulator.observe(),
                    {
                        "tool": "hunting_grounds",
                        "arguments": {"agent": "TestHero", "near": "Raza"},
                        "rationale": "Find an appropriate area.",
                    },
                )

                self.assertEqual("hunting_grounds", result["action"])
                call = next(arguments for name, arguments in simulator.calls if name == "hunting_grounds")
                self.assertEqual({"near": "Raza"}, call)
            finally:
                controller.storage.close()

    def test_invalid_proposal_is_classified_as_model_output_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                controller.broker = SimulatedBroker()
                controller.model = InvalidProposalModel()  # type: ignore[assignment]
                controller.storage.submit_goal(goal_payload())

                with self.assertRaisesRegex(ModelError, "planner proposed an invalid goal"):
                    controller.turn()

                self.assertEqual([], controller.storage.proposals())
            finally:
                controller.storage.close()

    def test_missing_proposal_object_is_safe_wait_when_a_proposal_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                controller.broker = SimulatedBroker()
                controller.model = MissingProposalModel()  # type: ignore[assignment]
                controller.storage.submit_goal(goal_payload())
                controller.storage.create_proposal(
                    {
                        "title": "Existing follow-up",
                        "objective": "Do something after the active goal.",
                        "success_criteria": [{"kind": "event_occurred", "event_kind": "conversation.responded"}],
                    },
                    "Already pending.",
                )

                result = controller.turn()

                self.assertTrue(result["wait"])
                self.assertEqual("proposal_already_pending", result["suppressed"])
                feedback = controller.storage.get_runtime("planner_feedback")
                self.assertIn("changed nothing", feedback["message"])
            finally:
                controller.storage.close()

    def test_repeated_wait_receives_explicit_liveness_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                controller.broker = SimulatedBroker()
                model = WaitingModel()
                controller.model = model  # type: ignore[assignment]
                controller.storage.submit_goal(goal_payload())

                first = controller.turn()
                second = controller.turn()

                self.assertIsNone(model.feedback[0])
                self.assertIn("waited without advancing", model.feedback[1]["message"])
                self.assertEqual(1, first["consecutive_waits"])
                self.assertEqual(2, second["consecutive_waits"])
                feedback = controller.status()["attention"]["planner_feedback"]
                self.assertEqual(2, feedback["consecutive_waits"])
            finally:
                controller.storage.close()

    def test_semantic_no_progress_result_is_not_recorded_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = NoProgressBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(goal_payload())["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "travel",
                        "arguments": {"destination": "Mausoleum"},
                        "rationale": "Try the known hunting ground.",
                    },
                )

                self.assertTrue(result["no_progress"])
                self.assertEqual(
                    ("rest", {"agent": "primary", "stand": True}),
                    broker.calls[0],
                )
                events = controller.storage.events(kinds=["action.no_progress"])["events"]
                self.assertEqual(1, len(events))
                self.assertIn("no route", events[0]["data"]["reason"])
                feedback = controller.storage.get_runtime("planner_feedback")
                self.assertIn("Do not repeat the same call unchanged", feedback["message"])
                self.assertEqual([], controller.storage.events(kinds=["action.succeeded"])["events"])

                repeated = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "travel",
                        "arguments": {"destination": "Mausoleum"},
                        "rationale": "Retry the same route.",
                    },
                )
                self.assertTrue(repeated["retry_suppressed"])
                self.assertEqual(1, len([name for name, _ in broker.calls if name == "travel"]))
                suppression = controller.storage.events(kinds=["action.retry_suppressed"])["events"]
                self.assertEqual(1, len(suppression))
                feedback = controller.storage.get_runtime("planner_feedback")
                self.assertEqual("travel", feedback["blocked_action"]["tool"])
            finally:
                controller.storage.close()

    def test_durable_lesson_immediately_ends_only_the_reselected_campaign_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="durable-lesson-campaign-breaker")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                phase = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "general",
                        "objective": "Retry an already disproved observation tactic.",
                        "success_criteria": [
                            {
                                "kind": "state_equals",
                                "path": "inventory.items",
                                "value": [],
                            }
                        ],
                    },
                    mode="start",
                )
                controller.learning = SimpleNamespace(
                    check_action=lambda *_: {
                        "lesson": {
                            "id": "durable-tactic-lesson",
                            "summary": "the same inventory tactic already failed twice",
                        }
                    }
                )  # type: ignore[assignment]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "inventory",
                        "arguments": {},
                        "rationale": "Retry the same disproved tactic.",
                        "expected_observation": {"inventory": "changed"},
                    },
                )

                self.assertTrue(result["campaign_breaker"]["breaker_tripped"])
                self.assertTrue(result["strategic_goal_preserved"])
                self.assertEqual("active", controller.storage.goal(goal["id"])["status"])
                self.assertIsNone(controller.storage.active_campaign_phase(run["id"]))
                self.assertEqual(
                    "failed", controller.storage.campaign_phases(run["id"])[0]["status"]
                )
                self.assertEqual(phase["id"], result["campaign_breaker"]["phase_id"])
            finally:
                controller.storage.close()

    def test_quarantined_farm_phase_is_retired_and_cannot_be_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="quarantined-campaign-phase",
                        title="Reach 101 max HP",
                        objective="Raise maximum HP to at least 101.",
                        success_criteria=[
                            {
                                "id": "max-hp-101",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 101,
                            }
                        ],
                    )
                )["goal"]
                controller.storage.set_runtime(
                    "farm_tactic_quarantine_v1",
                    {
                        "563": {
                            "room": 563,
                            "target": "ant",
                            "use_safe_spots": True,
                            "quarantine_scope": "safe_spots",
                            "guidance": "Choose a different grounded room.",
                            "reasons": ["live overlevel hostile"],
                        }
                    },
                )
                unsafe_phase = {
                    "kind": "farm",
                    "objective": "Farm ants in room 563.",
                    "success_criteria": [
                        {
                            "id": "max-hp-101",
                            "kind": "numeric_threshold",
                            "metric": "status.vitals.health.max",
                            "operator": ">=",
                            "value": 101,
                        }
                    ],
                    "abandon_predicates": [],
                    "budget": {"max_actions": 120, "max_minutes": 60},
                    "context": {
                        "room": 563,
                        "target": "ant",
                        "strategy": "Use safe spots while farming.",
                    },
                    "rationale": "Try the ant room.",
                }
                run = controller.storage.ensure_campaign_run(goal)
                old_phase = controller.storage.create_campaign_phase(
                    run,
                    unsafe_phase,
                    mode="start",
                )
                controller.model = SimpleNamespace(
                    manage_campaign=lambda **_: {
                        "decision": "start_phase",
                        "phase": unsafe_phase,
                        "rationale": "Retry the same quarantined room.",
                        "evidence": [],
                    }
                )  # type: ignore[assignment]

                _, replacement, _ = controller._campaign_turn_state(
                    goal,
                    broker.observe(),
                    {},
                )

                phases = controller.storage.campaign_phases(run["id"])
                retired = next(item for item in phases if item["id"] == old_phase["id"])
                self.assertEqual("failed", retired["status"])
                self.assertIsNotNone(replacement)
                self.assertNotEqual(old_phase["id"], replacement["id"])
                self.assertTrue(replacement["context"]["deterministic_fallback"])
                self.assertIsNone(
                    controller._campaign_phase_grounding_blocker(replacement)
                )
                self.assertEqual(
                    1,
                    len(
                        controller.storage.events(
                            kinds=["campaign.phase.grounding_rejected"]
                        )["events"]
                    ),
                )
                self.assertEqual(
                    1,
                    len(
                        controller.storage.events(kinds=["campaign.manager.rejected"])[
                            "events"
                        ]
                    ),
                )
            finally:
                controller.storage.close()

    def test_farm_phase_rejects_retained_route_failure_from_current_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                broker.room = {"num": 52, "name": "Familiars"}
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="campaign-route-failure")
                )["goal"]
                observation = broker.observe()
                lesson = controller.learning.defer_goal(
                    goal,
                    observation,
                    tool="travel",
                    arguments={"agent": "primary", "to": 6},
                    reason="no route from 52 to 6 in the graph",
                    classification="route_unavailable",
                    scope="tactic",
                    block=False,
                )["lesson"]
                phase = {
                    "kind": "farm",
                    "context": {
                        "room": 6,
                        "target": "ant",
                        "use_safe_spots": True,
                    },
                }

                blocker = controller._campaign_phase_grounding_blocker(
                    phase,
                    observation,
                )

                self.assertEqual("retained_route_failure", blocker["kind"])
                self.assertEqual(lesson["id"], blocker["lesson_id"])
                changed_origin = copy.deepcopy(observation)
                changed_origin["look"]["room"] = {
                    "num": 54,
                    "name": "First Royal Bank of Tos",
                }
                self.assertIsNone(
                    controller._campaign_phase_grounding_blocker(
                        phase,
                        changed_origin,
                    )
                )
            finally:
                controller.storage.close()

    def test_unwieldable_equip_best_is_no_progress_not_liveness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = UnwieldableWeaponBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(goal_payload())['goal']

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "equip_best",
                        "arguments": {},
                        "rationale": "Arm TestHero before hazardous work.",
                    },
                )

                self.assertTrue(result["no_progress"])
                self.assertIn("nothing wieldable", result["reason"])
                self.assertEqual(
                    1,
                    len(controller.storage.events(kinds=["action.no_progress"])["events"]),
                )
                self.assertEqual(
                    [], controller.storage.events(kinds=["action.succeeded"])["events"]
                )
                self.assertIsNone(
                    controller._no_progress_reason(
                        {"wielding": "mace", "verified": True},
                        broker.observe(),
                        tool="equip_best",
                    )
                )
            finally:
                controller.storage.close()

    def test_planner_invented_map_alias_defers_only_that_tactic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = EmptyMapBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(goal_payload())['goal']
                decision = {
                    "tool": "map",
                    "arguments": {"search": "Tos Blacksmith"},
                    "rationale": "Find a shop alias.",
                }

                first = controller._execute(goal, broker.observe(), decision)
                self.assertTrue(first["no_progress"])
                for _ in range(controller.config.learning.repeated_tactic_budget):
                    last = controller._execute(goal, broker.observe(), decision)

                self.assertTrue(last["tactic_deferred"])
                self.assertFalse(last["goal_blocked"])
                self.assertEqual("active", controller.storage.goal(goal["id"])["status"])
                lesson = controller.storage.goal_lessons(statuses=["deferred"], limit=10)[0]
                self.assertEqual("invalid_reference", lesson["classification"])
                self.assertEqual("tactic", lesson["scope"])
            finally:
                controller.storage.close()

    def test_identical_merchant_catalog_replay_is_no_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CatalogBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(goal_payload())['goal']
                decision = {
                    "tool": "merchants",
                    "arguments": {"search": "armor"},
                    "rationale": "Find an armor seller.",
                }

                first = controller._execute(goal, broker.observe(), decision)
                second = controller._execute(goal, broker.observe(), decision)

                self.assertIn("completion", first)
                self.assertTrue(second["no_progress"])
                self.assertIn("identical evidence lookup", second["reason"])
                self.assertEqual(2, len([name for name, _ in broker.calls if name == "merchants"]))
            finally:
                controller.storage.close()

    def test_identical_progression_lookup_replay_is_no_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                observation = {"look": {"room": {"num": 52, "name": "Familiars"}}}
                arguments = {"agent": "primary", "purpose": "advance", "goals": [{"kind": "hp"}]}
                result = {"candidates": [{"creature": "giant rat", "level": 30}]}

                first = controller._repeated_evidence_reason("prey", arguments, result, observation)
                second = controller._repeated_evidence_reason("prey", arguments, result, observation)

                self.assertIsNone(first)
                self.assertIn("identical evidence lookup", second or "")
                guidance = controller._no_progress_guidance("prey", second or "")
                self.assertIn("call hunting_grounds", guidance)
            finally:
                controller.storage.close()

    def test_identical_bank_balance_replay_is_no_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                observation = {
                    "look": {"room": {"num": 54, "name": "First Royal Bank of Tos"}}
                }
                arguments = {"agent": "primary", "action": "balance"}
                result = {"action": "balance", "balance": 1123}

                first = controller._repeated_evidence_reason(
                    "bank", arguments, result, observation
                )
                second = controller._repeated_evidence_reason(
                    "bank", arguments, result, observation
                )

                self.assertIsNone(first)
                self.assertIn("identical evidence lookup", second or "")
                guidance = controller._no_progress_guidance("bank", second or "")
                self.assertIn("launch the goal-owned bounded keeper", guidance)
            finally:
                controller.storage.close()

    def test_server_refusal_message_is_no_progress(self) -> None:
        reason = BotController._no_progress_reason(
            {"verb": "get", "messages": ["You're unable to pick up Paddock."]},
            {},
            tool="act",
        )
        self.assertEqual("You're unable to pick up Paddock.", reason)

    def test_drop_quantity_refusal_is_semantic_failure(self) -> None:
        reason = BotController._no_progress_reason(
            {
                "verb": "drop",
                "messages": ["You don't have that amount of mushrooms to drop."],
            },
            {"inventory": {"items": [{"id": 7, "name": "mushrooms", "amount": 2}]}},
            tool="act",
            arguments={"verb": "drop", "target": 7, "amount": 20},
        )
        self.assertEqual("You don't have that amount of mushrooms to drop.", reason)

    def test_shop_claim_without_received_items_is_no_progress(self) -> None:
        reason = BotController._no_progress_reason(
            {
                "bought": [177, 177, 177, 177],
                "got": [],
                "messages": ['Frisconar says, "Come back when you have enough money for the flask."'],
            },
            {},
            tool="shop",
        )
        self.assertIn("enough money", reason or "")

    def test_insufficient_funds_feedback_blocks_quantity_cycling(self) -> None:
        guidance = BotController._no_progress_guidance(
            "shop",
            'Frisconar says, "Come back when you have enough money for the flask."',
            repeated=True,
        )
        self.assertIn("Do not retry this purchase at any quantity", guidance)
        self.assertIn("sell confirm:false", guidance)
        self.assertIn("verified bank", guidance)

    def test_inventory_capacity_feedback_reports_cause_without_prescribing_solution(self) -> None:
        observation = {
            "inventory": {
                "carry": {
                    "known": True,
                    "items": 19,
                    "weight_max": 2300,
                    "bulk_max": 2300,
                    "load": {
                        "weight": 1810,
                        "bulk": 2260,
                        "exact": True,
                    },
                    "room_for": {"weight": 490, "bulk": 40},
                }
            }
        }
        guidance = BotController._no_progress_guidance(
            "shop",
            'Colhorr says, "I\'m unable to give you the mace. Perhaps you carry too much?"',
            repeated=True,
            observation=observation,
        )

        self.assertIn("reason the requested item was not transferred", guidance)
        self.assertIn("weight=1810/2300", guidance)
        self.assertIn("bulk=2260/2300", guidance)
        self.assertIn("expected item-transfer observation was disproved", guidance)
        for prescription in ("drop ", "sell ", "bank ", "free room", "choose a different tool"):
            self.assertNotIn(prescription, guidance.casefold())

    def test_capacity_refusal_upgrades_feedback_and_failed_retry_invalidates_purchase_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                broker.tools["shop"] = Tool(
                    "shop",
                    "Buy from a merchant.",
                    {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            "seller": {"type": "integer"},
                            "buy_ids": {"type": "array", "items": {"type": "integer"}},
                        },
                        "required": ["agent", "seller", "buy_ids"],
                    },
                )
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="capacity-plan-invalidation")
                )["goal"]
                observation = {
                    "look": {"room": {"num": 201, "name": "Ye Olde Slasher Salesman"}},
                    "inventory": {
                        "items": [{"id": 348, "name": "mace", "amount": 3}],
                        "carry": {
                            "known": True,
                            "items": 1,
                            "weight_max": 2300,
                            "bulk_max": 2300,
                            "load": {"weight": 2100, "bulk": 2290, "exact": True},
                            "room_for": {"weight": 200, "bulk": 10},
                        },
                    },
                }
                arguments = {"agent": "primary", "seller": 347, "buy_ids": [348]}
                reason = 'Colhorr says, "I\'m unable to give you the mace. Perhaps you carry too much?"'
                controller.last_observation = observation
                controller._record_blocked_action(goal, observation, "shop", arguments, reason)
                controller._set_planner_feedback(
                    goal,
                    "Choose different arguments.",
                    blocked_action={"tool": "shop", "arguments": arguments, "room": 201},
                )
                controller.storage.set_runtime(
                    "goal_execution_plans_v1",
                    {goal["id"]: {"summary": "Buy the fourth mace."}},
                )

                feedback = controller._planner_feedback(goal)

                self.assertEqual(
                    "inventory_capacity_refused",
                    feedback["failure_context"]["kind"],
                )
                self.assertFalse(feedback["failure_context"]["item_transfer_verified"])
                self.assertIn(
                    goal["id"],
                    controller.storage.get_runtime("goal_execution_plans_v1", {}),
                )

                result = controller._execute(
                    goal,
                    observation,
                    {
                        "tool": "shop",
                        "arguments": {"seller": 347, "buy_ids": [348]},
                        "rationale": "Retry the unchanged purchase.",
                        "plan_step_id": "buy-mace",
                    },
                )

                self.assertTrue(result["retry_suppressed"])
                self.assertNotIn(
                    goal["id"],
                    controller.storage.get_runtime("goal_execution_plans_v1", {}),
                )
            finally:
                controller.storage.close()

    def test_capacity_refusal_fingerprint_expires_when_inventory_load_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="capacity-state-change")
                )["goal"]
                arguments = {"seller": 347, "buy_ids": [348]}
                before = {
                    "look": {"room": {"num": 201, "name": "Ye Olde Slasher Salesman"}},
                    "inventory": {"items": [{"id": 348, "name": "mace", "amount": 3}]},
                }
                after = {
                    "look": before["look"],
                    "inventory": {"items": [{"id": 348, "name": "mace", "amount": 2}]},
                }
                controller._record_blocked_action(
                    goal,
                    before,
                    "shop",
                    arguments,
                    'Colhorr says, "Perhaps you carry too much?"',
                )

                self.assertIsNotNone(controller._blocked_action(goal, before, "shop", arguments))
                self.assertIsNone(controller._blocked_action(goal, after, "shop", arguments))
            finally:
                controller.storage.close()

    def test_equipment_refusal_tracks_candidates_not_unrelated_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="equipment-state-change")
                )["goal"]
                arguments = {"agent": "primary"}
                before = {
                    "look": {"room": {"num": 52, "name": "Familiars"}},
                    "inventory": {
                        "items": [
                            {"id": 101, "name": "mace", "can": ["use", "drop"]},
                            {"id": 1, "name": "shillings", "amount": 677},
                        ]
                    },
                    "equipment": {"known": True, "equipped": [], "wielding": None},
                }
                unrelated_inventory_change = copy.deepcopy(before)
                unrelated_inventory_change["inventory"]["items"][1]["amount"] = 1960
                unrelated_inventory_change["inventory"]["items"].append(
                    {"id": 202, "name": "herb", "amount": 4}
                )
                replacement_weapon = copy.deepcopy(unrelated_inventory_change)
                replacement_weapon["inventory"]["items"][0] = {
                    "id": 303,
                    "name": "mace",
                    "can": ["use", "drop"],
                }
                controller._record_blocked_action(
                    goal,
                    before,
                    "equip_best",
                    arguments,
                    "Every candidate was refused or broken.",
                )

                self.assertIsNotNone(
                    controller._blocked_action(goal, before, "equip_best", arguments)
                )
                self.assertIsNotNone(
                    controller._blocked_action(
                        goal,
                        unrelated_inventory_change,
                        "equip_best",
                        arguments,
                    )
                )
                self.assertIsNone(
                    controller._blocked_action(
                        goal,
                        replacement_weapon,
                        "equip_best",
                        arguments,
                    )
                )
                self.assertEqual([], controller.storage.get_runtime("blocked_actions"))
            finally:
                controller.storage.close()

    def test_legacy_equipment_refusal_expires_after_inventory_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="legacy-equipment-state-change")
                )["goal"]
                arguments = {"agent": "primary"}
                before = {
                    "look": {"room": {"num": 52, "name": "Familiars"}},
                    "inventory": {
                        "items": [{"id": 101, "name": "mace", "can": ["use", "drop"]}]
                    },
                    "equipment": {"known": True, "equipped": [], "wielding": None},
                }
                after = copy.deepcopy(before)
                after["inventory"]["items"].append(
                    {"id": 1, "name": "shillings", "amount": 1960}
                )
                controller._record_blocked_action(
                    goal,
                    before,
                    "equip_best",
                    arguments,
                    "Every candidate was refused or broken.",
                )
                legacy_entries = controller.storage.get_runtime("blocked_actions")
                legacy_entries[0].pop("equipment_attempt_hash")
                controller.storage.set_runtime("blocked_actions", legacy_entries)

                self.assertIsNotNone(
                    controller._blocked_action(goal, before, "equip_best", arguments)
                )
                self.assertIsNone(
                    controller._blocked_action(goal, after, "equip_best", arguments)
                )
                self.assertEqual([], controller.storage.get_runtime("blocked_actions"))
            finally:
                controller.storage.close()

    def test_persisted_generic_shop_feedback_is_upgraded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(goal_payload(request_id="feedback-upgrade"))["goal"]
                observation = {
                    "look": {"room": {"num": 53, "name": "Frisconar's Mysticals"}},
                    "inventory": {"items": []},
                }
                arguments = {"agent": "primary", "buy_ids": [177, 177, 177]}
                reason = 'Frisconar says, "Come back when you have enough money for the flask."'
                controller.last_observation = observation
                controller._record_blocked_action(goal, observation, "shop", arguments, reason)
                controller._set_planner_feedback(
                    goal,
                    "Choose different arguments.",
                    blocked_action={"tool": "shop", "arguments": arguments, "room": 53},
                )

                feedback = controller._planner_feedback(goal)

                self.assertIn("Do not retry this purchase at any quantity", feedback["message"])
            finally:
                controller.storage.close()

    def test_insufficient_funds_fingerprint_expires_when_carried_cash_increases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(goal_payload(request_id="cash-state-change"))["goal"]
                arguments = {"agent": "primary", "buy_ids": [177, 177]}
                before = {
                    "look": {"room": {"num": 53, "name": "Frisconar's Mysticals"}},
                    "inventory": {"items": [{"id": 1, "name": "shilling", "amount": 24}]},
                }
                after = {
                    "look": before["look"],
                    "inventory": {"items": [{"id": 1, "name": "shilling", "amount": 664}]},
                }
                controller._record_blocked_action(
                    goal,
                    before,
                    "shop",
                    arguments,
                    'Frisconar says, "Come back when you have enough money for the flask."',
                )

                self.assertIsNotNone(controller._blocked_action(goal, before, "shop", arguments))
                self.assertIsNone(controller._blocked_action(goal, after, "shop", arguments))
            finally:
                controller.storage.close()

    def test_rejected_sale_is_no_progress_but_counteroffer_is_progress(self) -> None:
        rejected = BotController._no_progress_reason(
            {
                "sold": False,
                "offered_price": None,
                "messages": ['Frisconar tells you, "I\'m not interested."'],
            },
            {},
            tool="sell",
        )
        quoted = BotController._no_progress_reason(
            {"sold": False, "offered_price": 12, "messages": []},
            {},
            tool="sell",
        )
        self.assertIn("not interested", rejected or "")
        self.assertIsNone(quoted)

    def test_sell_all_with_only_refusals_is_factual_no_progress(self) -> None:
        reason = BotController._no_progress_reason(
            {
                "sold": [],
                "refused": [
                    {"name": "herb", "why": "no counteroffer came back"},
                    {"name": "sapphire", "why": "no counteroffer came back"},
                ],
                "total_received": 0,
            },
            {},
            tool="sell_all",
        )
        guidance = BotController._no_progress_guidance("sell_all", reason or "")

        self.assertIn("merchant bought zero of 2", reason or "")
        self.assertIn("No property transferred", guidance)
        self.assertIn("did not reduce the carried inventory load", guidance)
        for prescription in ("different buyer", "different item", "drop ", "bank "):
            self.assertNotIn(prescription, guidance.casefold())
        self.assertTrue(BotController._failure_invalidates_plan("sell_all", reason))

    def test_empty_bank_note_is_no_progress(self) -> None:
        reason = BotController._no_progress_reason(
            {
                "action": "withdraw",
                "amount": 200,
                "balance": None,
                "banker_said": [],
                "note": "the banker said nothing, which almost always means there is no banker in this room",
            },
            {},
            tool="bank",
        )
        self.assertIn("banker said nothing", reason or "")

    def test_empty_bank_note_is_reconciled_when_inventory_actually_changed(self) -> None:
        before = {"inventory": {"items": []}}
        after = {"inventory": {"items": [{"id": 9, "name": "shilling", "amount": 205}]}}
        reason = BotController._no_progress_reason(
            {
                "action": "withdraw",
                "amount": 205,
                "balance": None,
                "banker_said": [],
                "note": "the banker said nothing, which almost always means there is no banker in this room",
            },
            before,
            tool="bank",
            after_observation=after,
        )
        self.assertIsNone(reason)

    def test_empty_bank_note_is_no_progress_inside_verified_bank_when_unchanged(self) -> None:
        reason = BotController._no_progress_reason(
            {
                "action": "withdraw",
                "amount": 205,
                "balance": None,
                "banker_said": [],
                "note": "the banker said nothing, which almost always means there is no banker in this room",
            },
            {"look": {"room": {"num": 54, "name": "First Royal Bank of Tos"}}, "inventory": {"items": []}},
            tool="bank",
            after_observation={"inventory": {"items": []}},
        )
        self.assertIn("banker said nothing", reason or "")

    def test_bank_withdrawal_refusal_is_no_progress_when_currency_does_not_increase(self) -> None:
        before = {
            "inventory": {"items": [{"id": 9, "name": "shilling", "amount": 400}]}
        }
        after = {
            "inventory": {"items": [{"id": 9, "name": "shilling", "amount": 400}]}
        }
        reason = BotController._no_progress_reason(
            {
                "action": "withdraw",
                "amount": 400,
                "balance": 133,
                "banker_said": ['Skivlat tells you, "But you only have 133 shillings in your account!"'],
            },
            before,
            tool="bank",
            after_observation=after,
        )
        self.assertIn("only have 133", reason or "")

    def test_bank_deposit_requires_carried_currency_to_decrease(self) -> None:
        unchanged = {"inventory": {"items": [{"id": 9, "name": "shilling", "amount": 400}]}}
        reason = BotController._no_progress_reason(
            {"action": "deposit", "amount": 100, "balance": None, "banker_said": []},
            unchanged,
            tool="bank",
            after_observation=unchanged,
        )
        self.assertIn("did not move", reason or "")

    def test_bank_withdrawal_currency_increase_is_verified_progress(self) -> None:
        before = {"inventory": {"items": [{"id": 9, "name": "shilling", "amount": 90}]}}
        after = {"inventory": {"items": [{"id": 9, "name": "shilling", "amount": 400}]}}
        reason = BotController._no_progress_reason(
            {"action": "withdraw", "amount": 400, "balance": None, "banker_said": []},
            before,
            tool="bank",
            after_observation=after,
        )
        self.assertIsNone(reason)

    def test_stale_no_banker_fingerprint_does_not_block_verified_bank(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(goal_payload(request_id="bank-stale-fingerprint"))["goal"]
                observation = {
                    "look": {"room": {"num": 54, "name": "First Royal Bank of Tos"}},
                    "inventory": {"items": []},
                }
                arguments = {"agent": "primary", "action": "withdraw", "amount": 205}
                controller._record_blocked_action(
                    goal,
                    observation,
                    "bank",
                    arguments,
                    "the banker said nothing, which almost always means there is no banker in this room",
                )

                self.assertIsNone(controller._blocked_action(goal, observation, "bank", arguments))
            finally:
                controller.storage.close()

    def test_fight_arguments_are_reduced_to_one_observable_swing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(goal_payload())['goal']

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "fight",
                        "arguments": {
                            "target": "giant rat",
                            "rounds": 9,
                            "swings_per_round": 4,
                            "disengage_at": 0.2,
                            "equip": False,
                            "loot": True,
                        },
                        "rationale": "Test the encounter cautiously.",
                    },
                )

                self.assertEqual("fight", result["action"])
                call = next(arguments for name, arguments in broker.calls if name == "fight")
                self.assertEqual(1, call["rounds"])
                self.assertEqual(1, call["swings_per_round"])
                self.assertEqual(0.7, call["disengage_at"])
                self.assertTrue(call["equip"])
                self.assertTrue(call["loot"])
                self.assertEqual(1, len(controller.storage.events(kinds=["action.safety_normalized"])["events"]))
                self.assertEqual(1, len(controller.learning.combat_summary()["recent"]))
            finally:
                controller.storage.close()

    def test_movement_relocalizes_immediately_after_standing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = PositionRefreshBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="movement-relocalization")
                )["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "travel",
                        "arguments": {"to": 54},
                        "rationale": "Travel to the verified bank.",
                    },
                )

                self.assertEqual("travel", result["action"])
                self.assertEqual(54, broker.room["num"])
                names = [name for name, _ in broker.calls]
                movement_index = names.index("travel")
                self.assertEqual(["rest", "look", "travel"], names[movement_index - 2 : movement_index + 1])
            finally:
                controller.storage.close()

    def test_position_unknown_movement_is_relocalized_and_retried_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = FlakyPositionRefreshBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="movement-bounded-retry")
                )["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "travel",
                        "arguments": {"to": 54},
                        "rationale": "Travel to the verified bank.",
                    },
                )

                self.assertEqual("travel", result["action"])
                self.assertEqual(54, broker.room["num"])
                self.assertEqual(
                    ["rest", "look", "travel", "look", "travel"],
                    [name for name, _ in broker.calls][-5:],
                )
                self.assertEqual(
                    1,
                    len(
                        controller.storage.events(
                            kinds=["action.movement_relocalized"]
                        )["events"]
                    ),
                )
            finally:
                controller.storage.close()

    def test_zero_currency_deposit_is_already_satisfied_without_broker_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="zero-currency-bank")
                )["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "bank",
                        "arguments": {"action": "deposit"},
                        "rationale": "Bank before danger.",
                    },
                )

                self.assertTrue(result["already_satisfied"])
                self.assertFalse(any(name == "bank" for name, _ in broker.calls))
                self.assertTrue(
                    controller._banking_resolved(goal, broker.observe())
                )
                self.assertEqual(
                    [], controller.storage.events(kinds=["action.no_progress"])["events"]
                )
                self.assertEqual([], controller.storage.goal_lessons())
            finally:
                controller.storage.close()

    def test_position_unknown_tactic_lessons_are_resolved_after_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="position-lesson-repair")
                )["goal"]
                deferred = controller.learning.defer_goal(
                    goal,
                    broker.observe(),
                    tool="travel",
                    arguments={"agent": "primary", "to": 54},
                    reason="own position unknown — call look first",
                    classification="route_unavailable",
                    scope="tactic",
                )
                controller._record_blocked_action(
                    goal,
                    broker.observe(),
                    "travel",
                    {"agent": "primary", "to": 54},
                    "own position unknown — call look first",
                )

                repaired = controller._repair_position_unknown_lessons()

                self.assertEqual(1, len(repaired))
                lesson = controller.storage.goal_lesson(deferred["lesson"]["id"])
                self.assertEqual("resolved", lesson["status"])
                self.assertEqual([], controller.storage.get_runtime("blocked_actions"))
            finally:
                controller.storage.close()

    def test_unbanked_currency_does_not_suppress_combat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            cfg = replace(cfg, policy=replace(cfg.policy, carried_currency_bank_threshold=1))
            controller = BotController(cfg)
            try:
                broker = CombatBroker()
                broker.inventory_items.append({"id": 2, "name": "shillings", "amount": 4})
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="bank-first", constraints={"bank_before_hazard": True})
                )["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {"tool": "fight", "arguments": {"target": "giant rat"}, "rationale": "Fight."},
                )

                self.assertNotIn("safety_suppressed", result)
                self.assertTrue(any(name == "fight" for name, _ in broker.calls))
            finally:
                controller.storage.close()

    def test_shopping_goal_suppresses_deposit_until_inventory_purchase_is_met(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                broker.inventory_items.append({"id": 2, "name": "shillings", "amount": 205})
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="shopping-funds",
                        title="Acquire four Flasks",
                        objective="Purchase four Flasks in a safe city shop.",
                        success_criteria=[
                            {"id": "flasks", "kind": "inventory_contains", "item": "Flask", "count": 4}
                        ],
                    )
                )["goal"]

                blockers = controller._safety_preflight(
                    "bank",
                    {"action": "deposit", "amount": 205},
                    broker.observe(),
                    goal,
                )

                self.assertIn("retain_purchase_funds", {item["kind"] for item in blockers})
            finally:
                controller.storage.close()

    def test_bank_mutation_is_suppressed_outside_a_bank_room(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                broker.room = {"num": 53, "name": "Frisconar's Mysticals"}
                goal = controller.storage.submit_goal(goal_payload(request_id="bank-location"))["goal"]

                blockers = controller._safety_preflight(
                    "bank",
                    {"action": "withdraw", "amount": 336},
                    broker.observe(),
                    goal,
                )

                self.assertIn("bank_location_required", {item["kind"] for item in blockers})
            finally:
                controller.storage.close()

    def test_foreground_travel_cannot_enter_goal_owned_farm_room(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                broker.tools["travel"] = Tool(
                    "travel",
                    "Travel to a room.",
                    {
                        "type": "object",
                        "properties": {"agent": {"type": "string"}, "to": {"type": "integer"}},
                        "required": ["agent", "to"],
                    },
                )
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="keeper-owns-route",
                        constraints={
                            "operator_notes": "hunt=groundworm larva; assigned_room=557; use_safe_spots=true"
                        },
                    )
                )["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "travel",
                        "arguments": {"to": 557},
                        "rationale": "Bank first, then let the keeper own the route.",
                    },
                )

                self.assertTrue(result["safety_suppressed"])
                self.assertIn(
                    "keeper_owned_hazardous_travel",
                    {item["kind"] for item in result["blockers"]},
                )
                self.assertFalse(any(name == "travel" for name, _ in broker.calls))
            finally:
                controller.storage.close()

    def test_unbanked_currency_does_not_add_a_hazardous_travel_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            cfg = replace(cfg, policy=replace(cfg.policy, carried_currency_bank_threshold=1))
            controller = BotController(cfg)
            try:
                broker = CombatBroker()
                broker.inventory_items.append({"id": 2, "name": "shillings", "amount": 92})
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="bank-before-route",
                        constraints={
                            "bank_before_hazard": True,
                            "operator_notes": "hunt=groundworm larva; assigned_room=557; use_safe_spots=true",
                        },
                    )
                )["goal"]

                blockers = controller._safety_preflight(
                    "travel", {"to": 557}, broker.observe(), goal
                )

                self.assertNotIn(
                    "bank_before_hazard_travel",
                    {item["kind"] for item in blockers},
                )
                self.assertIn(
                    "keeper_owned_hazardous_travel",
                    {item["kind"] for item in blockers},
                )
            finally:
                controller.storage.close()

    def test_bank_resolution_travel_is_never_blocked_as_hazardous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            cfg = replace(cfg, policy=replace(cfg.policy, carried_currency_bank_threshold=1))
            controller = BotController(cfg)
            try:
                broker = CombatBroker()
                broker.inventory_items.append({"id": 2, "name": "shillings", "amount": 92})
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="bank-resolution",
                        constraints={"bank_before_hazard": True},
                    )
                )["goal"]
                controller._foreground_room_transition = lambda *_: {
                    "requested": 54,
                    "room_ids": [54],
                    "hazardous": True,
                    "grounded_rooms": [
                        {"room_id": 54, "name": "First Royal Bank of Tos", "hostile_spawn_count": 1}
                    ],
                }

                blockers = controller._safety_preflight(
                    "travel", {"to": 54}, broker.observe(), goal
                )

                self.assertNotIn(
                    "bank_before_hazard_travel", {item["kind"] for item in blockers}
                )
            finally:
                controller.storage.close()

    def test_static_spawn_hazard_uses_role_not_npc_level(self) -> None:
        self.assertFalse(
            BotController._spawn_is_hostile(
                {"creature": "TosBanker", "level": 25, "role": "merchant"}
            )
        )
        self.assertTrue(
            BotController._spawn_is_hostile(
                {"creature": "Mummy", "level": 35, "role": "monster"}
            )
        )
        self.assertFalse(
            BotController._spawn_is_hostile({"creature": "Unknown NPC", "level": 50})
        )

    def test_repeated_safety_suppression_escalates_and_pauses_at_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                broker.tools["travel"] = Tool(
                    "travel",
                    "Travel to a room.",
                    {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            "to": {"type": "integer"},
                        },
                        "required": ["agent", "to"],
                    },
                )
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="bounded-safety-stall")
                )["goal"]
                controller._safety_preflight = lambda *_: [
                    {
                        "kind": "test_blocker",
                        "guidance": "choose a materially different action",
                    }
                ]

                result = {}
                for _ in range(controller.config.learning.wait_budget):
                    result = controller._execute(
                        goal,
                        broker.observe(),
                        {
                            "tool": "travel",
                            "arguments": {"to": 54},
                            "rationale": "Repeat the blocked route.",
                        },
                    )

                self.assertTrue(result["goal_paused"])
                self.assertEqual("paused", controller.storage.goal(goal["id"])["status"])
                stalled = controller.storage.events(kinds=["planner.stalled"])["events"]
                self.assertEqual(1, len(stalled))
                self.assertEqual(3, stalled[0]["data"]["same_blocker_count"])
                suppressions = controller.storage.events(
                    kinds=["action.safety_suppressed"]
                )["events"]
                self.assertEqual(3, len(suppressions))
                lessons = controller.storage.goal_lessons(
                    statuses=["deferred"], goal_id=goal["id"]
                )
                self.assertTrue(any(item["scope"] == "tactic" for item in lessons))
            finally:
                controller.storage.close()

    def test_character_status_exposes_complete_verified_character_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            broker = SimulatedBroker()
            controller.broker = broker
            observation = broker.observe()
            observation["observed_at"] = time.time()
            observation["status"]["character"] = "Sable"
            observation["status"]["attributes"] = {"might": 25, "intellect": 30}
            observation["inventory"] = {
                "items": [
                    {
                        "id": 7,
                        "name": "mace",
                        "amount": 1,
                        "in_use": True,
                        "slot": "hands",
                    },
                    {"id": 8, "name": "wheel of cheese", "amount": 3},
                ],
                "carry": {
                    "known": True,
                    "items": 2,
                    "load": {"weight": 8, "bulk": 4, "exact": True},
                    "weight_max": 50,
                    "bulk_max": 30,
                },
            }
            observation["equipment"] = {
                "known": True,
                "equipped": [{"id": 7, "name": "mace", "slot": "hands"}],
                "wielding": ["mace"],
            }
            observation["abilities"] = {
                "skills": [
                    {"name": f"Skill {index}", "ability": index}
                    for index in range(30)
                ],
                "spells": [
                    {"name": f"Spell {index}", "ability": index}
                    for index in range(27)
                ],
                "freshness": {"known": True, "from": "cache"},
            }
            observation["spells"] = {
                "spells": [
                    {
                        "name": "Spell 1",
                        "castable": False,
                        "blocked_by": ["mana"],
                    }
                ]
            }
            controller.last_observation = observation
            try:
                detail = controller.character_status()

                self.assertEqual("Sable", detail["game"]["character_name"])
                self.assertEqual(30, len(detail["abilities"]["skills"]))
                self.assertEqual(27, len(detail["abilities"]["spells"]))
                self.assertEqual(3, detail["inventory"]["items"][1]["quantity"])
                self.assertTrue(detail["inventory"]["items"][0]["equipped"])
                self.assertEqual(
                    ["mace"], detail["equipment"]["wielded_weapons"]
                )
                self.assertEqual(
                    "hands", detail["equipment"]["equipped"][0]["slot"]
                )
            finally:
                controller.storage.close()

    def test_supervision_status_is_compact_and_exposes_semantic_liveness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                controller.broker = broker
                controller.last_observation = broker.observe()
                controller.last_observation["observed_at"] = time.time()
                controller.last_observation["look"]["objects"] = [
                    {
                        "id": 700,
                        "name": "Claude Scout",
                        "is_player": True,
                        "distance": 2,
                        "relation": "neutral",
                    }
                ]
                controller.last_observation["abilities"] = {
                    "skills": [{"name": "Dodge", "ability": 12}],
                    "spells": [
                        {
                            "name": "Blink",
                            "ability": 5,
                            "school": "Riija",
                            "level": 1,
                            "mana": 15,
                        }
                    ],
                    "freshness": {
                        "from": "cache",
                        "age_ms": 250,
                        "known": {"skills": True, "spells": True},
                    },
                    "advancement": {
                        "changes_on_record": 1,
                        "recent": [
                            {
                                "kind": "skill",
                                "name": "Dodge",
                                "from": 11,
                                "to": 12,
                            }
                        ],
                        "atrophied": [],
                    },
                }
                controller.last_observation["spells"] = {
                    "known_spells": 1,
                    "castable_now": 0,
                    "your_mana": {"value": 10, "max": 18},
                    "spells": [
                        {
                            "name": "Blink",
                            "school": "Riija",
                            "level": 1,
                            "mana": 15,
                            "castable": False,
                            "blocked_by": ["mana 10/15"],
                        }
                    ],
                }
                active = controller.storage.submit_goal(
                    goal_payload(request_id="supervision-status")
                )["goal"]
                controller._begin_foreground_action(
                    "pvp_seek", goal_id=active["id"]
                )

                status = controller.status(
                    detail="supervision", include_recent_events=0
                )

                self.assertIn("now_local", status)
                self.assertEqual("foreground_action", status["controller"]["control_owner"])
                self.assertEqual("pvp_seek", status["controller"]["foreground_action"]["tool"])
                self.assertIn("liveness", status["attention"])
                self.assertEqual(
                    "controller_foreground_action",
                    status["attention"]["liveness"]["broker_keeper"]["control_owner"],
                )
                self.assertTrue(
                    status["attention"]["liveness"]["broker_keeper"]["suspension_expected"]
                )
                self.assertIn("readiness", status["campaign"])
                development = status["campaign"]["development"]
                self.assertEqual(12, development["skills"][0]["ability"])
                self.assertEqual(
                    "ability.skill.Dodge", development["skills"][0]["goal_metric"]
                )
                self.assertFalse(
                    development["spell_readiness"]["spells"][0]["castable"]
                )
                self.assertEqual("Claude Scout", status["game"]["visible_players"][0]["name"])
                pvp_today = status["campaign"]["pvp_today"]
                self.assertEqual("operator_goal_driven", pvp_today["policy"])
                self.assertIsNone(pvp_today["daily_limit"])
                self.assertTrue(pvp_today["opportunity"]["fresh_local_visibility"])
                self.assertNotIn("remaining", pvp_today)
                self.assertNotIn("campaign_memory", status)
                self.assertLess(len(str(status)), 15_000)
                controller._end_foreground_action()
            finally:
                controller.storage.close()

    def test_dashboard_projects_live_room_during_long_foreground_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = LiveForegroundStatusBroker()
                controller.broker = broker
                controller.last_observation = broker.observe()
                broker.room = {"num": 575, "name": "The King's Way"}
                controller._begin_foreground_action("travel", goal_id="goal-1")

                status = controller.status(detail="summary", include_recent_events=0)

                self.assertEqual("The King's Way", status["game"]["location"])
                self.assertLess(status["game"]["observation_age_seconds"], 1)
                progress = status["controller"]["foreground_action"]["progress"]
                self.assertEqual(575, progress["room_id"])
                self.assertEqual("The King's Way", progress["location"])
                self.assertEqual({"col": 12, "row": 34}, progress["position"])
                controller._end_foreground_action()
            finally:
                controller.storage.close()

    def test_paused_goal_supervision_uses_latest_global_recovery_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                controller.broker = broker
                controller.last_observation = broker.observe()
                paused = controller.storage.submit_goal(
                    goal_payload(request_id="paused-before-recovery")
                )["goal"]
                controller.storage.manage_goal(
                    {
                        "request_id": "pause-for-recovery",
                        "goal_id": paused["id"],
                        "expected_version": paused["version"],
                        "action": "pause",
                        "reason": "controller safety recovery",
                    }
                )
                recovery = controller.storage.submit_goal(
                    goal_payload(request_id="recovery-travel")
                )["goal"]
                controller.storage.emit_event(
                    "action.succeeded",
                    "Action succeeded: travel",
                    goal_id=recovery["id"],
                    data={"tool": "travel"},
                )
                controller.storage.set_goal_completion(
                    recovery["id"],
                    {"all_met": True, "percent_estimate": 100, "criteria": []},
                    terminal="succeeded",
                )

                status = controller.status(detail="supervision", include_recent_events=0)

                self.assertEqual("paused", status["goal"]["status"])
                self.assertEqual(
                    "travel",
                    status["attention"]["liveness"]["last_successful_action"]["tool"],
                )
                goal_status = controller.status(detail="goal", include_recent_events=0)
                self.assertEqual(paused["id"], goal_status["goal_detail"]["id"])
                self.assertEqual("paused", goal_status["goal_detail"]["status"])
                self.assertNotIn("campaign_memory", goal_status)
                self.assertLess(len(str(goal_status)), 15_000)
            finally:
                controller.storage.close()

    def test_unbanked_currency_does_not_suppress_background_farming(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            cfg = replace(cfg, policy=replace(cfg.policy, carried_currency_bank_threshold=1))
            controller = BotController(cfg)
            try:
                broker = CombatBroker()
                broker.inventory_items.append({"id": 2, "name": "shillings", "amount": 4})
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="bank-before-farm", constraints={"bank_before_hazard": True})
                )["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "autopilot",
                        "arguments": {"action": "start", "mode": "farm", "hunt": "giant rat"},
                        "rationale": "Farm for max health.",
                    },
                )

                self.assertNotIn("safety_suppressed", result)
                self.assertTrue(any(name == "autopilot" for name, _ in broker.calls))
                farm_call = next(
                    arguments
                    for name, arguments in broker.calls
                    if name == "autopilot" and arguments.get("mode") == "farm"
                )
                self.assertEqual(0, farm_call["bank_above"])
            finally:
                controller.storage.close()

    def test_verified_bank_receipt_suppresses_only_redundant_advice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            cfg = replace(cfg, policy=replace(cfg.policy, carried_currency_bank_threshold=1))
            controller = BotController(cfg)
            try:
                broker = CombatBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="walking-float-receipt",
                        constraints={"bank_before_hazard": True},
                    )
                )["goal"]
                before = broker.observe()
                before["inventory"]["items"].append(
                    {"id": 2, "name": "shillings", "amount": 4592}
                )
                after = broker.observe()
                after["look"]["room"] = {"num": 54, "name": "First Royal Bank of Tos"}
                after["inventory"]["items"].append(
                    {"id": 2, "name": "shillings", "amount": 400}
                )
                controller._record_bank_receipt(goal, before, after)

                allowed = controller._goal_advisories(goal, after)
                richer = {
                    **after,
                    "inventory": {
                        "items": [
                            item
                            for item in after["inventory"]["items"]
                            if "shilling" not in str(item.get("name", "")).casefold()
                        ]
                        + [{"id": 2, "name": "shillings", "amount": 401}]
                    },
                }
                suggested = controller._goal_advisories(goal, richer)

                self.assertNotIn(
                    "consider_banking", {item["kind"] for item in allowed}
                )
                self.assertIn(
                    "consider_banking", {item["kind"] for item in suggested}
                )
                successor = controller.storage.submit_goal(
                    goal_payload(request_id="walking-float-successor")
                )["goal"]
                self.assertTrue(controller._banking_resolved(successor, after))
                obsolete = controller.learning.defer_goal(
                    goal,
                    after,
                    tool="autopilot",
                    arguments={"action": "start", "mode": "farm"},
                    reason=(
                        "Repeated deterministic safety suppression exhausted the controller "
                        "wait budget: prefer banking carried currency before danger"
                    ),
                    classification="ineffective_tactic",
                    scope="tactic",
                )["lesson"]
                controller.last_observation = after
                controller._repair_bank_receipt()
                self.assertEqual(
                    "resolved", controller.storage.goal_lesson(obsolete["id"])["status"]
                )
            finally:
                controller.storage.close()

    def test_bank_receipt_repair_resumes_only_controller_paused_false_stall(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                broker.room = {"num": 54, "name": "First Royal Bank of Tos"}
                broker.inventory_items.append(
                    {"id": 2, "name": "shillings", "amount": 400}
                )
                controller.last_observation = broker.observe()
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="repair-walking-float-stall",
                        constraints={"bank_before_hazard": True},
                    )
                )["goal"]
                controller.storage.emit_event(
                    "action.succeeded",
                    "Action succeeded: bank",
                    goal_id=goal["id"],
                    data={"tool": "bank"},
                )
                lesson = controller.learning.defer_goal(
                    goal,
                    controller.last_observation,
                    tool="autopilot",
                    arguments={"action": "start", "mode": "farm"},
                    reason=(
                        "Repeated deterministic safety suppression exhausted the controller "
                        "wait budget: prefer banking carried currency before the next dangerous phase"
                    ),
                    classification="ineffective_tactic",
                    scope="tactic",
                )["lesson"]
                controller.storage.set_runtime(
                    "safety_suppression_v1",
                    {
                        "goal_id": goal["id"],
                        "blocker_kinds": ["bank_before_hazard"],
                        "same_blocker_count": 10,
                    },
                )
                controller.storage.manage_goal(
                    {
                        "request_id": "pause-false-bank-stall",
                        "goal_id": goal["id"],
                        "action": "pause",
                        "reason": "controller paused the goal after the same safety blocker repeated 10 times",
                    }
                )

                repaired = controller._repair_bank_receipt()

                self.assertEqual(goal["id"], repaired["resumed_goal_id"])
                self.assertEqual("active", controller.storage.goal(goal["id"])["status"])
                self.assertIsNone(controller.storage.get_runtime("safety_suppression_v1"))
                self.assertEqual("resolved", controller.storage.goal_lesson(lesson["id"])["status"])
            finally:
                controller.storage.close()

    def test_background_farm_waits_for_its_numeric_vigor_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                broker.vitals["vigor"] = {"value": 93, "scale_max": 200, "rested": True}
                controller.broker = broker
                goal = controller.storage.submit_goal(goal_payload(request_id="farm-vigor-gate"))["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "autopilot",
                        "arguments": {
                            "action": "start",
                            "mode": "farm",
                            "hunt": "giant rat",
                            "assigned_room": 575,
                            "fight_above_vigor": 140,
                        },
                        "rationale": "Launch the bounded farm.",
                    },
                )

                self.assertTrue(result["safety_suppressed"])
                self.assertIn("recover_combat_vigor", {item["kind"] for item in result["blockers"]})
                blocker = next(item for item in result["blockers"] if item["kind"] == "recover_combat_vigor")
                self.assertIn("resting stops at 80 vigor", blocker["guidance"])
                self.assertFalse(any(name == "autopilot" for name, _ in broker.calls))
            finally:
                controller.storage.close()

    def test_background_farm_allows_keeper_to_provision_from_carried_food(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                broker.vitals["vigor"] = {"value": 93, "scale_max": 200, "rested": True}
                broker.inventory_items.append({"id": 88, "name": "wheel of cheese", "amount": 2})
                controller.broker = broker
                goal = controller.storage.submit_goal(goal_payload(request_id="farm-food-provision"))["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "autopilot",
                        "arguments": {
                            "action": "start",
                            "mode": "farm",
                            "hunt": "giant rat",
                            "assigned_room": 586,
                            "fight_above_vigor": 140,
                        },
                        "rationale": "Let the keeper eat before leaving the sanctuary.",
                    },
                )

                self.assertEqual("autopilot", result["action"])
                self.assertNotIn("recover_combat_vigor", {item["kind"] for item in result.get("blockers", [])})
                self.assertTrue(any(name == "autopilot" for name, _ in broker.calls))
            finally:
                controller.storage.close()

    def test_background_farming_arguments_receive_survival_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                broker.inventory_items.extend(
                    {"id": 100 + index, "name": f"supply {index}"} for index in range(16)
                )
                controller.broker = broker
                goal = controller.storage.submit_goal(goal_payload(request_id="safe-farm"))["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "autopilot",
                        "arguments": {
                            "action": "start",
                            "mode": "farm",
                            "hunt": "giant rat",
                            "flee_below": 0.2,
                            "use_safe_spots": False,
                        },
                        "rationale": "Farm from a wall.",
                    },
                )

                self.assertEqual("autopilot", result["action"])
                call = next(arguments for name, arguments in broker.calls if name == "autopilot")
                self.assertEqual(0.7, call["rest_below"])
                self.assertEqual(0.60, call["flee_below"])
                self.assertEqual(100, call["fight_above_vigor"])
                self.assertTrue(call["use_safe_spots"])
                self.assertEqual(0.9, call["hold_resume_above"])
                self.assertFalse(call["break_out_via_logoff"])
                self.assertEqual(len(broker.inventory_items) + 6, call["max_carry"])
            finally:
                controller.storage.close()

    def test_nonfarm_keeper_owns_turn_until_recovered_and_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_mode = "survive"
                broker.vitals["health"] = {"value": 20, "max": 29}
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="survival-owner")
                )["goal"]

                recovering = controller._manage_background_farm(
                    goal, broker.observe(), {"all_met": False}
                )

                self.assertTrue(recovering["background_survival_monitoring"])
                self.assertFalse(
                    any(
                        name == "autopilot" and arguments.get("action") == "stop"
                        for name, arguments in broker.calls
                    )
                )

                broker.vitals["health"] = {"value": 29, "max": 29}
                stopping = controller._manage_background_farm(
                    goal, broker.observe(), {"all_met": False}
                )

                self.assertTrue(stopping["background_keeper_stopping"])
                self.assertTrue(
                    any(
                        name == "autopilot" and arguments.get("action") == "stop"
                        for name, arguments in broker.calls
                    )
                )
            finally:
                controller.storage.close()

    def test_inert_keeper_yields_campaign_control_under_new_stop_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_mode = "survive"
                broker.soft_stop_inert = True
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="inert-keeper-yields")
                )["goal"]

                stopping = controller._manage_background_farm(
                    goal, broker.observe(), {"all_met": False}
                )
                yielded = controller._manage_background_farm(
                    goal, broker.observe(), {"all_met": False}
                )

                self.assertTrue(stopping["background_keeper_stopping"])
                self.assertIsNone(yielded)
                self.assertEqual(
                    1,
                    sum(
                        1
                        for name, arguments in broker.calls
                        if name == "autopilot" and arguments.get("action") == "stop"
                    ),
                )
            finally:
                controller.storage.close()

    def test_inert_survival_keeper_is_revived_for_emergency_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_mode = "survive"
                broker.farm_inert = {"inert": True, "why": "foreground work"}
                controller.broker = broker

                result = controller._ensure_survival_keeper()

                self.assertTrue(result["survival_keeper_started"])
                self.assertFalse(result["already_running"])
                self.assertIsNone(broker.farm_inert)
                self.assertTrue(
                    any(
                        name == "autopilot" and arguments.get("action") == "start"
                        for name, arguments in broker.calls
                    )
                )
            finally:
                controller.storage.close()

    def test_durable_goal_can_select_open_field_strategy_after_wall_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                controller.broker = broker
                controller.storage.set_runtime(
                    "farm_tactic_quarantine_v1",
                    {
                        "575": {
                            "room": 575,
                            "target": "giant rat",
                            "reasons": ["live journal evidence disproved the safe spot"],
                            "guidance": "do not reuse the failed wall tactic",
                        }
                    },
                )
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="durable-open-field",
                        constraints={
                            "operator_notes": (
                                "Use hunt=giant rat, assigned_room=575, "
                                "use_safe_spots=false, flee_below=0.80, "
                                "bank_above=25, "
                                "break_out_via_logoff=false."
                            )
                        },
                    )
                )["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "autopilot",
                        "arguments": {
                            "action": "start",
                            "mode": "farm",
                            "hunt": "centipede",
                            "assigned_room": 554,
                            "use_safe_spots": True,
                        },
                        "rationale": "Planner accidentally repeated the old wall tactic.",
                    },
                )

                self.assertNotIn("safety_suppressed", result)
                call = next(
                    arguments
                    for name, arguments in broker.calls
                    if name == "autopilot" and arguments.get("action") == "start"
                )
                self.assertEqual("giant rat", call["hunt"])
                self.assertEqual(575, call["assigned_room"])
                self.assertFalse(call["use_safe_spots"])
                # Old durable recipes cannot restore the superseded 80%
                # boundary; operator policy owns the farm flee threshold.
                self.assertEqual(0.60, call["flee_below"])
                self.assertEqual(400, call["bank_above"])
            finally:
                controller.storage.close()

    def test_internal_farm_phase_controls_open_field_keeper_tactic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="campaign-open-field",
                        title="Raise maximum HP",
                        objective="Raise maximum HP to 110 through ordinary gameplay.",
                        success_criteria=[
                            {
                                "id": "hp-110",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 110,
                            }
                        ],
                    )
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "farm",
                        "objective": "Farm ants in room 26 with open-field engagement.",
                        "success_criteria": [
                            {
                                "id": "hp-101",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 101,
                            }
                        ],
                        "context": {
                            "target": "ant",
                            "room": 26,
                            # Compatibility coverage for a phase persisted by
                            # the earlier prompt before the boolean was required.
                            "strategy": "Use open-field engagement; no wall tactic.",
                        },
                        "rationale": "The wall strategy has no usable square here.",
                    },
                    mode="start",
                )

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "autopilot",
                        "arguments": {
                            "action": "start",
                            "mode": "farm",
                            "hunt": "giant rat",
                            "assigned_room": 575,
                            "use_safe_spots": True,
                        },
                        "rationale": "Launch the active internal farm phase.",
                    },
                )

                self.assertEqual("autopilot", result["action"])
                call = next(
                    arguments
                    for name, arguments in broker.calls
                    if name == "autopilot" and arguments.get("action") == "start"
                )
                self.assertEqual("ant", call["hunt"])
                self.assertEqual(26, call["assigned_room"])
                self.assertFalse(call["use_safe_spots"])
                self.assertEqual(0.60, call["flee_below"])
            finally:
                controller.storage.close()

    def test_farm_phase_without_executable_context_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                blocker = controller._campaign_phase_grounding_blocker(
                    {
                        "kind": "farm",
                        "objective": "Farm something somewhere.",
                        "context": {},
                    }
                )

                self.assertEqual("invalid_farm_phase_context", blocker["kind"])
                self.assertIn("context.room", blocker["guidance"])
                self.assertIn("context.target", blocker["guidance"])
                self.assertIn("context.use_safe_spots", blocker["guidance"])
            finally:
                controller.storage.close()

    def test_attackable_faction_troop_without_aggression_does_not_block_farm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.room = {"num": 586, "name": "Main gate to the city of Tos"}
                broker.vitals["health"] = {"current": 26, "max": 26}
                controller.broker = broker
                controller.knowledge = SimpleNamespace(
                    corpus_version="test-corpus",
                    resolve=lambda query, **_: {
                        "status": "found",
                        "entity": {
                            "id": "creature:duketroop",
                            "facts": {"level": 50},
                            "evidence": {"source_ref": "troop/duketr.kod:52"},
                        },
                    }
                )
                observation = broker.observe()
                observation["look"]["objects"] = [
                    {
                        "id": 7131,
                        "name": "soldier of the Duke's army",
                        "distance": 4,
                        "reachable": True,
                        "is_player": False,
                        "can": ["attack", "look"],
                    }
                ]
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="dynamic-faction-threat")
                )["goal"]

                blockers = controller._combat_preflight(
                    "autopilot",
                    {
                        "action": "start",
                        "mode": "farm",
                        "hunt": "giant rat",
                        "assigned_room": 586,
                    },
                    observation,
                    goal,
                )

                self.assertFalse(
                    any(
                        item["kind"] == "live_room_overlevel_hostile"
                        for item in blockers
                    )
                )

                observation["look"]["objects"][0]["relation"] = "hostile"
                hostile_blockers = controller._combat_preflight(
                    "autopilot",
                    {
                        "action": "start",
                        "mode": "farm",
                        "hunt": "giant rat",
                        "assigned_room": 586,
                    },
                    observation,
                    goal,
                )
                blocker = next(
                    item
                    for item in hostile_blockers
                    if item["kind"] == "live_room_overlevel_hostile"
                )
                self.assertEqual(50, blocker["hostiles"][0]["level"])
                self.assertEqual(32, blocker["hostiles"][0]["danger_limit"])
            finally:
                controller.storage.close()

    def test_running_farm_hands_off_on_dynamic_overlevel_hostile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.room = {"num": 586, "name": "Main gate to the city of Tos"}
                broker.vitals["health"] = {"current": 26, "max": 26}
                original_observe = broker.observe

                def observe_with_troop() -> dict[str, object]:
                    observation = original_observe()
                    observation["look"]["objects"] = [
                        {
                            "id": 7131,
                            "name": "hostile event summon",
                            "distance": 8,
                            "reachable": True,
                            "is_player": False,
                            "can": ["attack", "look"],
                        }
                    ]
                    return observation

                broker.observe = observe_with_troop  # type: ignore[method-assign]
                controller.broker = broker
                controller.knowledge = SimpleNamespace(
                    corpus_version="test-corpus",
                    resolve=lambda query, **_: {
                        "status": "found",
                        "entity": {
                            "id": "creature:hostileeventsummon",
                            "facts": {"level": 50},
                            "evidence": {"source_ref": "troop/duketr.kod:52"},
                        },
                    },
                    validate_goal=lambda goal: {"valid": True},
                )
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="running-dynamic-faction-threat",
                        objective="Reach 27 max HP.",
                        success_criteria=[
                            {
                                "id": "max-hp-27",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 27,
                            }
                        ],
                    )
                )["goal"]

                result = controller.turn()

                self.assertTrue(result["switched_to_survival"])
                self.assertEqual("paused", controller.storage.goal(goal["id"])["status"])
                quarantine = controller.storage.get_runtime("farm_tactic_quarantine_v1", {})
                self.assertEqual(
                    50, quarantine["586"]["live_overlevel_hostiles"][0]["level"]
                )
                events = controller.storage.goal_events(
                    goal["id"],
                    kinds=["background_farm.live_threat_detected"],
                    limit=10,
                )
                self.assertEqual(1, len(events))
                lesson = controller.storage.goal_lessons(statuses=["deferred"], limit=10)[0]
                self.assertEqual("ineffective_tactic", lesson["classification"])
                self.assertEqual("tactic", lesson["scope"])
            finally:
                controller.storage.close()

    def test_presence_only_faction_troop_quarantines_are_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                controller.storage.set_runtime(
                    "farm_tactic_quarantine_v1",
                    {
                        "586": {
                            "room": 586,
                            "target": "giant rat",
                            "reasons": [
                                "live dynamic level-50 Duke soldiers exceed TestHero danger limit 32"
                            ],
                            "live_overlevel_hostiles": [
                                {
                                    "entity_id": "creature:duketroop",
                                    "name": "soldier of the Duke's army",
                                }
                            ],
                        },
                        "575": {
                            "room": 575,
                            "target": "giant rat",
                            "reasons": ["live journal evidence disproved the safe spot"],
                        },
                    },
                )

                removed = controller._repair_false_faction_troop_quarantines()

                self.assertEqual([586], [item["room"] for item in removed])
                remaining = controller.storage.get_runtime(
                    "farm_tactic_quarantine_v1", {}
                )
                self.assertNotIn("586", remaining)
                self.assertIn("575", remaining)
                events = controller.storage.events(
                    after_cursor=0,
                    limit=20,
                    kinds=["background_farm.quarantine_corrected"],
                )["events"]
                self.assertEqual(1, len(events))
                self.assertEqual([586], events[0]["data"]["rooms"])
            finally:
                controller.storage.close()

    def test_survivability_quarantine_releases_after_verified_health_gain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                observation = SimulatedBroker().observe()
                controller.storage.set_runtime(
                    "combat_outcomes_v1",
                    [
                        {
                            "occurred_at": "2026-08-04T22:20:00.000Z",
                            "room": {"id": 557, "name": "The Sweet Grass Prairies"},
                            "target": "groundworm larva",
                            "health_after": {"max": 90, "value": 70},
                            "equipment_hash": "older-equipment",
                        }
                    ],
                )
                controller.storage.set_runtime(
                    "farm_tactic_quarantine_v1",
                    {
                        "557": {
                            "room": 557,
                            "assigned_room": 557,
                            "target": "groundworm larva",
                            "use_safe_spots": True,
                            "quarantined_at": "2026-08-04T22:22:00.000Z",
                            "reasons": ["health reached the keeper flee threshold"],
                        },
                        "603": {
                            "room": 603,
                            "target": "giant rat",
                            "quarantined_at": "2026-08-04T22:22:00.000Z",
                            "reasons": ["the keeper reported no safe spot available in room 603"],
                        },
                    },
                )

                released = controller._repair_capability_unlocked_farm_quarantines(
                    observation
                )

                self.assertEqual([557], [item["room"] for item in released])
                remaining = controller.storage.get_runtime(
                    "farm_tactic_quarantine_v1", {}
                )
                self.assertNotIn("557", remaining)
                self.assertIn("603", remaining)
                self.assertEqual(90, released[0]["baseline_max_health"])
                self.assertEqual(100, released[0]["current_max_health"])
            finally:
                controller.storage.close()

    def test_threshold_only_quarantine_releases_when_flee_policy_is_lowered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                controller.storage.set_runtime(
                    "farm_tactic_quarantine_v1",
                    {
                        "557": {
                            "room": 557,
                            "target": "groundworm larva",
                            "flee_threshold": 0.8,
                            "reasons": ["health reached the keeper flee threshold"],
                            "deltas": {"deaths": 0, "withdrawals": 0},
                        },
                        "535": {
                            "room": 535,
                            "target": "giant rat",
                            "flee_threshold": 0.8,
                            "reasons": ["health reached the keeper flee threshold"],
                            "deltas": {"deaths": 0, "withdrawals": 1},
                        },
                        "575": {
                            "room": 575,
                            "target": "giant rat",
                            "flee_threshold": 0.8,
                            "reasons": ["live journal evidence disproved the safe spot"],
                            "deltas": {"deaths": 0, "withdrawals": 0},
                        },
                    },
                )

                released = controller._repair_policy_obsolete_farm_quarantines()

                self.assertEqual([557], [item["room"] for item in released])
                remaining = controller.storage.get_runtime(
                    "farm_tactic_quarantine_v1", {}
                )
                self.assertNotIn("557", remaining)
                self.assertIn("535", remaining)
                self.assertIn("575", remaining)
                self.assertEqual(0.8, released[0]["prior_flee_threshold"])
                self.assertEqual(0.6, released[0]["current_flee_threshold"])
            finally:
                controller.storage.close()

    def test_survival_fallback_clears_retained_farm_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                controller.broker = broker

                controller._set_fallback()

                arguments = next(
                    args
                    for name, args in broker.calls
                    if name == "autopilot" and args.get("action") == "start"
                )
                self.assertEqual("survive", arguments["mode"])
                self.assertEqual("", arguments["hunt"])
                self.assertIsNone(arguments["assigned_room"])
                self.assertEqual(0, arguments["bank_above"])
                self.assertFalse(arguments["break_out_via_logoff"])
            finally:
                controller.storage.close()

    def test_startup_fallback_re_adopts_matching_active_farm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                controller.broker = broker
                submitted = controller.storage.submit_goal(
                    goal_payload(
                        "recover-farm",
                        title="Raise max HP",
                        objective="Raise maximum health with giant rats.",
                        activation="replace_active_pause",
                        constraints={
                            "operator_notes": (
                                "hunt='giant rat'; assigned_room=586"
                            )
                        },
                        success_criteria=[
                            {
                                "id": "hp",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 27,
                            }
                        ],
                    )
                )["goal"]

                controller._set_fallback()

                starts = [
                    args
                    for name, args in broker.calls
                    if name == "autopilot" and args.get("action") == "start"
                ]
                self.assertEqual([], starts)
                events = controller.storage.goal_events(
                    submitted["id"], kinds=["background_farm.recovered"], limit=5
                )
                self.assertEqual(1, len(events))
            finally:
                controller.storage.close()

    def test_successful_farm_launch_resets_prior_failure_handling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_running = False
                controller.broker = broker
                plan = {
                    "decision": "act",
                    "tool": "autopilot",
                    "arguments": {
                        "action": "start",
                        "mode": "farm",
                        "hunt": "giant rat",
                        "assigned_room": 586,
                        "use_safe_spots": False,
                        "flee_below": 0.85,
                        "hold_resume_above": 0.98,
                        "fight_above_vigor": 140,
                        "bank_above": 400,
                        "break_out_via_logoff": False,
                    },
                    "rationale": "Launch the corrected farm.",
                    "expected_observation": {},
                    "proposal": None,
                }
                goal = controller.storage.submit_goal(
                    goal_payload(
                        "reset-failure-marker",
                        title="Raise max HP",
                        objective="Raise maximum health with giant rats.",
                        activation="replace_active_pause",
                        constraints={
                            "operator_notes": (
                                "hunt='giant rat'; assigned_room=586; "
                                "use_safe_spots=false; flee_below=0.85; "
                                "hold_resume_above=0.98; fight_above_vigor=140; "
                                "bank_above=400; break_out_via_logoff=false"
                            )
                        },
                        success_criteria=[
                            {
                                "id": "hp",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 27,
                            }
                        ],
                    )
                )["goal"]
                controller.storage.set_runtime(
                    f"background_farm_failure_handled_v1:{goal['id']}", True
                )

                controller._execute(goal, broker.observe(), plan)

                self.assertFalse(
                    controller.storage.get_runtime(
                        f"background_farm_failure_handled_v1:{goal['id']}"
                    )
                )
            finally:
                controller.storage.close()

    def test_farm_safety_text_reads_current_broker_recent_shape(self) -> None:
        text = BotController._farm_journal_text(
            {
                "recent": [
                    {
                        "what": "no safe spot available here",
                        "consequence": "fighting in the open",
                    }
                ],
                "trials": [{"verdict": "not holding a spot — nothing to test"}],
            }
        )
        self.assertIn("no safe spot available", text)
        self.assertIn("fighting in the open", text)

    def test_unreachable_wall_for_current_quarry_is_not_a_disproved_safe_spot(self) -> None:
        status = {
            "recent": [
                {
                    "pass": 469,
                    "what": "no safe spot available here",
                    "consequence": "fighting in the open",
                    "why": "the defensible squares cannot be reached by what we are fighting",
                }
            ],
            "trials": [
                {
                    "pass": 469,
                    "verdict": "not holding a spot — nothing to test",
                    "lost": 0,
                    "swung_in_window": False,
                    "moved_in_window": False,
                    "adjacent_at_start": 0,
                }
            ],
        }

        self.assertFalse(BotController._farm_safe_spot_disproved(status, minimum_pass=444))

    def test_idle_damage_explicitly_disproves_a_safe_spot(self) -> None:
        status = {
            "recent": [
                {
                    "pass": 470,
                    "what": "THIS IS NOT A SAFE SPOT",
                    "lost_health": 2,
                    "attackers": 1,
                }
            ]
        }

        self.assertTrue(BotController._farm_safe_spot_disproved(status, minimum_pass=444))

    def test_safe_spot_failure_ids_deduplicate_mirrored_keeper_records(self) -> None:
        status = {
            "recent": [
                {
                    "pass": 470,
                    "what": "THIS IS NOT A SAFE SPOT",
                    "where": {"col": 12, "row": 9},
                    "lost_health": 2,
                }
            ],
            "trials": [
                {
                    "pass": 470,
                    "at_col": 12,
                    "at_row": 9,
                    "lost": 2,
                    "verdict": "this wall does not work",
                }
            ],
        }

        self.assertEqual(
            ["pass:470|col:12|row:9"],
            BotController._farm_safe_spot_failure_ids(status, minimum_pass=444),
        )

    def test_open_field_idle_damage_does_not_disprove_a_safe_spot(self) -> None:
        status = {
            "trials": [
                {
                    "pass": 531,
                    "verdict": "not holding a spot — nothing to test",
                    "lost": 2,
                    "swung_in_window": False,
                    "moved_in_window": False,
                    "adjacent_at_start": 1,
                }
            ]
        }

        self.assertFalse(BotController._farm_safe_spot_disproved(status, minimum_pass=518))

    def test_farm_safety_text_ignores_prior_session_records(self) -> None:
        text = BotController._farm_journal_text(
            {
                "journal": [
                    {"pass": 90, "what": "no safe spot available here"},
                    {"pass": 101, "what": "this safe spot works"},
                ],
                "recent": [{"what": "old unscoped warning"}],
            },
            minimum_pass=100,
        )

        self.assertNotIn("no safe spot available", text)
        self.assertNotIn("old unscoped warning", text)
        self.assertIn("this safe spot works", text)

    def test_combat_start_reobserves_vitals_after_model_latency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(goal_payload(request_id="fresh-combat-preflight"))["goal"]
                stale_observation = broker.observe()
                broker.vitals["health"] = {"current": 5, "max": 100}

                result = controller._execute(
                    goal,
                    stale_observation,
                    {
                        "tool": "autopilot",
                        "arguments": {"action": "start", "mode": "farm", "hunt": "giant rat"},
                        "rationale": "Start farming after a slow planning call.",
                    },
                )

                self.assertTrue(result["safety_suppressed"])
                self.assertIn("recover_health", {item["kind"] for item in result["blockers"]})
                self.assertFalse(any(name == "autopilot" for name, _ in broker.calls))
            finally:
                controller.storage.close()

    def test_running_background_farm_exclusively_owns_hp_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                controller.broker = broker
                controller.model = FixedModel()  # type: ignore[assignment]
                controller.storage.submit_goal(
                    goal_payload(
                        request_id="keeper-owned-phase",
                        objective="Reach 101 max HP.",
                        success_criteria=[
                            {
                                "id": "max-hp-101",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 101,
                            }
                        ],
                    )
                )

                result = controller.turn()

                self.assertTrue(result["background_farm_monitoring"])
                self.assertTrue(broker.farm_running)
                self.assertEqual(1, len(broker.inventory_items))
                self.assertFalse(any(name == "act" for name, _ in broker.calls))
                first_status = next(
                    arguments
                    for name, arguments in broker.calls
                    if name == "autopilot" and arguments.get("action") == "status"
                )
                self.assertTrue(first_status["full_journal"])

                controller.turn()
                status_calls = [
                    arguments
                    for name, arguments in broker.calls
                    if name == "autopilot" and arguments.get("action") == "status"
                ]
                self.assertFalse(status_calls[-1]["full_journal"])
            finally:
                controller.storage.close()

    def test_structured_farm_goal_launches_keeper_without_model_rediscovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 52)
                broker = BackgroundFarmBroker()
                broker.farm_running = False
                broker.room = {"num": 52, "name": "Familiars"}
                broker.vitals["vigor"] = {
                    "value": 160,
                    "scale_max": 200,
                    "rested": True,
                }
                broker.inventory_items[0]["in_use"] = True
                controller.broker = broker
                controller.model = FixedModel()  # type: ignore[assignment]
                controller.storage.submit_goal(
                    goal_payload(
                        request_id="structured-farm-fast-path",
                        objective="Reach 101 max HP.",
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
                            "avoid_death": True,
                            "bank_before_hazard": True,
                            "operator_notes": (
                                "hunt=giant rat; assigned_room=567; "
                                "use_safe_spots=true; flee_below=0.80; "
                                "fight_above_vigor=140; bank_above=400; "
                                "break_out_via_logoff=false"
                            ),
                        },
                    )
                )

                planned = controller.turn()
                self.assertTrue(planned.get("planned"), planned)
                result = controller.turn()

                self.assertEqual("autopilot", result["action"])
                launch = next(
                    arguments
                    for name, arguments in broker.calls
                    if name == "autopilot" and arguments.get("action") == "start"
                )
                self.assertEqual("giant rat", launch["hunt"])
                self.assertEqual(567, launch["assigned_room"])
                self.assertTrue(launch["use_safe_spots"])
                self.assertFalse(any(name in {"prey", "hunting_grounds"} for name, _ in broker.calls))
                self.assertEqual(1, len(broker.inventory_items))
            finally:
                controller.storage.close()

    def test_raza_farm_launches_from_raza_inn_without_routing_to_tos(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 1011)
                broker = BackgroundFarmBroker()
                broker.farm_running = False
                broker.room = {"num": 1011, "name": "Raza Inn"}
                broker.vitals["health"] = {"value": 20, "max": 20}
                broker.vitals["vigor"] = {
                    "value": 100,
                    "scale_max": 200,
                    "rested": True,
                }
                broker.inventory_items[0]["in_use"] = True
                controller.broker = broker
                controller.model = FixedModel()  # type: ignore[assignment]
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="raza-farm-fast-path",
                        objective="Raise maximum HP to 25 before leaving Raza.",
                        success_criteria=[
                            {
                                "id": "max-hp-25",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 25,
                            },
                            {"id": "left-raza", "kind": "operator_confirmed"},
                        ],
                    )
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                phase = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "farm",
                        "objective": "Farm mummies in the Raza Mausoleum.",
                        "success_criteria": [
                            {
                                "id": "phase-hp-22",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 22,
                            }
                        ],
                        "abandon_predicates": [],
                        "budget": {"max_actions": 120, "max_minutes": 60},
                        "context": {
                            "room": 1016,
                            "target": "mummy",
                            "use_safe_spots": True,
                            "fight_above_vigor": 100,
                        },
                        "rationale": "Use the regional sanctuary and nearby farm.",
                    },
                    mode="start",
                )
                controller._set_planner_feedback(
                    goal,
                    "The proposed execution plan failed deterministic verification: "
                    "the farm plan must travel to the verified regional sanctuary "
                    "(Raza Inn, room 1011) before its autopilot launch step.",
                    consecutive_plan_rejections=1,
                )

                planned = controller.turn()
                self.assertTrue(planned["planned"])
                self.assertIsNone(controller._planner_feedback(goal))
                result = controller.turn()

                self.assertEqual("autopilot", result.get("action"), result)
                self.assertFalse(any(name == "travel" for name, _ in broker.calls))
                launch = next(
                    arguments
                    for name, arguments in broker.calls
                    if name == "autopilot" and arguments.get("action") == "start"
                )
                self.assertEqual("mummy", launch["hunt"])
                self.assertEqual(1016, launch["assigned_room"])
                plan = controller._execution_plan(goal)
                self.assertEqual(phase["id"], plan["phase_id"])
                self.assertNotIn("room 52", plan["summary"])
            finally:
                controller.storage.close()

    def test_structured_farm_preparation_resolves_zero_cash_food_deadlock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 52)
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="structured-farm-provisioning",
                        objective="Reach 33 max HP.",
                        success_criteria=[
                            {
                                "id": "max-hp-33",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 33,
                            }
                        ],
                        constraints={
                            "avoid_death": True,
                            "bank_before_hazard": True,
                            "operator_notes": (
                                "hunt=groundworm larva; assigned_room=557; "
                                "use_safe_spots=true; flee_below=0.60; "
                                "fight_above_vigor=140; bank_above=400; "
                                "break_out_via_logoff=false"
                            ),
                        },
                    )
                )["goal"]
                controller.storage.emit_event(
                    "action.succeeded",
                    "Action succeeded: shop",
                    goal_id=goal["id"],
                    data={
                        "tool": "shop",
                        "result": {
                            "seller": 96,
                            "items": [
                                {"id": 98, "name": "wheel of cheese", "cost": 112}
                            ],
                        },
                    },
                )

                def observation(
                    room: int,
                    *,
                    currency: int = 0,
                    cheese: int = 0,
                    vigor: int = 78,
                    rested: bool = False,
                ) -> dict[str, object]:
                    items: list[dict[str, object]] = [
                        {"id": 7136, "name": "mace"}
                    ]
                    if currency:
                        items.append(
                            {"id": 2, "name": "shillings", "amount": currency}
                        )
                    if cheese:
                        items.append(
                            {"id": 88, "name": "wheel of cheese", "amount": cheese}
                        )
                    return {
                        "look": {
                            "room": {"num": room, "name": "Familiars" if room == 52 else "First Royal Bank of Tos"},
                            "vitals": {
                                "health": {"value": 31, "max": 31},
                                "vigor": {
                                    "value": vigor,
                                    "scale_max": 200,
                                    "rested": rested,
                                },
                            },
                        },
                        "status": {
                            "vitals": {
                                "health": {"value": 31, "max": 31},
                                "vigor": {
                                    "value": vigor,
                                    "scale_max": 200,
                                    "rested": rested,
                                },
                            }
                        },
                        "inventory": {"items": items},
                        "equipment": {
                            "known": True,
                            "equipped": [{"id": 7136, "name": "mace"}],
                        },
                    }

                completion = controller.criteria.evaluate(goal, observation(52))
                to_bank = controller._structured_farm_preparation_action(
                    goal, observation(52), completion
                )
                self.assertEqual("travel", to_bank["tool"])
                self.assertEqual({"to": 54}, to_bank["arguments"])

                withdraw = controller._structured_farm_preparation_action(
                    goal, observation(54), completion
                )
                self.assertEqual("bank", withdraw["tool"])
                self.assertEqual(
                    {"action": "withdraw", "amount": 112},
                    withdraw["arguments"],
                )

                back_to_inn = controller._structured_farm_preparation_action(
                    goal, observation(54, currency=112), completion
                )
                self.assertEqual("travel", back_to_inn["tool"])
                self.assertEqual({"to": 52}, back_to_inn["arguments"])

                controller._set_planner_feedback(
                    goal,
                    "The default route made no progress.",
                    blocked_action={
                        "tool": "travel",
                        "arguments": {"agent": "primary", "to": 52},
                        "room": 54,
                    },
                )
                explicit_route_retry = controller._structured_farm_preparation_action(
                    goal, observation(54, currency=112), completion
                )
                self.assertEqual(
                    {"to": 52, "max_hops": 25},
                    explicit_route_retry["arguments"],
                )
                controller._clear_planner_feedback()

                buy_food = controller._structured_farm_preparation_action(
                    goal,
                    observation(52, currency=112),
                    completion,
                )
                self.assertEqual("shop", buy_food["tool"])
                self.assertEqual([98], buy_food["arguments"]["buy_ids"])

                rest = controller._structured_farm_preparation_action(
                    goal, observation(52, cheese=1), completion
                )
                self.assertEqual("rest_up", rest["tool"])
                self.assertEqual(0.4, rest["arguments"]["to"])

                ready = observation(52, cheese=1, vigor=80, rested=True)
                self.assertIsNone(
                    controller._structured_farm_preparation_action(
                        goal, ready, completion
                    )
                )
                self.assertIsNotNone(
                    controller._structured_farm_launch_plan(goal, ready, completion)
                )
            finally:
                controller.storage.close()

    def test_structured_farm_requires_a_wielded_weapon_not_merely_armor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 52)
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="structured-farm-weapon-readiness",
                        objective="Reach 31 max HP.",
                        success_criteria=[
                            {
                                "id": "max-hp-31",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 31,
                            }
                        ],
                        constraints={
                            "bank_before_hazard": False,
                            "operator_notes": (
                                "hunt=groundworm larva; assigned_room=557; "
                                "use_safe_spots=true; fight_above_vigor=100"
                            ),
                        },
                    )
                )["goal"]
                armor_only = {
                    "look": {
                        "room": {"num": 52, "name": "Familiars"},
                        "vitals": {
                            "health": {"value": 30, "max": 30},
                            "vigor": {"value": 160, "scale_max": 200, "rested": True},
                        },
                    },
                    "status": {
                        "vitals": {
                            "health": {"value": 30, "max": 30},
                            "vigor": {"value": 160, "scale_max": 200, "rested": True},
                        }
                    },
                    "inventory": {
                        "items": [{"id": 10, "name": "leather armor", "can": ["unuse"]}]
                    },
                    "equipment": {
                        "known": True,
                        "equipped": [{"id": 10, "name": "leather armor"}],
                        "wielding": None,
                    },
                }
                completion = controller.criteria.evaluate(goal, armor_only)

                self.assertIsNone(
                    controller._structured_farm_preparation_action(
                        goal, armor_only, completion
                    )
                )
                self.assertIsNone(
                    controller._structured_farm_launch_plan(
                        goal, armor_only, completion
                    )
                )

                carrying_mace = copy.deepcopy(armor_only)
                carrying_mace["inventory"]["items"].append(
                    {"id": 11, "name": "mace", "can": ["use"]}
                )
                equip = controller._structured_farm_preparation_action(
                    goal, carrying_mace, completion
                )
                self.assertEqual("equip_best", equip["tool"])
            finally:
                controller.storage.close()

    def test_deferred_equip_best_is_hidden_from_planner_and_fast_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            controller.broker = UnwieldableWeaponBroker()
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="deferred-equip-best",
                        objective="Reach 31 max HP.",
                        success_criteria=[
                            {
                                "id": "max-hp-31",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 31,
                            }
                        ],
                        constraints={
                            "bank_before_hazard": False,
                            "operator_notes": (
                                "hunt=groundworm larva; assigned_room=557; "
                                "use_safe_spots=true; fight_above_vigor=100"
                            ),
                        },
                    )
                )["goal"]
                observation = {
                    "look": {
                        "room": {"num": 52, "name": "Familiars"},
                        "vitals": {
                            "health": {"value": 30, "max": 30},
                            "vigor": {
                                "value": 160,
                                "scale_max": 200,
                                "rested": True,
                            },
                        },
                    },
                    "status": {
                        "vitals": {
                            "health": {"value": 30, "max": 30},
                            "vigor": {
                                "value": 160,
                                "scale_max": 200,
                                "rested": True,
                            },
                        }
                    },
                    "inventory": {
                        "items": [
                            {"id": 10, "name": "leather armor", "can": ["unuse"]},
                            {"id": 11, "name": "mace", "can": ["use", "drop"]},
                        ]
                    },
                    "equipment": {
                        "known": True,
                        "equipped": [{"id": 10, "name": "leather armor"}],
                        "wielding": None,
                    },
                }
                controller.last_observation = observation
                controller.learning.defer_goal(
                    goal,
                    observation,
                    tool="equip_best",
                    arguments={},
                    reason="every candidate is broken; fighting bare-handed",
                    classification="missing_capability",
                    scope="tactic",
                    block=False,
                )

                planner_tool_names = {
                    tool["name"]
                    for tool in controller._planner_tools({"kind": "prepare_combat"})
                }
                completion = controller.criteria.evaluate(goal, observation)

                self.assertNotIn("equip_best", planner_tool_names)
                self.assertIsNone(
                    controller._structured_farm_preparation_action(
                        goal, observation, completion
                    )
                )
            finally:
                controller.storage.close()

    def test_structured_farm_does_not_force_banking_existing_cash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            cfg = replace(
                cfg,
                policy=replace(cfg.policy, carried_currency_bank_threshold=1),
            )
            controller = BotController(cfg)
            try:
                source_verify_safe_rooms(controller, 52)
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="structured-farm-bank-existing-cash",
                        objective="Reach 31 max HP.",
                        success_criteria=[
                            {
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 31,
                            }
                        ],
                        constraints={
                            "bank_before_hazard": True,
                            "operator_notes": (
                                "hunt=groundworm larva; assigned_room=557; "
                                "use_safe_spots=true; fight_above_vigor=100"
                            ),
                        },
                    )
                )["goal"]

                def ready(room: int) -> dict[str, object]:
                    return {
                        "look": {
                            "room": {
                                "num": room,
                                "name": "First Royal Bank of Tos" if room == 54 else "Familiars",
                            },
                            "vitals": {
                                "health": {"value": 30, "max": 30},
                                "vigor": {"value": 160, "scale_max": 200, "rested": True},
                            },
                        },
                        "status": {
                            "vitals": {
                                "health": {"value": 30, "max": 30},
                                "vigor": {"value": 160, "scale_max": 200, "rested": True},
                            }
                        },
                        "inventory": {
                            "items": [
                                {"id": 11, "name": "mace", "can": ["unuse"]},
                                {"id": 2, "name": "shillings", "amount": 52},
                            ]
                        },
                        "equipment": {
                            "known": True,
                            "equipped": [{"id": 11, "name": "mace"}],
                            "wielding": ["mace"],
                        },
                    }

                completion = controller.criteria.evaluate(goal, ready(52))
                preparation = controller._structured_farm_preparation_action(
                    goal, ready(52), completion
                )
                self.assertIsNone(preparation)

                return_to_inn = controller._structured_farm_preparation_action(
                    goal, ready(54), completion
                )
                self.assertEqual("travel", return_to_inn["tool"])
                self.assertEqual({"to": 52}, return_to_inn["arguments"])
            finally:
                controller.storage.close()

    def test_invalid_optional_plan_revision_keeps_verified_plan_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                controller.broker = SimulatedBroker()
                controller.model = InvalidRevisionModel()  # type: ignore[assignment]
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="retain-plan-after-invalid-revision")
                )["goal"]
                grounding = controller.knowledge.validate_goal(goal)
                controller._store_execution_plan(
                    goal,
                    with_safe_ending({
                        "summary": "Drop the requested item.",
                        "steps": [
                            {
                                "id": "drop-item",
                                "outcome": "Drop the requested item.",
                                "tool": "act",
                                "verification": "The item is absent from inventory.",
                            }
                        ],
                        "assumptions": [],
                        "revision_reason": None,
                    }, 100),
                    grounding=grounding,
                    revision=False,
                )

                result = controller.turn()

                self.assertTrue(result["plan_rejected"])
                self.assertIsNotNone(controller._execution_plan(goal))
                feedback = controller.status()["attention"]["planner_feedback"]
                self.assertIn("existing execution plan remains verified", feedback["message"].casefold())
                self.assertIn("drop-item", feedback["message"])
                self.assertEqual(1, feedback["consecutive_plan_rejections"])
            finally:
                controller.storage.close()

    def test_successful_action_clears_stale_planner_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                controller.broker = SimulatedBroker()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="clear-feedback-after-success")
                )["goal"]
                controller._set_planner_feedback(
                    goal,
                    "The previous route failed.",
                    blocked_action={
                        "tool": "travel",
                        "arguments": {"agent": "primary", "to": 52},
                        "room": 54,
                    },
                )

                result = controller._execute(
                    goal,
                    controller.broker.observe(),
                    {
                        "tool": "act",
                        "arguments": {"verb": "drop", "target": 1},
                        "rationale": "Complete the active goal.",
                        "plan_step_id": "drop-item",
                    },
                )

                self.assertEqual("act", result["action"])
                self.assertIsNone(controller._planner_feedback(goal))
            finally:
                controller.storage.close()

    def test_malformed_farm_contract_blocks_without_poisoning_goal_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_running = False
                broker.room = {"num": 52, "name": "Familiars"}
                controller.broker = broker
                submitted = controller.storage.submit_goal(
                    goal_payload(
                        request_id="malformed-farm-contract",
                        objective="Reach 101 max HP.",
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
                            "operator_notes": "Use room 567 with an assigned_room."
                        },
                    )
                )["goal"]

                result = controller.turn()

                self.assertTrue(result["replacement_allowed"])
                self.assertEqual("blocked", controller.storage.goal(submitted["id"])["status"])
                self.assertEqual([], controller.storage.goal_lessons(statuses=["deferred"], limit=20))
                self.assertFalse(any(name in {"prey", "hunting_grounds"} for name, _ in broker.calls))
            finally:
                controller.storage.close()

    def test_contract_repair_resolves_legacy_goal_scoped_loop_lesson(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_running = False
                broker.room = {"num": 52, "name": "Familiars"}
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="legacy-malformed-farm-loop",
                        objective="Reach 101 max HP.",
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
                            "operator_notes": "Use room 567 with an assigned_room."
                        },
                    )
                )["goal"]
                lesson = controller.learning.defer_goal(
                    goal,
                    broker.observe(),
                    tool="hunting_grounds",
                    arguments={"creature": "groundworm larva"},
                    reason="Failure budget exhausted without verified goal progress",
                    classification="ineffective_tactic",
                    scope="goal",
                )["lesson"]

                repaired = controller._repair_invalid_farm_contract_lessons()

                self.assertEqual([lesson["id"]], [item["id"] for item in repaired])
                self.assertEqual(
                    "resolved", controller.storage.goal_lesson(lesson["id"])["status"]
                )
            finally:
                controller.storage.close()

    def test_running_farm_owned_by_cancelled_goal_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="replacement-farm-owner")
                )["goal"]
                controller.storage.set_runtime(
                    "background_farm_owner_v1",
                    {
                        "goal_id": "cancelled-prior-goal",
                        "assigned_room": 586,
                        "hunt": "giant rat",
                    },
                )

                result = controller.turn()

                self.assertTrue(result["background_farm_stale_stopped"])
                self.assertFalse(broker.farm_running)
                self.assertIn(
                    "different durable goal", " ".join(result["mismatch"]["reasons"])
                )
                self.assertEqual(
                    {}, controller.storage.get_runtime("background_farm_owner_v1", {})
                )
                events = controller.storage.goal_events(
                    goal["id"], kinds=["background_farm.owner_mismatch"], limit=10
                )
                self.assertEqual(1, len(events))
            finally:
                controller.storage.close()

    def test_legacy_running_farm_must_match_structured_goal_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_room = 575
                controller.broker = broker
                controller.storage.submit_goal(
                    goal_payload(
                        request_id="replacement-farm-signature",
                        constraints={
                            "operator_notes": (
                                "Use hunt=centipede, assigned_room=554, "
                                "flee_below=0.75 and use_safe_spots=true."
                            )
                        },
                    )
                )

                result = controller.turn()

                self.assertTrue(result["background_farm_stale_stopped"])
                self.assertFalse(broker.farm_running)
                reasons = " ".join(result["mismatch"]["reasons"])
                self.assertIn("assigned_room", reasons)
                self.assertIn("hunt", reasons)
            finally:
                controller.storage.close()

    def test_background_farm_stops_at_bounded_hp_target_before_planning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.vitals["health"] = {"current": 101, "max": 101}
                controller.broker = broker
                controller.model = FixedModel()  # type: ignore[assignment]
                controller.storage.submit_goal(
                    goal_payload(
                        request_id="keeper-target-reached",
                        objective="Reach 101 max HP and retain an item.",
                        success_criteria=[
                            {
                                "id": "max-hp-101",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 101,
                            },
                            {
                                "id": "retain-item",
                                "kind": "inventory_contains",
                                "item": "not carried",
                            },
                        ],
                    )
                )

                result = controller.turn()

                self.assertTrue(result["background_farm_stopped"])
                self.assertFalse(broker.farm_running)
                self.assertEqual(1, len(broker.inventory_items))
                self.assertFalse(any(name == "act" for name, _ in broker.calls))
            finally:
                controller.storage.close()

    def test_stopped_farm_route_failure_pauses_phase_and_promotes_queued_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_running = False
                broker.farm_room = 557
                broker.farm_hunt = "groundworm larva"
                broker.room = {"num": 568, "name": "Lake of Jala's Song"}
                broker.farm_placement = {
                    "assigned_room": None,
                    "failed": 1,
                    "why_not": [
                        {
                            "room": 557,
                            "why": "every square for that exit refused (3 tried)",
                        }
                    ],
                }
                broker.farm_journal = [
                    {
                        "activity": "stopped",
                        "detail": "every square for that exit refused (3 tried)",
                    }
                ]
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="failed-farm-route",
                        objective="Raise max HP to 101.",
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
                            "operator_notes": (
                                "Use hunt=groundworm larva, assigned_room=557, "
                                "flee_below=0.60 and use_safe_spots=true."
                            )
                        },
                    )
                )["goal"]
                queued = controller.storage.submit_goal(
                    goal_payload(request_id="queued-skill-work", priority=40)
                )["goal"]
                controller.storage.set_runtime(
                    "background_farm_owner_v1",
                    {
                        "goal_id": goal["id"],
                        "assigned_room": 557,
                        "hunt": "groundworm larva",
                        "origin_room": 52,
                    },
                )
                observation = broker.observe()

                result = controller._manage_background_farm(
                    goal,
                    observation,
                    controller.criteria.evaluate(goal, observation),
                )

                self.assertIsNotNone(result)
                self.assertTrue(result["background_farm_route_failed"])
                self.assertTrue(result["goal_paused"])
                self.assertEqual("paused", controller.storage.goal(goal["id"])["status"])
                self.assertEqual(queued["id"], controller.storage.active_goal()["id"])
                stagnations = controller.storage.get_runtime(
                    "farm_tactic_stagnation_v1", {}
                )
                self.assertIn(
                    f"{goal['id']}|557|groundworm larva", stagnations
                )
                self.assertTrue(
                    stagnations[f"{goal['id']}|557|groundworm larva"][
                        "stalled_in_transit"
                    ]
                )
                self.assertEqual(
                    "route_unavailable", result["lesson"]["classification"]
                )
                events = controller.storage.goal_events(
                    goal["id"], kinds=["background_farm.route_failed"], limit=10
                )
                self.assertEqual(1, len(events))
            finally:
                controller.storage.close()

    def test_live_arrival_releases_retained_farm_route_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_running = False
                broker.farm_hunt = "groundworm larva"
                broker.room = {"num": 568, "name": "Lake of Jala's Song"}
                broker.farm_placement = {
                    "assigned_room": None,
                    "failed": 1,
                    "why_not": [
                        {
                            "room": 557,
                            "why": "every square for that exit refused (3 tried)",
                        }
                    ],
                }
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="recovered-farm-route",
                        objective="Raise max HP to 101.",
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
                            "operator_notes": (
                                "hunt=groundworm larva; assigned_room=557; "
                                "use_safe_spots=true; fight_above_vigor=100"
                            )
                        },
                    )
                )["goal"]
                queued = controller.storage.submit_goal(
                    goal_payload(request_id="work-while-route-paused", priority=40)
                )["goal"]
                lesson = controller.learning.defer_goal(
                    goal,
                    broker.observe(),
                    tool="autopilot",
                    arguments={
                        "action": "start",
                        "mode": "farm",
                        "hunt": "groundworm larva",
                        "assigned_room": 557,
                    },
                    reason="keeper route failed before assigned_room=557",
                    classification="route_unavailable",
                    scope="tactic",
                    block=False,
                )["lesson"]
                stagnation_key = f"{goal['id']}|557|groundworm larva"
                controller.storage.set_runtime(
                    "farm_tactic_stagnation_v1",
                    {
                        stagnation_key: {
                            "goal_id": goal["id"],
                            "room": 557,
                            "assigned_room": 557,
                            "stalled_in_transit": False,
                            "target": "groundworm larva",
                        }
                    },
                )
                controller.storage.manage_goal(
                    {
                        "request_id": "pause-before-live-arrival",
                        "goal_id": goal["id"],
                        "action": "pause",
                        "reason": "controller paused after route failure",
                    }
                )
                self.assertEqual(queued["id"], controller.storage.active_goal()["id"])

                broker.room = {"num": 52, "name": "Familiars"}
                repaired = controller._repair_recovered_farm_route_evidence(
                    broker.observe()
                )

                self.assertEqual(1, len(repaired))
                self.assertEqual("queued", controller.storage.goal(goal["id"])["status"])
                self.assertEqual(queued["id"], controller.storage.active_goal()["id"])
                self.assertEqual("resolved", controller.storage.goal_lesson(lesson["id"])["status"])
                self.assertNotIn(
                    stagnation_key,
                    controller.storage.get_runtime("farm_tactic_stagnation_v1", {}),
                )
                broker.room = {"num": 557, "name": "The Sweet Grass Prairies"}
                self.assertIsNone(
                    controller._handle_stopped_farm_route_failure(
                        goal,
                        broker.observe(),
                        {
                            "running": False,
                            "mode": "survive",
                            "placement": broker.farm_placement,
                            "policy": {"hunt": "", "assignedRoom": None},
                        },
                        controller.criteria.evaluate(goal, broker.observe()),
                    )
                )
            finally:
                controller.storage.close()

    def test_game_action_timeouts_are_independent_from_model_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                self.assertEqual(5.0, controller._broker_action_timeout("act"))
                self.assertEqual(120.0, controller._broker_action_timeout("go_through"))
                self.assertEqual(300.0, controller._broker_action_timeout("rest_up"))
                self.assertEqual(600.0, controller._broker_action_timeout("travel"))
            finally:
                controller.storage.close()

    def test_stalled_background_farm_defers_unchanged_room_tactic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_stalled = {"idle_passes": 5, "why": "broke off without a kill"}
                broker.farm_room = 575
                broker.room = {"num": 575, "name": "The King's Way"}
                controller.broker = broker
                controller.model = FixedModel()  # type: ignore[assignment]
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="stagnated-farm")
                )["goal"]

                stopped = controller.turn()

                self.assertTrue(stopped["background_farm_stopped"])
                stagnations = controller.storage.get_runtime("farm_tactic_stagnation_v1", {})
                self.assertIn(f"{goal['id']}|575|giant rat", stagnations)

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "autopilot",
                        "arguments": {
                            "action": "start",
                            "mode": "farm",
                            "hunt": "giant rat",
                            "assigned_room": 575,
                        },
                        "rationale": "Restart the unchanged stalled tactic.",
                    },
                )

                self.assertTrue(result["safety_suppressed"])
                self.assertIn("stagnated_farm_tactic", {item["kind"] for item in result["blockers"]})
                restart_calls = [
                    args
                    for name, args in broker.calls
                    if name == "autopilot" and args.get("action") == "start"
                ]
                self.assertEqual([], restart_calls)
            finally:
                controller.storage.close()

    def test_transit_stall_does_not_defer_the_unreached_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_stalled = {
                    "idle_passes": 5,
                    "why": "departure-room geometry refused movement",
                }
                broker.farm_room = 586
                broker.room = {"num": 575, "name": "The King's Way"}
                controller.broker = broker
                controller.model = FixedModel()  # type: ignore[assignment]
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="transit-stagnation")
                )["goal"]

                stopped = controller.turn()

                self.assertTrue(stopped["background_farm_stopped"])
                stagnations = controller.storage.get_runtime("farm_tactic_stagnation_v1", {})
                self.assertIn(f"{goal['id']}|575|giant rat", stagnations)
                self.assertNotIn(f"{goal['id']}|586|giant rat", stagnations)
                evidence = stagnations[f"{goal['id']}|575|giant rat"]
                self.assertTrue(evidence["stalled_in_transit"])
                self.assertEqual(586, evidence["requested_assigned_room"])

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "autopilot",
                        "arguments": {
                            "action": "start",
                            "mode": "farm",
                            "hunt": "giant rat",
                            "assigned_room": 586,
                        },
                        "rationale": "Launch after reaching the intended destination.",
                    },
                )

                self.assertNotIn("safety_suppressed", result)
                self.assertTrue(
                    any(
                        name == "autopilot"
                        and args.get("action") == "start"
                        and args.get("assigned_room") == 586
                        for name, args in broker.calls
                    )
                )
            finally:
                controller.storage.close()

    def test_one_continuous_injury_is_one_survival_incident(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.vitals["health"] = {"current": 20, "max": 100}
                controller.broker = broker
                controller.model = FixedModel()  # type: ignore[assignment]
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="one-survival-incident")
                )["goal"]

                first = controller.turn()
                second = controller.turn()

                self.assertTrue(first["survival_interrupt"])
                self.assertTrue(second["idle"])
                self.assertEqual("survive", broker.farm_mode)
                events = controller.storage.goal_events(goal["id"], kinds=["survival.interrupt"], limit=20)
                self.assertEqual(1, len(events))
                self.assertEqual("paused", controller.storage.goal(goal["id"])["status"])
            finally:
                controller.storage.close()

    def test_foreground_critical_health_starts_survival_keeper_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_running = False
                broker.vitals["health"] = {"current": 20, "max": 100}
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="foreground-survival-start")
                )["goal"]

                first = controller.turn()
                second = controller.turn()

                self.assertTrue(first["survival_interrupt"])
                self.assertTrue(first["background_farm"]["survival_keeper_started"])
                self.assertTrue(second["survival_interrupt"])
                self.assertTrue(second["background_farm"]["already_running"])
                starts = [
                    arguments
                    for name, arguments in broker.calls
                    if name == "autopilot" and arguments.get("action") == "start"
                ]
                self.assertEqual(1, len(starts))
                self.assertEqual("survive", starts[0]["mode"])
                self.assertEqual("", starts[0]["hunt"])
                self.assertIsNone(starts[0]["assigned_room"])
                self.assertEqual(0, starts[0]["bank_above"])
                self.assertFalse(starts[0]["break_out_via_logoff"])
                self.assertEqual("active", controller.storage.goal(goal["id"])["status"])
                events = controller.storage.goal_events(
                    goal["id"], kinds=["survival.interrupt"], limit=20
                )
                self.assertEqual(1, len(events))
            finally:
                controller.storage.close()

    def test_background_farm_hands_off_at_keeper_flee_threshold_and_quarantines_room(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_room = 535
                broker.room = {"num": 535, "name": "West Merchant Way through Ilerian Woods"}
                broker.vitals["health"] = {"current": 19, "max": 26}
                broker.farm_did.update({"kills": 4, "withdrawals": 1})
                broker.farm_journal = [
                    {
                        "at": 1000 + index,
                        "pass": 10 + index,
                        "what": "killed",
                        "target": "giant rat",
                    }
                    for index in range(4)
                ]
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="keeper-flee-boundary",
                        objective="Reach 27 max HP.",
                        success_criteria=[
                            {
                                "id": "max-hp-27",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 27,
                            }
                        ],
                    )
                )["goal"]

                result = controller.turn()

                self.assertTrue(result["switched_to_survival"])
                self.assertTrue(result["goal_paused"])
                self.assertEqual("survive", broker.farm_mode)
                self.assertEqual("paused", controller.storage.goal(goal["id"])["status"])
                quarantine = controller.storage.get_runtime("farm_tactic_quarantine_v1")
                self.assertIn("535", quarantine)
                self.assertEqual(4, controller.learning.combat_summary()["by_target"][0]["kills"])
                lesson = controller.storage.goal_lessons(statuses=["deferred"], goal_id=goal["id"])[0]
                self.assertEqual("ineffective_tactic", lesson["classification"])
                self.assertEqual("tactic", lesson["scope"])
                self.assertFalse(controller.learning.evaluate_retry(lesson, broker.observe())["met"])
                survive_call = [
                    arguments
                    for name, arguments in broker.calls
                    if name == "autopilot" and arguments.get("action") == "start"
                ][-1]
                self.assertFalse(survive_call["break_out_via_logoff"])
            finally:
                controller.storage.close()

    def test_one_disproved_safe_spot_is_tactical_evidence_not_a_survival_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_room = 575
                broker.room = {"num": 575, "name": "The King's Way"}
                broker.vitals["health"] = {"current": 27, "max": 27}
                broker.farm_journal = [
                    {
                        "pass": 470,
                        "what": "THIS IS NOT A SAFE SPOT",
                        "where": {"col": 40, "row": 33},
                        "lost_health": 3,
                    }
                ]
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="one-bad-wall",
                        objective="Reach 28 max HP.",
                        success_criteria=[
                            {
                                "id": "max-hp-28",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 28,
                            }
                        ],
                    )
                )["goal"]
                controller.storage.set_runtime(
                    "background_farm_owner_v1",
                    {
                        "goal_id": goal["id"],
                        "assigned_room": 575,
                        "hunt": "giant rat",
                    },
                )

                result = controller.turn()

                self.assertTrue(result["background_farm_monitoring"])
                self.assertTrue(broker.farm_running)
                self.assertEqual("active", controller.storage.goal(goal["id"])["status"])
                self.assertEqual(
                    {}, controller.storage.get_runtime("farm_tactic_quarantine_v1", {})
                )
                events = controller.storage.goal_events(
                    goal["id"], kinds=["background_farm.evidence"], limit=10
                )
                self.assertEqual(1, len(events))
                self.assertEqual([], events[0]["data"]["risk_reasons"])
                self.assertIn(
                    "disproved a safe spot", events[0]["data"]["tactic_warnings"][0]
                )
            finally:
                controller.storage.close()

    def test_prearrival_farm_withdrawal_is_route_tactic_not_goal_strength_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_room = 586
                broker.room = {"num": 575, "name": "The King's Way"}
                broker.farm_did["withdrawals"] = 1
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="prearrival-withdrawal",
                        objective="Reach 101 max HP.",
                        success_criteria=[
                            {
                                "id": "max-hp-101",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 101,
                            }
                        ],
                    )
                )["goal"]

                result = controller.turn()

                self.assertTrue(result["switched_to_survival"])
                lesson = controller.storage.goal_lessons(
                    statuses=["deferred"], goal_id=goal["id"]
                )[0]
                self.assertEqual("route_unavailable", lesson["classification"])
                self.assertEqual("tactic", lesson["scope"])
                self.assertFalse(result["quarantine"]["quarantined"])
                self.assertNotIn(
                    "586", controller.storage.get_runtime("farm_tactic_quarantine_v1", {})
                )
            finally:
                controller.storage.close()

    def test_legacy_prearrival_strength_lesson_is_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="legacy-transit-strength")
                )["goal"]
                deferred = controller.learning.defer_goal(
                    goal,
                    SimulatedBroker().observe(),
                    tool="autopilot",
                    arguments={"action": "start", "mode": "farm", "assigned_room": 544},
                    reason=(
                        "Hazardous transit to the assigned farm room exceeded verified survivability "
                        "before arrival: the keeper had to withdraw"
                    ),
                    classification="insufficient_combat_power",
                    scope="goal",
                    block=False,
                )

                repaired = controller._repair_transit_goal_lessons()

                self.assertEqual([deferred["lesson"]["id"]], [item["id"] for item in repaired])
                self.assertEqual(
                    "resolved",
                    controller.storage.goal_lesson(deferred["lesson"]["id"])["status"],
                )
            finally:
                controller.storage.close()

    def test_background_farm_learning_uses_actual_journal_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(goal_payload(request_id="actual-farm-targets"))["goal"]
                observation = broker.observe()
                status = broker.call_tool(
                    "autopilot", {"agent": "primary", "action": "status"}
                )
                status["did"]["kills"] = 2
                status["journal"] = [
                    {"at": 1001, "pass": 11, "what": "killed", "target": "baby spider"},
                    {"at": 1002, "pass": 12, "what": "killed", "target": "giant rat"},
                ]

                evidence = controller._farm_status_evidence(goal, observation, status)

                self.assertEqual({"baby spider": 1, "giant rat": 1}, evidence["kills_by_target"])
                by_target = {
                    item["target"]: item["kills"]
                    for item in controller.learning.combat_summary()["by_target"]
                }
                self.assertEqual(1, by_target["baby spider"])
                self.assertEqual(1, by_target["giant rat"])
            finally:
                controller.storage.close()

    def test_transit_retreat_does_not_quarantine_destination_room(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_room = 575
                broker.room = {"num": 577, "name": "The Twisted Wood"}
                broker.vitals["health"] = {"current": 17, "max": 26}
                broker.vitals["vigor"] = {"value": 93, "scale_max": 200, "rested": True}
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="keeper-transit-retreat",
                        objective="Reach 27 max HP.",
                        success_criteria=[
                            {
                                "id": "max-hp-27",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 27,
                            }
                        ],
                    )
                )["goal"]

                result = controller.turn()

                self.assertTrue(result["switched_to_survival"])
                quarantine = controller.storage.get_runtime("farm_tactic_quarantine_v1", {})
                self.assertNotIn("575", quarantine)
                lesson = controller.storage.goal_lessons(statuses=["deferred"], goal_id=goal["id"])[0]
                self.assertEqual("route_unavailable", lesson["classification"])
                self.assertEqual("tactic", lesson["scope"])
                retry_kinds = {
                    condition.get("kind")
                    for condition in lesson["retry_when"]["conditions"]
                }
                retry_fields = {
                    condition.get("field")
                    for condition in lesson["retry_when"]["conditions"]
                }
                self.assertIn("location_changed", retry_kinds)
                self.assertIn("corpus_changed", retry_kinds)
                self.assertNotIn("max_health", retry_fields)
                self.assertNotIn("equipment_hash", retry_fields)
                self.assertNotIn("vigor", retry_fields)
                self.assertFalse(controller.learning.evaluate_retry(lesson, broker.observe())["met"])
                event = controller.storage.goal_events(
                    goal["id"], kinds=["background_farm.survival_handoff"], limit=10
                )[0]
                self.assertFalse(event["data"]["quarantine"]["quarantined"])
            finally:
                controller.storage.close()

    def test_noncritical_transit_damage_is_route_evidence_not_farm_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_room = 557
                broker.farm_activity = "travelling"
                broker.farm_flee_below = 0.85
                broker.room = {"num": 576, "name": "The King's Way"}
                broker.vitals["health"] = {"current": 25, "max": 30}
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="keeper-transit-damage",
                        objective="Reach 31 max HP.",
                        success_criteria=[
                            {
                                "id": "max-hp-31",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 31,
                            }
                        ],
                    )
                )["goal"]

                result = controller.turn()

                self.assertTrue(result["background_farm_monitoring"])
                self.assertEqual("active", controller.storage.goal(goal["id"])["status"])
                self.assertNotIn(
                    "background_farm.survival_handoff",
                    {
                        event["kind"]
                        for event in controller.storage.goal_events(goal["id"], limit=20)
                    },
                )
                scorecard = controller.learning.farm_room_scorecard()
                self.assertEqual(1, scorecard[0]["route_damage_samples"])
                self.assertEqual(0, scorecard[0]["risk_samples"])
            finally:
                controller.storage.close()

    def test_quarantined_farm_room_is_suppressed_before_broker_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = CombatBroker()
                controller.broker = broker
                controller.storage.set_runtime(
                    "farm_tactic_quarantine_v1",
                    {"535": {"room": 535, "guidance": "choose another room"}},
                )
                goal = controller.storage.submit_goal(goal_payload(request_id="quarantine-preflight"))["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "autopilot",
                        "arguments": {
                            "action": "start",
                            "mode": "farm",
                            "hunt": "giant rat",
                            "assigned_room": 535,
                        },
                        "rationale": "Reuse a failed room.",
                    },
                )

                self.assertTrue(result["safety_suppressed"])
                self.assertIn("quarantined_farm_tactic", {item["kind"] for item in result["blockers"]})
                self.assertFalse(any(name == "autopilot" for name, _ in broker.calls))
            finally:
                controller.storage.close()

    def test_background_farm_death_is_ingested_once_and_switches_to_survival(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.vitals["health"] = {"current": 5, "max": 26}
                broker.last_death = {
                    "at": 12345,
                    "died_in": "Outskirts of Tos",
                    "room_num": 596,
                    "level": 26,
                    "last_health": 5,
                    "hunting": "giant rat",
                    "post_mortem": "postmortem.json",
                }
                controller.broker = broker
                controller.storage.submit_goal(goal_payload(request_id="ingest-keeper-death"))

                controller.turn()
                controller.turn()

                self.assertEqual("survive", broker.farm_mode)
                self.assertEqual(1, controller.learning.combat_summary()["total_deaths"])
                self.assertEqual(1, len(controller.storage.events(kinds=["character.died"])["events"]))
            finally:
                controller.storage.close()

    def test_session_loss_after_fight_is_reconciled_as_death(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = DeathReconciliationBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(goal_payload(request_id="death"))["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {"tool": "fight", "arguments": {"target": "centipede"}, "rationale": "Try one swing."},
                )

                self.assertTrue(result["died"])
                self.assertTrue(result["goal_blocked"])
                self.assertEqual("blocked", controller.storage.goal(goal["id"])["status"])
                deaths = controller.storage.events(kinds=["character.died"])["events"]
                self.assertEqual(1, len(deaths))
                self.assertEqual("The Underworld", deaths[0]["data"]["after"]["room"]["name"])
                self.assertEqual("died", controller.learning.combat_summary()["recent"][-1]["outcome"])
            finally:
                controller.storage.close()

    def test_repeated_degradation_is_deduplicated_and_attached_to_active_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(goal_payload())["goal"]

                controller._degrade("model", ModelError("same failure"))
                controller._degrade("model", ModelError("same failure"))
                events = controller.storage.events(kinds=["dependency.model.unhealthy"])["events"]

                self.assertEqual(1, len(events))
                self.assertEqual(goal["id"], events[0]["goal_id"])

                controller._degrade("model", ModelError("different failure"))
                events = controller.storage.events(kinds=["dependency.model.unhealthy"])["events"]
                self.assertEqual(2, len(events))
            finally:
                controller.storage.close()


if __name__ == "__main__":
    unittest.main()
