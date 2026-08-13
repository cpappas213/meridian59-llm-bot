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
from meridian_bot.campaign import CampaignCoordinator
from meridian_bot.contracts import CRITERION_KINDS
from meridian_bot.controller import (
    RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY,
    RESEARCH_RETRY_STATE_SCHEMA_VERSION,
    BotController,
)
from meridian_bot.criteria import CriteriaEvaluator
from meridian_bot.mcp import TOOLS
from meridian_bot.model import CAMPAIGN_MANAGER_SYSTEM, ModelError, PLANNER_SYSTEM
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


class AuthorizedRevisionModel:
    def plan(self, **kwargs: object) -> dict[str, object]:
        authorization = kwargs.get("revision_authorization")
        assert isinstance(authorization, dict)
        return {
            "decision": "plan",
            "tool": None,
            "arguments": {},
            "rationale": "Fresh action evidence changed the remaining work.",
            "expected_observation": {},
            "proposal": None,
            "execution_plan": with_safe_ending(
                {
                    "summary": "Use the fresh result and finish the bounded goal.",
                    "steps": [
                        {
                            "id": "drop-item",
                            "outcome": "Drop the requested item.",
                            "tool": "act",
                            "verification": "The item is absent from inventory.",
                        }
                    ],
                    "assumptions": [],
                    "revision_reason": "The prior plan action produced fresh evidence.",
                    "revision_authorization_id": authorization["id"],
                },
                100,
            ),
        }


class DecisionSequenceModel:
    def __init__(self, decisions: list[dict[str, object]]) -> None:
        self.decisions = list(decisions)
        self.feedback: list[object] = []

    def plan(self, **kwargs: object) -> dict[str, object]:
        self.feedback.append(kwargs.get("planner_feedback"))
        return copy.deepcopy(self.decisions.pop(0))


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


class PartialTravelBroker(SimulatedBroker):
    def __init__(self) -> None:
        super().__init__()
        self.room = {"num": 1, "name": "Room A"}
        self.tools["travel"] = Tool(
            "travel",
            "Travel to a destination.",
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
        if name == "travel":
            self.calls.append((name, dict(arguments)))
            self.room = {"num": 3, "name": "Room C"}
            return {
                "arrived": False,
                "reason": "gave up after 25 hops",
                "destination": {"num": int(arguments["to"]), "name": "Room Z"},
                "log": [
                    {"from": "Room A", "to": "Room B", "ok": True},
                    {"from": "Room B", "to": "Room C", "ok": True},
                ],
                "now": {"room": dict(self.room)},
            }
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)


class StalePostTravelBroker(SimulatedBroker):
    """Return an authoritative arrival while look briefly retains the source."""

    def __init__(self) -> None:
        super().__init__()
        self.tools["bank"] = Tool(
            "bank",
            "Use a bank in the current room.",
            {
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["agent", "action"],
            },
        )

    def call_tool(self, name: str, arguments: dict[str, object], *, timeout: float = 180, mutation: bool = False) -> object:
        if name == "travel":
            self.calls.append((name, dict(arguments)))
            destination = int(arguments["to"])
            # Deliberately leave self.room unchanged so the immediately
            # following observe reproduces the live client's stale look.
            return {
                "arrived": True,
                "destination": {
                    "num": destination,
                    "name": "First Royal Bank of Tos",
                },
                "now": {
                    "room": {
                        "num": destination,
                        "name": "First Royal Bank of Tos",
                    }
                },
            }
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)


class StalePostCreateFoodBroker(SimulatedBroker):
    def __init__(self) -> None:
        super().__init__()
        self.inventory_items = [{"id": 7, "name": "apple", "amount": 1}]
        self.tools["cast"] = Tool(
            "cast",
            "Cast a spell and optionally observe created inventory.",
            {
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "spell": {"type": "string"},
                    "observe_created": {"type": "boolean"},
                },
                "required": ["agent", "spell"],
            },
        )

    def call_tool(self, name: str, arguments: dict[str, object], *, timeout: float = 180, mutation: bool = False) -> object:
        if name == "cast":
            self.calls.append((name, dict(arguments)))
            # Return the broker's authoritative delta while deliberately
            # retaining a stale inventory cache for the next observation.
            return {
                "cast": True,
                "spell": "create food",
                "mana_spent": 10,
                "created": [{"name": "apple", "amount": 1}],
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


class SilentTransitionBroker(PositionRefreshBroker):
    SILENT_REASON = (
        "sent go and the server answered nothing at all — no room change and no refusal, "
        "which is not a door problem but a lost packet or a reply that did not arrive inside 4s"
    )

    def __init__(self, *, recover: bool) -> None:
        super().__init__()
        self.recover = recover
        self.travel_attempts = 0

    def call_tool(self, name: str, arguments: dict[str, object], *, timeout: float = 180, mutation: bool = False) -> object:
        if name == "travel":
            self.calls.append((name, dict(arguments)))
            self.travel_attempts += 1
            if self.travel_attempts == 1 or not self.recover:
                return {
                    "arrived": False,
                    "reason": self.SILENT_REASON,
                    "now": {"room": dict(self.room)},
                }
            self.room = {
                "num": int(arguments["to"]),
                "name": "First Royal Bank of Tos",
            }
            return {"arrived": True, "room": dict(self.room)}
        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)


class RazaExitBroker(SimulatedBroker):
    def __init__(self) -> None:
        super().__init__()
        self.room = {"num": 1016, "name": "Mausoleum"}
        self.vitals = {
            "health": {"value": 25, "max": 25},
            "mana": {"value": 18, "max": 18},
        }
        self.tools["leave_raza"] = Tool(
            "leave_raza",
            "Leave the one-way Raza tutorial zone.",
            {
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "then_travel_to": {"type": ["string", "number"]},
                },
                "required": ["agent"],
            },
        )

    def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        timeout: float = 180,
        mutation: bool = False,
    ) -> object:
        if name == "leave_raza":
            self.calls.append((name, dict(arguments)))
            destination = int(arguments.get("then_travel_to") or 52)
            self.room = {"num": destination, "name": "Tos Inn"}
            return {
                "left": True,
                "now": {"room": dict(self.room)},
                "note": "one-way - you cannot walk back into Raza",
            }
        return super().call_tool(
            name, arguments, timeout=timeout, mutation=mutation
        )


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
        self.farm_use_safe_spots = True
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
                    "useSafeSpots": self.farm_use_safe_spots,
                },
            }
        if name == "autopilot" and arguments.get("action") == "stop":
            self.calls.append((name, dict(arguments)))
            if self.soft_stop_inert and arguments.get("hard") is not True:
                self.farm_inert = {
                    "inert": True,
                    "why": "asked to stop",
                }
                return {"running": True, "inert": dict(self.farm_inert)}
            self.farm_running = False
            self.farm_inert = None
            return {"running": False, "stopped": True}
        if name == "autopilot" and arguments.get("action") == "start":
            self.calls.append((name, dict(arguments)))
            self.farm_running = True
            self.farm_inert = None
            self.farm_mode = str(arguments.get("mode") or self.farm_mode)
            self.farm_room = int(arguments.get("assigned_room") or self.farm_room)
            self.farm_hunt = str(arguments.get("hunt") or self.farm_hunt)
            self.farm_use_safe_spots = bool(
                arguments.get("use_safe_spots", self.farm_use_safe_spots)
            )
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
        self.assertIn("corrective constraints, not generic obstacles", PLANNER_SYSTEM)
        self.assertIn("shop only inspects a merchant's stock", PLANNER_SYSTEM)
        self.assertIn("Create Weapon directly supplies", PLANNER_SYSTEM)
        self.assertIn("read-only catalogue or status lookup is evidence, not progress", PLANNER_SYSTEM)
        self.assertIn(
            "negative evidence narrows the available tactics",
            CAMPAIGN_MANAGER_SYSTEM,
        )

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

    def test_higher_priority_goal_preempts_only_after_safe_phase_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                simulator = SimulatedBroker()
                simulator.room = {"num": 200, "name": "Unsafe test room"}
                controller.broker = simulator
                controller.model = FixedModel()  # type: ignore[assignment]
                progression = controller.storage.submit_goal(
                    goal_payload(
                        request_id="safe-boundary-preemption-active",
                        title="Raise maximum health",
                        priority=40,
                    )
                )["goal"]
                run = controller.storage.ensure_campaign_run(progression)
                phase = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "general",
                        "objective": "Reach the next bounded health milestone.",
                        "success_criteria": [
                            {
                                "id": "bounded-milestone",
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
                controller._store_execution_plan(
                    progression,
                    with_safe_ending(
                        {
                            "summary": "Finish the bounded phase safely.",
                            "steps": [],
                        },
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(progression),
                    revision=False,
                )
                skill_goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="safe-boundary-preemption-skill",
                        title="Buy Mace Fighting Skill",
                        objective="Learn the Mace Fighting skill.",
                        priority=50,
                    )
                )["goal"]

                safe_return = controller.turn()

                self.assertEqual("travel", safe_return["action"])
                self.assertEqual(100, simulator.room["num"])
                self.assertEqual(
                    progression["id"], controller.storage.active_goal()["id"]
                )
                self.assertEqual(
                    phase["id"],
                    controller.storage.active_campaign_phase(run["id"])["id"],
                )

                boundary = controller.turn()

                self.assertTrue(boundary["campaign_phase_completed"])
                self.assertTrue(boundary["goal_preempted"])
                self.assertEqual(
                    skill_goal["id"], controller.storage.active_goal()["id"]
                )
                self.assertEqual(
                    "queued", controller.storage.goal(progression["id"])["status"]
                )
                self.assertEqual(
                    run["id"],
                    controller.storage.campaign_run(progression["id"])["id"],
                )
            finally:
                controller.storage.close()

    def test_safe_boundary_preemption_waits_until_keeper_releases_control(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                broker = BackgroundFarmBroker()
                broker.room = {"num": 100, "name": "Safe staging"}
                original_call = broker.call_tool

                def refuse_stop(
                    name: str,
                    arguments: dict[str, object],
                    *,
                    timeout: float = 180,
                    mutation: bool = False,
                ) -> object:
                    if name == "autopilot" and arguments.get("action") == "stop":
                        broker.calls.append((name, dict(arguments)))
                        return {"running": True, "mode": "farm"}
                    return original_call(
                        name, arguments, timeout=timeout, mutation=mutation
                    )

                broker.call_tool = refuse_stop  # type: ignore[method-assign]
                controller.broker = broker
                progression = controller.storage.submit_goal(
                    goal_payload(
                        request_id="keeper-release-preemption-active",
                        priority=40,
                    )
                )["goal"]
                run = controller.storage.ensure_campaign_run(progression)
                phase = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "farm",
                        "objective": "Finish one bounded farm milestone.",
                        "success_criteria": [],
                    },
                    mode="start",
                )
                terminal_phase = controller.storage.transition_campaign_phase(
                    phase["id"], "succeeded", reason="test boundary"
                )
                queued = controller.storage.submit_goal(
                    goal_payload(
                        request_id="keeper-release-preemption-higher",
                        title="Buy Mace Fighting Skill",
                        priority=50,
                    )
                )["goal"]

                result = controller._preempt_at_safe_campaign_boundary(
                    progression,
                    broker.observe(),
                    terminal_phase,
                )

                self.assertTrue(result["goal_preemption_pending"])
                self.assertEqual(queued["id"], result["queued_goal"]["id"])
                self.assertEqual(
                    progression["id"], controller.storage.active_goal()["id"]
                )
                self.assertEqual("queued", controller.storage.goal(queued["id"])["status"])
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

                observational = with_safe_ending(
                    {
                        "summary": "Confirm position, then drop the item.",
                        "steps": [
                            {
                                "id": "confirm-position",
                                "outcome": "Confirm MANIAC's exact position before movement.",
                                "tool": "act",
                                "verification": "A fresh look observation reports the current room.",
                            }
                        ],
                    },
                    100,
                )
                with self.assertRaisesRegex(ModelError, "observation-only outcome"):
                    controller._store_execution_plan(
                        goal,
                        observational,
                        grounding=controller.knowledge.validate_goal(goal),
                        revision=True,
                    )

                # Plans persisted by an older controller receive the same
                # validation when loaded, so deployment repairs the live stall.
                values = controller.storage.get_runtime(
                    "goal_execution_plans_v1", {}
                )
                legacy = copy.deepcopy(accepted)
                legacy["steps"][0]["outcome"] = (
                    "Confirm MANIAC's exact position before movement."
                )
                values[goal["id"]] = legacy
                controller.storage.set_runtime("goal_execution_plans_v1", values)
                self.assertIsNone(controller._execution_plan(goal))
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

    def test_execution_plan_enforces_commerce_semantics_and_funding_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                broker = SimulatedBroker()
                for name in ("shop", "sell", "sell_all", "merchants"):
                    broker.tools[name] = Tool(
                        name,
                        f"Test {name} capability.",
                        {"type": "object", "properties": {}},
                    )
                controller.broker = broker
                controller.last_observation = broker.observe()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="commerce-plan-semantics")
                )["goal"]
                grounding = controller.knowledge.validate_goal(goal)

                mislabeled_sale = with_safe_ending(
                    {
                        "summary": "Sell loot and withdraw safely.",
                        "steps": [
                            {
                                "id": "sell-mushrooms",
                                "outcome": "Sell mushrooms to the merchant.",
                                "tool": "shop",
                                "verification": "The merchant bought the loot and shillings increased.",
                            }
                        ],
                    },
                    100,
                )
                with self.assertRaisesRegex(ModelError, "cannot produce its stated outcome"):
                    controller._store_execution_plan(
                        goal,
                        mislabeled_sale,
                        grounding=grounding,
                        revision=False,
                    )

                unfunded_purchase = with_safe_ending(
                    {
                        "summary": "Buy a mace and withdraw safely.",
                        "steps": [
                            {
                                "id": "buy-mace",
                                "outcome": "Purchase a mace from the merchant.",
                                "tool": "shop",
                                "verification": "Inventory contains the purchased mace.",
                            }
                        ],
                    },
                    100,
                )
                with self.assertRaisesRegex(ModelError, "zero carried shillings"):
                    controller._store_execution_plan(
                        goal,
                        unfunded_purchase,
                        grounding=grounding,
                        revision=False,
                    )

                funded_purchase = copy.deepcopy(unfunded_purchase)
                funded_purchase["steps"].insert(
                    0,
                    {
                        "id": "fund-purchase",
                        "outcome": "Sell ordinary mushrooms to raise purchase funds.",
                        "tool": "sell",
                        "verification": "The sale transfers mushrooms and increases shillings.",
                    },
                )
                stored = controller._store_execution_plan(
                    goal,
                    funded_purchase,
                    grounding=grounding,
                    revision=False,
                )
                self.assertEqual("sell", stored["steps"][0]["tool"])
                self.assertEqual("shop", stored["steps"][1]["tool"])
            finally:
                controller.storage.close()

    def test_plan_funding_check_defers_until_inventory_is_observed(self) -> None:
        purchase = [
            {
                "id": "buy-food",
                "tool": "shop",
                "outcome": "Purchase edible food.",
                "verification": "Inventory contains the purchased food.",
            }
        ]

        self.assertIsNone(BotController._plan_funding_error(purchase, {}))
        self.assertIsNone(
            BotController._plan_funding_error(
                purchase,
                {"inventory": {"items": [], "carry": {"known": False}}},
            )
        )
        self.assertIn(
            "zero carried shillings",
            BotController._plan_funding_error(
                purchase,
                {"inventory": {"items": [], "carry": {"known": True}}},
            )
            or "",
        )

    def test_prepare_combat_sale_guards_preserve_loadout_and_require_quote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="prepare-combat-sale-guards")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                phase = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "prepare_combat",
                        "objective": "Acquire a working weapon.",
                        "success_criteria": [],
                    },
                    mode="start",
                )
                observation = {
                    "look": {"room": {"num": 52, "name": "Bhrama & Falcon"}},
                    "inventory": {
                        "items": [
                            {"id": 77, "name": "mace", "in_use": True},
                            {"id": 78, "name": "mace"},
                            {"id": 88, "name": "mushroom"},
                        ]
                    },
                    "equipment": {
                        "equipped": [{"id": 77, "name": "mace"}],
                        "wielding": ["mace"],
                    },
                }

                with self.assertRaisesRegex(ModelError, "preserve goal-relevant equipment"):
                    controller._guard_prepare_combat_sale(
                        goal,
                        phase,
                        "sell_all",
                        {"merchant": 10, "ignore_loadout": True},
                        observation,
                    )
                with self.assertRaisesRegex(ModelError, "preserve all candidate weapons"):
                    controller._guard_prepare_combat_sale(
                        goal,
                        phase,
                        "sell_all",
                        {"merchant": 10, "max_weapons": 1},
                        observation,
                    )

                with self.assertRaisesRegex(
                    ModelError,
                    r"equipped item id\(s\) 77.*unequipped duplicate item id\(s\) 78",
                ):
                    controller._guard_prepare_combat_sale(
                        goal,
                        phase,
                        "sell",
                        {"to": 10, "items": [77], "confirm": False},
                        observation,
                    )
                with self.assertRaisesRegex(ModelError, "equipped item"):
                    controller._guard_prepare_combat_sale(
                        goal,
                        phase,
                        "sell",
                        {"to": 10, "items": ["mace"], "confirm": False},
                        observation,
                    )

                sale = {"to": 10, "items": [88], "confirm": True}
                with self.assertRaisesRegex(ModelError, "unquoted targeted sale"):
                    controller._guard_prepare_combat_sale(
                        goal,
                        phase,
                        "sell",
                        sale,
                        observation,
                    )
                quote = {**sale, "confirm": False}
                controller._record_prepare_combat_sell_quote(
                    goal,
                    phase,
                    quote,
                    observation,
                    {"sold": False, "offered_price": 12},
                )
                controller._guard_prepare_combat_sale(
                    goal,
                    phase,
                    "sell",
                    sale,
                    observation,
                )
            finally:
                controller.storage.close()

    def test_liquidation_sale_guard_preserves_phase_keep_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="liquidation-sale-guards")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                phase = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "liquidate_inventory",
                        "objective": "Sell ordinary excess loot.",
                        "success_criteria": [],
                        "context": {"keep_candidates": ["mace", "sapphire"]},
                    },
                    mode="start",
                )
                observation = {
                    "look": {"room": {"num": 106, "name": "Brownestone Inn"}},
                    "inventory": {
                        "items": [
                            {"id": 77, "name": "mace"},
                            {"id": 88, "name": "mushroom"},
                        ]
                    },
                }

                with self.assertRaisesRegex(ModelError, "retained keep_candidate.*mace"):
                    controller._guard_prepare_combat_sale(
                        goal,
                        phase,
                        "sell",
                        {"to": 736, "items": [77], "confirm": False},
                        observation,
                    )

                bulk = {"merchant": 736, "keep": ["reagent"]}
                controller._guard_prepare_combat_sale(
                    goal, phase, "sell_all", bulk, observation
                )
                self.assertFalse(bulk["ignore_loadout"])
                self.assertEqual(["mace", "reagent", "sapphire"], bulk["keep"])

                plan_error = controller._protected_phase_sale_step_error(
                    phase,
                    {
                        "tool": "sell",
                        "outcome": "Get a read-only quote for the mace.",
                    },
                )
                retained_error = controller._protected_phase_sale_step_error(
                    phase,
                    {
                        "tool": "sell",
                        "outcome": "Sell mushrooms while keeping the mace and sapphire.",
                    },
                )
                self.assertIn("keep_candidate 'mace'", plan_error or "")
                self.assertIsNone(retained_error)
            finally:
                controller.storage.close()

    def test_train_ability_rejects_map_as_creature_spawn_search(self) -> None:
        phase = {
            "kind": "train_ability",
            "context": {"target": "ant", "room": 6},
        }
        error = BotController._map_step_error(
            phase,
            {
                "tool": "map",
                "outcome": "Use map to find a reachable ant room.",
                "verification": "Map lists a candidate room with ants.",
            },
        )
        valid = BotController._map_step_error(
            phase,
            {
                "tool": "map",
                "outcome": "Verify the route to exact room 6.",
                "verification": "Map returns a non-empty route to room 6.",
            },
        )

        self.assertIn("map.search only matches room names", error or "")
        self.assertIsNone(valid)
        with self.assertRaisesRegex(ModelError, "cannot establish creature occupancy"):
            BotController._guard_map_semantics(
                phase,
                "map",
                {"search": "ants"},
            )
        BotController._guard_map_semantics(phase, "map", {"to": 6})

    def test_combat_training_phase_is_keeper_owned_until_ability_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                broker = BackgroundFarmBroker()
                broker.farm_running = False
                broker.tools["fight"] = Tool(
                    "fight",
                    "One foreground combat swing.",
                    {
                        "type": "object",
                        "properties": {"agent": {}, "target": {}},
                        "required": ["agent", "target"],
                    },
                )
                for tool_name in ("rest_up", "equip_best"):
                    broker.tools[tool_name] = Tool(
                        tool_name,
                        f"Test {tool_name} tool.",
                        {"type": "object", "properties": {"agent": {}}},
                    )
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="keeper-owned-combat-training",
                        objective="Raise mace fighting to 20.",
                        success_criteria=[
                            {
                                "id": "mace-20",
                                "kind": "numeric_threshold",
                                "metric": "ability.skill.mace fighting",
                                "operator": ">=",
                                "value": 20,
                            }
                        ],
                    )
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                phase = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "train_ability",
                        "objective": "Train mace fighting against ants.",
                        "success_criteria": list(goal["success_criteria"]),
                        "abandon_predicates": [],
                        "budget": {"max_actions": 40, "max_minutes": 90},
                        "context": {
                            "training_method": "combat",
                            "prey": "ant",
                            "room": 6,
                            "use_safe_spots": False,
                            "flee_below": 0.70,
                            "fight_above_vigor": 100,
                        },
                    },
                    mode="start",
                )
                observation = broker.observe()
                observation["abilities"] = {
                    "skills": [{"name": "Mace Fighting", "ability": 10}]
                }
                controller.last_observation = observation
                completion = controller.criteria.evaluate(goal, observation)

                self.assertTrue(
                    controller._keeper_combat_work_remains(goal, completion)
                )
                self.assertEqual(
                    {"assigned_room": 6, "hunt": "ant"},
                    {
                        key: controller._effective_farm_intent(goal).get(key)
                        for key in ("assigned_room", "hunt")
                    },
                )
                allowed = {
                    item["name"] for item in controller._planner_tools(phase)
                }
                self.assertIn("autopilot", allowed)
                self.assertNotIn("fight", allowed)
                with self.assertRaisesRegex(
                    ModelError, "foreground fight is unsafe for combat training"
                ):
                    controller._execute(
                        goal,
                        observation,
                        {
                            "decision": "act",
                            "tool": "fight",
                            "arguments": {"target": "ant"},
                        },
                    )

                plan = with_safe_ending(
                    controller._structured_farm_controller_plan(goal), 100
                )
                stored = controller._store_execution_plan(
                    goal,
                    plan,
                    grounding={
                        "valid": True,
                        "corpus": {"corpus_version": "test"},
                    },
                    revision=False,
                )
                launch = controller._execute(
                    goal,
                    observation,
                    {
                        "decision": "act",
                        "tool": "autopilot",
                        "arguments": {
                            "action": "start",
                            "mode": "farm",
                            "hunt": "ant",
                            "assigned_room": 6,
                            "use_safe_spots": False,
                        },
                        "plan_step_id": "launch-goal-keeper",
                    },
                )
                self.assertEqual("autopilot", launch["action"])
                owner = controller.storage.get_runtime(
                    "background_farm_owner_v1", {}
                )
                self.assertEqual(phase["id"], owner["phase_id"])
                monitoring = controller._manage_background_farm(
                    goal, observation, completion
                )
                self.assertTrue(monitoring["background_farm_monitoring"])

                finished_observation = copy.deepcopy(observation)
                finished_observation["abilities"]["skills"][0]["ability"] = 20
                completed = controller._reconcile_existing_campaign_phase(
                    goal, finished_observation
                )
                self.assertTrue(completed["campaign_phase_completed"])
                self.assertTrue(completed["keeper_released"])
                self.assertFalse(broker.farm_running)
                self.assertEqual(phase["id"], stored["phase_id"])
            finally:
                controller.storage.close()

    def test_combat_training_recipe_is_validated_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                incomplete = {
                    "kind": "train_ability",
                    "objective": "Train mace fighting in combat.",
                    "success_criteria": [
                        {
                            "kind": "numeric_threshold",
                            "metric": "ability.skill.mace fighting",
                            "operator": ">=",
                            "value": 20,
                        }
                    ],
                    "context": {
                        "training_method": "combat",
                        "room": 6,
                        "use_safe_spots": False,
                    },
                }

                blocker = controller._campaign_phase_grounding_blocker(incomplete)

                self.assertEqual(
                    "invalid_combat_training_phase_context", blocker["kind"]
                )
                self.assertIn("context.target", blocker["guidance"])
                self.assertIn(
                    'training_method="combat"', CAMPAIGN_MANAGER_SYSTEM
                )
                self.assertIn("one-swing foreground fight", CAMPAIGN_MANAGER_SYSTEM)

                teacher = {
                    "kind": "train_ability",
                    "objective": "Learn mace fighting from a teacher.",
                    "success_criteria": [
                        {
                            "kind": "numeric_threshold",
                            "metric": "ability.skill.mace fighting",
                            "operator": ">=",
                            "value": 1,
                        }
                    ],
                    "context": {
                        "training_method": "teacher",
                        "room": 154,
                        "target": "mace fighting",
                        "merchant_class": "CorNothSergeant",
                    },
                }
                self.assertEqual(
                    {}, controller._campaign_phase_farm_intent(teacher)
                )
                self.assertIsNone(
                    controller._campaign_phase_grounding_blocker(teacher)
                )
            finally:
                controller.storage.close()

    def test_completed_combat_training_keeps_survival_owner_until_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                broker = BackgroundFarmBroker()
                for tool_name in ("rest_up", "equip_best"):
                    broker.tools[tool_name] = Tool(
                        tool_name,
                        f"Test {tool_name} tool.",
                        {"type": "object", "properties": {"agent": {}}},
                    )
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="safe-combat-training-handoff",
                        objective="Raise mace fighting to 100.",
                        success_criteria=[
                            {
                                "id": "mace-100",
                                "kind": "numeric_threshold",
                                "metric": "ability.skill.mace fighting",
                                "operator": ">=",
                                "value": 100,
                            }
                        ],
                    )
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                phase = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "train_ability",
                        "objective": "Reach a bounded mace fighting milestone.",
                        "success_criteria": [
                            {
                                "id": "mace-20",
                                "kind": "numeric_threshold",
                                "metric": "ability.skill.mace fighting",
                                "operator": ">=",
                                "value": 20,
                            }
                        ],
                        "abandon_predicates": [],
                        "budget": {"max_actions": 40, "max_minutes": 90},
                        "context": {
                            "training_method": "combat",
                            "prey": "ant",
                            "room": 6,
                            "use_safe_spots": False,
                            "flee_below": 0.70,
                            "fight_above_vigor": 100,
                        },
                    },
                    mode="start",
                )
                planning_observation = broker.observe()
                planning_observation["abilities"] = {
                    "skills": [{"name": "Mace Fighting", "ability": 10}]
                }
                controller.last_observation = planning_observation
                controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        controller._structured_farm_controller_plan(goal), 100
                    ),
                    grounding={
                        "valid": True,
                        "corpus": {"corpus_version": "test"},
                    },
                    revision=False,
                )

                broker.room = {"num": 6, "name": "Ant field"}
                broker.farm_room = 6
                broker.farm_hunt = "ant"
                broker.farm_use_safe_spots = False
                hazardous = broker.observe()
                hazardous["abilities"] = {
                    "skills": [{"name": "Mace Fighting", "ability": 20}]
                }
                public_completion = controller.criteria.evaluate(goal, hazardous)

                self.assertIsNone(
                    controller._reconcile_existing_campaign_phase(goal, hazardous)
                )
                self.assertIsNotNone(
                    controller._phase_completion_checkpoint(phase)
                )
                handed_off = controller._manage_background_farm(
                    goal,
                    hazardous,
                    public_completion,
                    force_stop_reason="phase outcome verified",
                )
                self.assertTrue(handed_off["background_safe_ending_handoff"])
                self.assertEqual("survive", broker.farm_mode)
                self.assertIsNotNone(
                    controller.storage.active_campaign_phase(run["id"])
                )
                self.assertEqual(
                    {},
                    controller.storage.get_runtime("farm_tactic_quarantine_v1", {}),
                )

                still_unsafe = controller._manage_background_farm(
                    goal,
                    hazardous,
                    public_completion,
                    force_stop_reason="phase outcome verified",
                )
                self.assertTrue(still_unsafe["background_survival_monitoring"])
                self.assertTrue(still_unsafe["safe_ending_pending"])

                broker.room = {"num": 100, "name": "Training Hall"}
                safe = broker.observe()
                safe["abilities"] = hazardous["abilities"]
                stopped = controller._manage_background_farm(
                    goal,
                    safe,
                    public_completion,
                    force_stop_reason="phase outcome verified",
                )
                self.assertTrue(stopped["background_keeper_stopping"])
                completed = controller._reconcile_existing_campaign_phase(goal, safe)
                self.assertTrue(completed["campaign_phase_completed"])
                self.assertTrue(completed["keeper_released"])
            finally:
                controller.storage.close()

    def test_map_route_lookup_without_a_route_is_no_progress(self) -> None:
        reason = BotController._no_progress_reason(
            {
                "destination": {"num": 6, "name": "The Deep Dark Woods of Marion"},
                "route": None,
            },
            {"look": {"room": {"num": 103, "name": "The Bhrama & Falcon"}}},
            tool="map",
            arguments={"to": 6},
        )

        self.assertIn("found no route from current room 103", reason or "")

    def test_direct_prepare_combat_capability_highlights_create_weapon(self) -> None:
        context = BotController._direct_phase_capabilities(
            {"kind": "prepare_combat"},
            {
                "spells": {
                    "spells": [
                        {
                            "name": "Create Weapon",
                            "mana": 15,
                            "reagents": [],
                            "castable": True,
                        },
                        {"name": "Blink", "castable": True},
                    ]
                }
            },
        )

        self.assertIsNotNone(context)
        self.assertEqual("cast", context["preferred_tool"])
        self.assertEqual("Create Weapon", context["capabilities"][0]["name"])

    def test_direct_prepare_combat_capabilities_ground_create_food_semantics(self) -> None:
        phase = {
            "kind": "prepare_combat",
            "objective": "Equip a weapon and create consumable food.",
            "context": {},
        }
        observation = {
            "spells": {
                "spells": [
                    {
                        "name": "Create Weapon",
                        "mana": 15,
                        "reagents": [],
                        "castable": True,
                    },
                    {
                        "name": "Create Food",
                        "mana": 10,
                        "reagents": [],
                        "castable": True,
                    },
                ]
            }
        }

        context = BotController._direct_phase_capabilities(phase, observation)

        self.assertIsNotNone(context)
        capabilities = {
            item["name"]: item for item in (context or {}).get("capabilities", [])
        }
        self.assertFalse(capabilities["Create Weapon"]["npc_transferable"])
        self.assertFalse(capabilities["Create Weapon"]["funding_eligible"])
        self.assertIn("no reagent prerequisite", capabilities["Create Food"]["server_semantics"])

        blocked_food_observation = copy.deepcopy(observation)
        blocked_food = blocked_food_observation["spells"]["spells"][1]
        blocked_food.update(
            {
                "castable": False,
                "reagents": ["2 x ElderBerry", "2 x Herbs"],
                "blocked_by": [
                    "needs 2 x ElderBerry, carrying 0",
                    "needs 2 x Herbs, carrying 0",
                ],
            }
        )
        blocked_context = BotController._direct_phase_capabilities(
            phase, blocked_food_observation
        )
        blocked_capabilities = {
            item["name"]: item
            for item in (blocked_context or {}).get("capabilities", [])
        }
        self.assertFalse(blocked_capabilities["Create Food"]["castable"])
        self.assertEqual(
            ["2 x ElderBerry", "2 x Herbs"],
            blocked_capabilities["Create Food"]["reagents"],
        )
        self.assertIn(
            "Acquire exactly its listed reagents",
            blocked_capabilities["Create Food"]["server_semantics"],
        )

        stocked_food_observation = copy.deepcopy(blocked_food_observation)
        stocked_food_observation["inventory"] = {
            "items": [
                {"name": "elderberry", "amount": 2},
                {"name": "herb", "amount": 2},
            ]
        }
        stocked_context = BotController._direct_phase_capabilities(
            phase, stocked_food_observation
        )
        stocked_food = next(
            item
            for item in (stocked_context or {}).get("capabilities", [])
            if item.get("name") == "Create Food"
        )
        self.assertTrue(stocked_food["castable"])
        self.assertNotIn("blocked_by", stocked_food)
        self.assertTrue(stocked_food["availability_reconciled_from_inventory"])
        self.assertIsNone(
            BotController._direct_capability_plan_error(
                phase,
                [
                    {
                        "id": "buy-live-reagents",
                        "tool": "shop",
                        "outcome": "Buy 2 ElderBerry and 2 Herbs.",
                        "verification": "Inventory contains the live listed reagents.",
                    },
                    {
                        "id": "cast-after-reagents",
                        "tool": "cast",
                        "outcome": "Cast Create Food.",
                        "verification": "Inventory contains food.",
                    },
                ],
                blocked_food_observation,
                ["Create Food needs 2 ElderBerry and 2 Herbs."],
            )
        )

        detour = [
            {
                "id": "sell-mace",
                "tool": "sell",
                "outcome": "Sell mace id 2 to raise funds.",
                "verification": "Shillings increase.",
            },
            {
                "id": "buy-herbs",
                "tool": "shop",
                "outcome": "Buy ElderBerry and Herbs as Create Food reagents.",
                "verification": "Inventory contains the reagents.",
            },
            {
                "id": "create-food",
                "tool": "cast",
                "outcome": "Cast Create Food.",
                "verification": "Inventory contains food.",
            },
        ]
        self.assertIn(
            "castable with no reagents",
            BotController._direct_capability_plan_error(
                phase, detour, observation, []
            )
            or "",
        )
        self.assertIsNone(
            BotController._direct_capability_plan_error(
                phase,
                [detour[-1]],
                observation,
                [],
            )
        )
        self.assertIn(
            "invents Create Food reagents",
            BotController._direct_capability_plan_error(
                phase,
                [detour[-1]],
                observation,
                ["Create Food requires ElderBerry and Herbs."],
            )
            or "",
        )

        created_weapon_sale = [
            {
                "id": "create-weapon",
                "tool": "cast",
                "outcome": "Cast Create Weapon.",
                "verification": "A created weapon appears.",
            },
            {
                "id": "sell-created",
                "tool": "sell",
                "outcome": "Sell the newly created mace with the exact id from create-weapon.",
                "verification": "A merchant buys that mace and shillings increase.",
            },
        ]
        weapon_only_phase = {
            "kind": "prepare_combat",
            "objective": "Equip a reliable weapon.",
            "context": {},
        }
        self.assertIn(
            "IA_MADE",
            BotController._direct_capability_plan_error(
                weapon_only_phase,
                created_weapon_sale,
                observation,
                [],
            )
            or "",
        )
        self.assertIn(
            "assumes a Create Weapon product is sellable",
            BotController._direct_capability_plan_error(
                weapon_only_phase,
                created_weapon_sale[:1],
                observation,
                [
                    "At least one merchant will accept the newly created mace as sellable funding."
                ],
            )
            or "",
        )
        self.assertIn("Create Weapon products are marked IA_MADE", PLANNER_SYSTEM)

    def test_create_food_context_quantifies_remaining_phase_requirement(self) -> None:
        phase = {
            "kind": "prepare_combat",
            "objective": "Create food supplies.",
            "success_criteria": [
                {
                    "id": "food_stock",
                    "kind": "inventory_contains",
                    "item": "food",
                    "count": 4,
                }
            ],
            "context": {},
        }
        observation = {
            "inventory": {"items": [{"name": "apple", "amount": 1}]},
            "abilities": {
                "spells": [{"name": "Create Food", "ability": 25}]
            },
            "spells": {
                "spells": [
                    {
                        "name": "Create Food",
                        "mana": 10,
                        "reagents": ["2 x ElderBerry", "2 x Herbs"],
                        "castable": False,
                        "blocked_by": ["needs reagents"],
                    }
                ]
            },
        }

        context = BotController._direct_phase_capabilities(phase, observation)
        capability = (context or {})["capabilities"][0]

        self.assertEqual(["apple"], capability["production"]["possible_products_at_current_ability"])
        self.assertEqual("vigor, not health", capability["production"]["restores"])
        self.assertEqual(
            {
                "criterion_id": "food_stock",
                "required_total": 4,
                "currently_carried": 1,
                "remaining": 3,
                "casts_required": 3,
                "reagents_required_for_remaining_casts": [
                    "6 x ElderBerry",
                    "6 x Herbs",
                ],
                "verification_category": "food",
            },
            capability["phase_requirement"],
        )

    def test_phase_inventory_plan_requires_exact_remaining_quantity(self) -> None:
        phase = {
            "kind": "prepare_combat",
            "success_criteria": [
                {
                    "id": "food_stock",
                    "kind": "inventory_contains",
                    "item": "food",
                    "count": 4,
                }
            ],
        }
        observation = {
            "inventory": {"items": [{"name": "apple", "amount": 1}]}
        }
        insufficient = [
            {
                "id": "one-cast",
                "tool": "cast",
                "outcome": "Cast Create Food once.",
                "verification": "Inventory contains an apple.",
            }
        ]
        quantified = [
            {
                "id": "finish-food",
                "tool": "cast",
                "outcome": "Cast Create Food as needed.",
                "verification": "Inventory contains 4 total edible food items.",
            }
        ]
        separate_casts = [
            {
                "id": f"cast-{index}",
                "tool": "cast",
                "outcome": "Cast Create Food once.",
                "verification": "Another apple appears.",
            }
            for index in range(3)
        ]

        self.assertIn(
            "does not cover active phase criterion",
            BotController._phase_inventory_plan_error(
                phase, insufficient, observation
            )
            or "",
        )
        self.assertIsNone(
            BotController._phase_inventory_plan_error(
                phase, quantified, observation
            )
        )
        self.assertIsNone(
            BotController._phase_inventory_plan_error(
                phase, separate_casts, observation
            )
        )

    def test_food_plan_accounts_for_removals_and_per_cast_reagents(self) -> None:
        phase = {
            "kind": "prepare_combat",
            "success_criteria": [
                {
                    "id": "food_stock",
                    "kind": "inventory_contains",
                    "item": "food",
                    "count": 4,
                }
            ],
        }
        observation = {
            "inventory": {
                "items": [
                    {"id": 10, "name": "apple"},
                    {"id": 20, "name": "elderberry", "amount": 2},
                    {"id": 30, "name": "herb", "amount": 2},
                ]
            },
            "spells": {
                "spells": [
                    {
                        "name": "Create Food",
                        "reagents": ["2 x ElderBerry", "2 x Herbs"],
                    }
                ]
            },
        }
        three_casts = [
            {
                "id": f"cast-{index}",
                "tool": "cast",
                "outcome": "Cast Create Food once.",
                "verification": f"Inventory contains {index + 2} edible food items total.",
            }
            for index in range(3)
        ]
        sell_existing = [
            {
                "id": "sell-apple",
                "tool": "sell",
                "outcome": "Sell apple id 10.",
                "verification": "Apple id 10 is gone and shillings increase.",
            },
            *three_casts,
        ]

        self.assertIn(
            "plans to remove 1",
            BotController._phase_inventory_plan_error(
                phase, sell_existing, observation
            )
            or "",
        )
        self.assertIn(
            "elderberry is 2 per cast",
            BotController._phase_inventory_plan_error(
                phase, three_casts, observation
            )
            or "",
        )

        grounded = [
            {
                "id": "buy-reagents",
                "tool": "shop",
                "outcome": "Buy 4 ElderBerry and 4 Herbs.",
                "verification": (
                    "Inventory contains 6 ElderBerry total and 6 Herbs total."
                ),
            },
            *three_casts,
        ]
        self.assertIsNone(
            BotController._phase_inventory_plan_error(
                phase, grounded, observation
            )
        )

        stocked = copy.deepcopy(observation)
        stocked["inventory"]["items"][1]["amount"] = 6
        stocked["inventory"]["items"][2]["amount"] = 6
        self.assertEqual(
            6,
            CriteriaEvaluator.inventory_count(
                stocked["inventory"]["items"], "Herbs"
            ),
        )
        self.assertIsNone(
            BotController._phase_inventory_plan_error(
                phase, three_casts, stocked
            )
        )

    def test_financial_context_retains_latest_known_bank_balance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="durable-bank-balance-context")
                )["goal"]
                controller.storage.emit_event(
                    "action.succeeded",
                    "Action succeeded: bank",
                    goal_id=goal["id"],
                    data={
                        "tool": "bank",
                        "result": {
                            "account": "jasper-tos-barloque",
                            "action": "withdraw",
                            "balance": 1712,
                            "balance_observed": False,
                        },
                    },
                )
                observation = {
                    "inventory": {
                        "items": [
                            {"id": 1, "name": "shilling", "amount": 32},
                            {"id": 2, "name": "apple"},
                        ]
                    }
                }

                financial = controller._financial_context(observation)

                self.assertEqual(
                    1712,
                    financial["bank_accounts"][0]["last_known_balance"],
                )
                self.assertIn(
                    "preserving that required inventory",
                    financial["banking_policy"]["funding_guidance"],
                )

                controller.storage.emit_event(
                    "action.succeeded",
                    "Action succeeded: bank",
                    goal_id=goal["id"],
                    data={
                        "tool": "bank",
                        "result": {
                            "account": "jasper-tos-barloque",
                            "action": "withdraw",
                            "balance": 1600,
                            "balance_observed": True,
                        },
                    },
                )
                refreshed = controller._financial_context(observation)
                self.assertEqual(
                    1600,
                    refreshed["bank_accounts"][0]["last_known_balance"],
                )
            finally:
                controller.storage.close()

    def test_positive_bank_balance_protects_phase_required_inventory_from_sale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="bank-preserves-phase-inventory")
                )["goal"]
                controller.storage.emit_event(
                    "action.succeeded",
                    "Action succeeded: bank",
                    goal_id=goal["id"],
                    data={
                        "tool": "bank",
                        "result": {
                            "account": "jasper-tos-barloque",
                            "action": "balance",
                            "balance": 1712,
                            "balance_observed": True,
                        },
                    },
                )
                phase = {
                    "kind": "prepare_combat",
                    "success_criteria": [
                        {
                            "id": "food-stock",
                            "kind": "inventory_contains",
                            "item": "food",
                            "count": 4,
                        }
                    ],
                }
                observation = {
                    "inventory": {
                        "items": [{"id": 17375, "name": "apple"}]
                    }
                }
                sell_plan = [
                    {
                        "id": "sell-apple",
                        "tool": "sell",
                        "outcome": "Sell apple id 17375 to raise reagent funds.",
                        "verification": "Apple is removed and shillings increase.",
                    }
                ]

                error = controller._phase_required_sale_plan_error(
                    phase, sell_plan, observation
                )

                self.assertIn("durable bank evidence shows 1712", error or "")
                self.assertIn("travel to bank room 54", error or "")
            finally:
                controller.storage.close()

    def test_shop_plan_affordability_uses_live_catalogue_basket_total(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="live-catalogue-affordability")
                )["goal"]
                controller.storage.emit_event(
                    "action.succeeded",
                    "Action succeeded: shop",
                    goal_id=goal["id"],
                    data={
                        "tool": "shop",
                        "result": {
                            "seller": 711,
                            "items": [
                                {"id": 714, "name": "elderberry", "cost": 28},
                                {"id": 712, "name": "herb", "cost": 14},
                            ],
                        },
                    },
                )
                controller.storage.emit_event(
                    "action.succeeded",
                    "Action succeeded: bank",
                    goal_id=goal["id"],
                    data={
                        "tool": "bank",
                        "result": {
                            "account": "jasper-tos-barloque",
                            "balance": 1712,
                            "balance_observed": True,
                        },
                    },
                )
                observation = {
                    "inventory": {
                        "items": [
                            {"id": 1, "name": "shilling", "amount": 32}
                        ],
                        "carry": {"known": True},
                    }
                }
                purchase = [
                    {
                        "id": "buy-reagents",
                        "tool": "shop",
                        "outcome": "Buy 6 ElderBerry and 6 Herbs from Joguer.",
                        "verification": "Inventory contains the reagents.",
                    }
                ]

                error = controller._shop_plan_affordability_error(
                    purchase, observation
                )

                self.assertIn("grounded basket cost of 252", error or "")
                self.assertIn("only 32 are carried", error or "")
                self.assertIn("6 elderberry @ 28", error or "")
                self.assertIn("bank room 54", error or "")

                funded = [
                    {
                        "id": "reach-bank",
                        "tool": "travel",
                        "outcome": "Travel to bank room 54.",
                        "verification": "Current room is 54.",
                    },
                    {
                        "id": "withdraw",
                        "tool": "bank",
                        "outcome": "Withdraw enough shillings for the basket.",
                        "verification": "Carried shillings cover 252.",
                    },
                    *purchase,
                ]
                self.assertIsNone(
                    controller._shop_plan_affordability_error(
                        funded, observation
                    )
                )
            finally:
                controller.storage.close()

    def test_quantified_shop_step_requires_exact_live_basket_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="quantified-shop-action")
                )["goal"]
                controller.storage.emit_event(
                    "action.succeeded",
                    "Action succeeded: shop",
                    goal_id=goal["id"],
                    data={
                        "tool": "shop",
                        "result": {
                            "seller": 711,
                            "items": [
                                {"id": 714, "name": "elderberry", "cost": 28},
                                {"id": 712, "name": "herb", "cost": 14},
                            ],
                        },
                    },
                )
                step = {
                    "id": "buy-reagents",
                    "tool": "shop",
                    "outcome": "Buy 6 ElderBerry and 6 Herbs from Joguer.",
                    "verification": "Inventory contains the required reagents.",
                }

                underfilled = controller._quantified_shop_action_error(
                    step,
                    {
                        "seller": 711,
                        "buy_ids": [714, 714, 714, 712, 712, 712],
                    },
                )

                self.assertIn("requires exactly 6 elderberry", underfilled or "")
                self.assertIn("3 herb (id 712)", underfilled or "")
                self.assertIn("send the exact repeated live item ids", underfilled or "")
                self.assertIsNone(
                    controller._quantified_shop_action_error(
                        step,
                        {
                            "seller": 711,
                            "buy_ids": [714] * 6 + [712] * 6,
                        },
                    )
                )
            finally:
                controller.storage.close()

    def test_underfilled_quantified_shop_action_is_returned_to_planner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                broker = ShopBroker()
                broker.inventory_items.append(
                    {"id": 2, "name": "shilling", "amount": 284, "can": []}
                )
                controller.broker = broker
                controller.last_observation = broker.observe()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="reject-underfilled-shop-action")
                )["goal"]
                controller.storage.emit_event(
                    "action.succeeded",
                    "Action succeeded: shop",
                    goal_id=goal["id"],
                    data={
                        "tool": "shop",
                        "result": {
                            "seller": 711,
                            "items": [
                                {"id": 714, "name": "elderberry", "cost": 28},
                                {"id": 712, "name": "herb", "cost": 14},
                            ],
                        },
                    },
                )
                controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {
                            "summary": "Buy the exact reagent basket.",
                            "steps": [
                                {
                                    "id": "buy-reagents",
                                    "tool": "shop",
                                    "outcome": "Buy 6 ElderBerry and 6 Herbs from Joguer.",
                                    "verification": "Inventory contains the reagent basket.",
                                }
                            ],
                        },
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )
                controller.model = DecisionSequenceModel(
                    [
                        {
                            "decision": "act",
                            "tool": "shop",
                            "arguments": {
                                "seller": 711,
                                "buy_ids": [714, 714, 714, 712, 712, 712],
                            },
                            "rationale": "Buy only half of the declared basket.",
                            "expected_observation": {},
                            "proposal": None,
                            "plan_step_id": "buy-reagents",
                        }
                    ]
                )  # type: ignore[assignment]

                rejected = controller.turn()

                self.assertTrue(rejected["planner_action_rejected"])
                self.assertTrue(rejected["quantified_shop_basket_mismatch"])
                self.assertFalse(any(name == "shop" for name, _ in broker.calls))
                feedback = controller._planner_feedback(goal)
                self.assertIn("requires exactly 6 elderberry", feedback["message"])
                self.assertIn("Correct the rejected arguments", feedback["failure_context"]["required_response"])
            finally:
                controller.storage.close()

    def test_completed_purchase_prefix_is_not_revalidated_as_future_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                broker = ShopBroker()
                controller.broker = broker
                broker.inventory_items.append(
                    {"id": 2, "name": "shilling", "amount": 284, "can": []}
                )
                controller.last_observation = broker.observe()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="completed-purchase-prefix")
                )["goal"]
                controller.storage.emit_event(
                    "action.succeeded",
                    "Action succeeded: shop",
                    goal_id=goal["id"],
                    data={
                        "tool": "shop",
                        "result": {
                            "seller": 711,
                            "items": [
                                {"id": 714, "name": "elderberry", "cost": 28},
                                {"id": 712, "name": "herb", "cost": 14},
                            ],
                        },
                    },
                )
                controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {
                            "summary": "Buy the reagent basket and finish safely.",
                            "steps": [
                                {
                                    "id": "buy-reagents",
                                    "tool": "shop",
                                    "outcome": "Buy 6 ElderBerry and 6 Herbs from Joguer.",
                                    "verification": "Inventory contains the reagent basket.",
                                }
                            ],
                        },
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )
                controller._record_plan_action(
                    goal,
                    step_id="buy-reagents",
                    tool="shop",
                    arguments={"seller": 711, "buy_ids": [714] * 6 + [712] * 6},
                    result={"bought": [714] * 6 + [712] * 6},
                    status="succeeded",
                )
                broker.inventory_items[-1]["amount"] = 158
                controller.last_observation = broker.observe()

                stored = controller._execution_plan(goal)

                self.assertIsNotNone(stored)
                self.assertEqual("buy-reagents", stored["last_action"]["step_id"])
                self.assertEqual(
                    [],
                    controller.storage.events(kinds=["planner.plan.invalidated"])[
                        "events"
                    ],
                )
            finally:
                controller.storage.close()

    def test_duplicate_targeted_sale_plan_requires_exact_current_item_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="duplicate-sale-grounding")
                )["goal"]
                phase = {"kind": "prepare_combat", "context": {}}
                observation = {
                    "inventory": {
                        "items": [
                            {"id": 1, "name": "mace", "in_use": True},
                            {"id": 2, "name": "mace"},
                        ]
                    },
                    "equipment": {"equipped": [{"id": 1, "name": "mace"}]},
                    "look": {"room": {"num": 106}},
                }
                vague = [
                    {
                        "tool": "sell",
                        "outcome": "Sell one spare mace.",
                        "verification": "The mace is removed and shillings increase.",
                    }
                ]
                exact = copy.deepcopy(vague)
                exact[0]["outcome"] = "Sell the unequipped mace with exact item id 2."

                self.assertIn(
                    "without an exact current item id",
                    controller._targeted_sale_grounding_error(
                        goal, phase, vague, observation
                    )
                    or "",
                )
                self.assertIsNone(
                    controller._targeted_sale_grounding_error(
                        goal, phase, exact, observation
                    )
                )
            finally:
                controller.storage.close()

    def test_bank_plan_requires_separate_preceding_travel(self) -> None:
        observation = {
            "look": {"room": {"num": 106, "name": "Brownestone Inn"}}
        }
        invalid = [
            {
                "id": "balance",
                "tool": "bank",
                "outcome": "Check the balance at Tos bank room 54.",
                "verification": "A positive bank balance is shown.",
            }
        ]
        valid = [
            {
                "id": "reach-bank",
                "tool": "travel",
                "outcome": "Travel to First Royal Bank of Tos, room 54.",
                "verification": "Current room id is 54.",
            },
            invalid[0],
        ]

        self.assertIn(
            "calls bank before reaching a verified bank room",
            BotController._bank_plan_error(invalid, observation) or "",
        )
        self.assertIsNone(BotController._bank_plan_error(valid, observation))
        self.assertIsNone(
            BotController._bank_plan_error(
                invalid,
                {"look": {"room": {"num": 54, "name": "First Royal Bank of Tos"}}},
            )
        )

    def test_bank_plan_ignores_completed_prefix_after_leaving_bank(self) -> None:
        steps = [
            {
                "id": "check-balance",
                "tool": "bank",
                "outcome": "Check the balance at Tos bank room 54.",
                "verification": "The account balance is shown.",
            },
            {
                "id": "withdraw-funds",
                "tool": "bank",
                "outcome": "Withdraw 100 shillings at Tos bank room 54.",
                "verification": "Inventory contains 100 shillings.",
            },
            {
                "id": "reach-shop",
                "tool": "travel",
                "outcome": "Travel to Joguer's Herbs and Roots, room 104.",
                "verification": "Current room id is 104.",
            },
            {
                "id": "buy-reagents",
                "tool": "shop",
                "outcome": "Buy Create Food reagents from Joguer.",
                "verification": "Inventory contains the required reagents.",
            },
        ]
        observation = {
            "look": {"room": {"num": 104, "name": "Joguer's Herbs and Roots"}}
        }

        self.assertIsNone(
            BotController._bank_plan_error(
                steps,
                observation,
                completed_through_step_id="reach-shop",
            )
        )

    def test_bank_plan_still_checks_future_bank_step_after_completed_prefix(self) -> None:
        steps = [
            {
                "id": "inspect",
                "tool": "look",
                "outcome": "Inspect the current room.",
                "verification": "The room is observed.",
            },
            {
                "id": "withdraw-funds",
                "tool": "bank",
                "outcome": "Withdraw 100 shillings.",
                "verification": "Inventory contains 100 shillings.",
            },
        ]

        self.assertIn(
            "calls bank before reaching a verified bank room",
            BotController._bank_plan_error(
                steps,
                {"look": {"room": {"num": 104, "name": "Joguer's Herbs and Roots"}}},
                completed_through_step_id="inspect",
            )
            or "",
        )

    def test_bank_plan_defers_location_check_until_room_is_observed(self) -> None:
        steps = [
            {
                "id": "withdraw-funds",
                "tool": "bank",
                "outcome": "Withdraw 100 shillings.",
                "verification": "Inventory contains 100 shillings.",
            }
        ]

        self.assertIsNone(BotController._bank_plan_error(steps, {}))

    def test_intrinsic_sale_evidence_survives_id_churn_only_for_unchanged_inventory(self) -> None:
        reason = (
            'Meidei tells you, "I cannot see how you could bear to part with a mace! '
            'I certainly couldn\'t be the one to take it off your hands."'
        )
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="intrinsic-id-churn")
                )["goal"]
                first = {
                    "look": {"room": {"num": 103}},
                    "inventory": {
                        "carry": {
                            "known": True,
                            "items": 2,
                            "load": {"weight": 0, "bulk": 0, "exact": True},
                            "weight_max": 2700,
                            "bulk_max": 2700,
                        },
                        "items": [
                            {"id": 10, "name": "mace"},
                            {"id": 20, "name": "mace"},
                        ]
                    },
                    "equipment": {"equipped": [{"id": 10, "name": "mace"}]},
                }
                second = copy.deepcopy(first)
                second["inventory"]["items"] = [
                    {"id": 30, "name": "mace"},
                    {"id": 40, "name": "mace"},
                ]
                second["equipment"]["equipped"] = [{"id": 30, "name": "mace"}]
                current = copy.deepcopy(second)
                current["inventory"]["items"] = [
                    {"id": 50, "name": "mace"},
                    {"id": 60, "name": "mace"},
                ]
                current["equipment"]["equipped"] = [{"id": 50, "name": "mace"}]

                self.assertEqual(
                    controller.learning.profile(first)["inventory_load_hash"],
                    controller.learning.profile(second)["inventory_load_hash"],
                )
                controller._record_blocked_action(
                    goal, first, "sell", {"to": 1, "items": [10]}, reason
                )
                self.assertEqual(
                    set(),
                    controller._intrinsically_unsellable_item_names(goal, current),
                )
                controller._record_blocked_action(
                    goal, second, "sell", {"to": 2, "items": [40]}, reason
                )

                self.assertEqual(
                    {"mace"},
                    controller._intrinsically_unsellable_item_names(goal, current),
                )
                self.assertEqual(
                    {"10", "40", "50", "60"},
                    controller._intrinsically_unsellable_item_ids(goal, current),
                )
                self.assertIsNotNone(
                    controller._intrinsic_sale_block(
                        goal, {"to": 3, "items": [60]}, current
                    )
                )
                plan_error = controller._targeted_sale_grounding_error(
                    goal,
                    {"kind": "prepare_combat", "context": {}},
                    [
                        {
                            "tool": "sell",
                            "outcome": "Sell exact mace id 60.",
                            "verification": "Shillings increase.",
                        }
                    ],
                    current,
                )
                self.assertIn("across item-id churn", plan_error or "")

                materially_changed = copy.deepcopy(current)
                materially_changed["inventory"]["items"].append(
                    {"id": 70, "name": "mace"}
                )
                materially_changed["inventory"]["carry"]["items"] = 3
                self.assertEqual(
                    set(),
                    controller._intrinsically_unsellable_item_names(
                        goal, materially_changed
                    ),
                )
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

    def test_execution_plan_allows_ten_bounded_steps_but_rejects_eleven(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                broker = SimulatedBroker()
                controller.broker = broker
                controller.last_observation = broker.observe()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="ten-step-plan")
                )["goal"]

                def plan(action_count: int) -> dict[str, Any]:
                    return with_safe_ending(
                        {
                            "summary": "Execute one bounded commerce phase.",
                            "steps": [
                                {
                                    "id": f"action-{index}",
                                    "outcome": f"Complete bounded action {index}.",
                                    "tool": "act",
                                    "verification": "Observe the requested state change.",
                                }
                                for index in range(action_count)
                            ],
                        },
                        100,
                    )

                stored = controller._store_execution_plan(
                    goal,
                    plan(9),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )
                self.assertEqual(10, len(stored["steps"]))

                with self.assertRaisesRegex(ModelError, "1-10 ordered steps"):
                    controller._store_execution_plan(
                        goal,
                        plan(10),
                        grounding=controller.knowledge.validate_goal(goal),
                        revision=True,
                    )
            finally:
                controller.storage.close()

    def test_execution_plan_step_cannot_claim_a_second_tool_action(self) -> None:
        self.assertIn(
            "separate equip_best/wear_best step",
            BotController._commerce_step_error(
                {
                    "tool": "shop",
                    "outcome": "Buy a war mace, then equip it.",
                    "verification": "The war mace is wielded.",
                }
            )
            or "",
        )
        self.assertIn(
            "separate shop step",
            BotController._commerce_step_error(
                {
                    "tool": "travel",
                    "outcome": "Travel to the merchant, then buy armor.",
                    "verification": "Armor is carried.",
                }
            )
            or "",
        )
        self.assertIn(
            "separate escape_underworld/go_through/leave_raza/travel/walk_to step",
            BotController._commerce_step_error(
                {
                    "tool": "sell",
                    "outcome": (
                        "Quote the local buyer; if not, travel to the alternate buyer."
                    ),
                    "verification": "A quote is returned or the room changes.",
                }
            )
            or "",
        )
        self.assertIsNone(
            BotController._commerce_step_error(
                {
                    "tool": "shop",
                    "outcome": "Read the catalogue, then buy the quoted armor.",
                    "verification": "Armor is carried.",
                }
            )
        )

    def test_farm_execution_plan_rejects_later_out_of_phase_step(self) -> None:
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

                with self.assertRaisesRegex(
                    ModelError,
                    "active 'farm' phase boundary: finish-at-tos-bar=walk_to",
                ):
                    controller._store_execution_plan(
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
                controller.last_observation["look"]["room"] = {
                    "num": 54,
                    "name": "First Royal Bank of Tos",
                }
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

    def test_paid_training_controller_plan_uses_only_phase_allowed_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                for tool_name in (
                    "inventory",
                    "equipment",
                    "merchants",
                    "map",
                    "travel",
                    "go_through",
                    "shop",
                    "sell",
                    "sell_all",
                    "bank",
                    "equip_best",
                    "wear_best",
                    "act",
                ):
                    broker.tools.setdefault(
                        tool_name,
                        Tool(
                            tool_name,
                            f"Test {tool_name} tool.",
                            {"type": "object", "properties": {}},
                        ),
                    )
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="training-plan-phase-tools",
                        objective="Learn mace fighting.",
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
                                "maximum_price": 3615,
                            }
                        },
                    )
                )["goal"]

                plan = controller._structured_purchase_controller_plan(goal)
                allowed = {
                    tool["name"]
                    for tool in controller._planner_tools({"kind": "acquire_item"})
                }

                self.assertIn("go_through", allowed)
                self.assertEqual(
                    set(),
                    {
                        step["tool"]
                        for step in plan["steps"]
                        if step["tool"] not in allowed
                    },
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

    def test_partial_travel_is_not_recorded_as_completed_plan_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = PartialTravelBroker()
                controller.broker = broker
                controller.last_observation = broker.observe()
                source_verify_safe_rooms(controller, 54)
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="partial-travel-plan-step")
                )["goal"]
                controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {
                            "summary": "Reach the work room, then finish safely.",
                            "steps": [
                                {
                                    "id": "reach-work-room",
                                    "outcome": "Reach room 99.",
                                    "tool": "travel",
                                    "verification": "Current room id is 99.",
                                }
                            ],
                        },
                        54,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "travel",
                        "arguments": {"to": 99},
                        "rationale": "Continue to the verified work room.",
                        "plan_step_id": "reach-work-room",
                    },
                )

                self.assertTrue(result["partial_progress"])
                self.assertFalse(result["outcome_complete"])
                self.assertEqual(3, broker.room["num"])
                self.assertEqual(
                    [], controller.storage.events(kinds=["action.succeeded"])["events"]
                )
                self.assertEqual(
                    1,
                    len(
                        controller.storage.events(kinds=["action.partial_progress"])[
                            "events"
                        ]
                    ),
                )
                self.assertEqual(
                    [], controller.storage.events(kinds=["action.no_progress"])["events"]
                )
                stored = controller._execution_plan(goal)
                self.assertIsNotNone(stored)
                self.assertEqual(
                    "partial_progress", stored["last_action"]["status"]
                )
                self.assertEqual(
                    "reach-work-room", stored["last_action"]["step_id"]
                )
                self.assertIsNone(
                    controller._plan_revision_authorization(
                        goal,
                        stored,
                        controller.storage.get_runtime("planner_feedback"),
                    )
                )
                feedback = controller.storage.get_runtime("planner_feedback")
                self.assertIn("repeat this same plan step", feedback["message"])
            finally:
                controller.storage.close()

    def test_successful_travel_receipt_prevents_stale_room_plan_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = StalePostTravelBroker()
                controller.broker = broker
                controller.last_observation = broker.observe()
                source_verify_safe_rooms(controller, 54)
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="stale-post-travel-bank-room")
                )["goal"]
                controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {
                            "summary": "Travel to the bank, check it, and finish safely.",
                            "steps": [
                                {
                                    "id": "reach-bank",
                                    "outcome": "Travel to First Royal Bank of Tos room 54.",
                                    "tool": "travel",
                                    "verification": "Current room id is 54.",
                                },
                                {
                                    "id": "check-bank",
                                    "outcome": "Check the balance in bank room 54.",
                                    "tool": "bank",
                                    "verification": "Bank reports the account balance.",
                                },
                            ],
                        },
                        54,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "travel",
                        "arguments": {"to": 54},
                        "rationale": "Reach the bank before using it.",
                        "plan_step_id": "reach-bank",
                    },
                )

                self.assertEqual("travel", result["action"])
                self.assertEqual(100, broker.room["num"])
                self.assertEqual(54, controller._observation_room(controller.last_observation))
                stored = controller._execution_plan(goal)
                self.assertIsNotNone(stored)
                self.assertEqual("reach-bank", stored["last_action"]["step_id"])
                next_observation = controller._reconcile_recent_room_transition(
                    broker.observe()
                )
                self.assertEqual(
                    54,
                    controller._observation_room(next_observation),
                )
                controller.last_observation = next_observation
                self.assertIsNotNone(controller._execution_plan(goal))
                self.assertEqual(
                    [],
                    controller.storage.events(kinds=["planner.plan.invalidated"])[
                        "events"
                    ],
                )
                reconciled = controller.storage.events(
                    kinds=["action.movement_observation_reconciled"]
                )["events"]
                self.assertEqual(2, len(reconciled))
                self.assertEqual(100, reconciled[0]["data"]["stale_observed_room"])
                self.assertEqual(54, reconciled[0]["data"]["receipt_room"])
            finally:
                controller.storage.close()

    def test_create_food_delta_reconciles_next_stale_inventory_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = StalePostCreateFoodBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="stale-post-create-food")
                )["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "cast",
                        "arguments": {"spell": "create food"},
                        "rationale": "Create one required food item.",
                        "plan_step_id": "create-food",
                    },
                )

                self.assertEqual("cast", result["action"])
                cast_arguments = next(
                    arguments for name, arguments in broker.calls if name == "cast"
                )
                self.assertTrue(cast_arguments["observe_created"])
                stale = broker.observe()
                self.assertEqual(
                    1,
                    CriteriaEvaluator.inventory_count(
                        stale["inventory"]["items"], "food"
                    ),
                )
                reconciled = controller._reconcile_recent_inventory_creation(stale)
                self.assertEqual(
                    2,
                    CriteriaEvaluator.inventory_count(
                        reconciled["inventory"]["items"], "food"
                    ),
                )
                events = controller.storage.events(
                    kinds=["action.inventory_observation_reconciled"]
                )["events"]
                self.assertEqual(1, len(events))
            finally:
                controller.storage.close()

    def test_durable_lesson_allows_one_plan_revision_before_phase_breaker(self) -> None:
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

                first = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "inventory",
                        "arguments": {},
                        "rationale": "Retry the same disproved tactic.",
                        "expected_observation": {"inventory": "changed"},
                    },
                )

                self.assertTrue(first["retry_suppressed"])
                self.assertNotIn("campaign_breaker", first)
                self.assertEqual("active", controller.storage.goal(goal["id"])["status"])
                self.assertEqual(
                    phase["id"],
                    controller.storage.active_campaign_phase(run["id"])["id"],
                )
                feedback = controller.storage.get_runtime("planner_feedback")
                self.assertIn("Choose different arguments or a different tool", feedback["message"])

                repeated = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "inventory",
                        "arguments": {},
                        "rationale": "Ignore the feedback and retry unchanged.",
                        "expected_observation": {"inventory": "changed"},
                    },
                )

                self.assertTrue(repeated["campaign_breaker"]["breaker_tripped"])
                self.assertTrue(repeated["strategic_goal_preserved"])
                self.assertIsNone(controller.storage.active_campaign_phase(run["id"]))
                self.assertEqual(
                    "failed", controller.storage.campaign_phases(run["id"])[0]["status"]
                )
                self.assertEqual(
                    phase["id"], repeated["campaign_breaker"]["phase_id"]
                )
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

    def test_recent_research_avoidance_is_soft_and_does_not_retire_farm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="research-room-exclusion")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                research = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "research_progression",
                        "objective": "Find a reachable farm room.",
                        "success_criteria": [
                            {
                                "id": "candidate",
                                "kind": "phase_action_succeeded",
                                "tools": ["hunting_grounds"],
                            }
                        ],
                        "abandon_predicates": [],
                        "budget": {"max_actions": 40, "max_minutes": 30},
                        "context": {"avoid_rooms": [586]},
                        "rationale": "Exclude previously failed routes.",
                    },
                    mode="start",
                )
                controller.storage.transition_campaign_phase(
                    research["id"], "succeeded"
                )
                farm = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "farm",
                        "objective": "Reuse the grounded giant-rat room.",
                        "success_criteria": [
                            {
                                "id": "hp-101",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 101,
                            }
                        ],
                        "abandon_predicates": [],
                        "budget": {"max_actions": 120, "max_minutes": 60},
                        "context": {
                            "room": 586,
                            "target": "giant rat",
                            "use_safe_spots": True,
                        },
                        "rationale": "Recent use alone is not a safety failure.",
                    },
                    mode="start",
                )

                _, active, _ = controller._campaign_turn_state(
                    goal,
                    broker.observe(),
                    {},
                )

                phases = controller.storage.campaign_phases(run["id"])
                retained = next(item for item in phases if item["id"] == farm["id"])
                self.assertEqual("active", retained["status"])
                self.assertEqual(farm["id"], active["id"])
                self.assertIsNone(
                    controller._campaign_phase_grounding_blocker(
                        farm, broker.observe(), avoid_rooms={"586"}
                    )
                )
            finally:
                controller.storage.close()

    def test_launch_only_farm_phase_is_rejected_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                phase = {
                    "kind": "farm",
                    "success_criteria": [
                        {
                            "id": "keeper-launched",
                            "kind": "phase_action_succeeded",
                            "tools": ["autopilot"],
                        }
                    ],
                    "context": {
                        "room": 557,
                        "target": "groundworm larva",
                        "use_safe_spots": True,
                    },
                }

                blocker = controller._campaign_phase_grounding_blocker(
                    phase, SimulatedBroker().observe()
                )

                self.assertEqual("invalid_farm_phase_outcome", blocker["kind"])
            finally:
                controller.storage.close()

    def test_persisted_launch_only_farm_is_retired_before_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="persisted-launch-only-farm")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                phase = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "farm",
                        "objective": "Legacy launch-only farm.",
                        "success_criteria": [
                            {
                                "id": "keeper-launched",
                                "kind": "phase_action_succeeded",
                                "tools": ["autopilot"],
                            }
                        ],
                        "abandon_predicates": [],
                        "budget": {"max_actions": 120, "max_minutes": 45},
                        "context": {
                            "room": 557,
                            "target": "groundworm larva",
                            "use_safe_spots": True,
                        },
                        "rationale": "Persisted by an older controller.",
                    },
                    mode="start",
                )
                controller.storage.set_runtime(
                    "phase_completion_checkpoints_v1",
                    {
                        phase["id"]: {
                            "phase_id": phase["id"],
                            "completion": {"all_met": True},
                        }
                    },
                )

                outcome = controller._evaluate_campaign_phase(
                    goal, run, phase, SimulatedBroker().observe()
                )

                self.assertTrue(outcome.failed)
                self.assertEqual("failed", outcome.phase["status"])
                self.assertEqual(
                    "invalid_farm_phase_outcome",
                    outcome.detail["grounding_blocker"]["kind"],
                )
                self.assertEqual(
                    {},
                    controller.storage.get_runtime(
                        "phase_completion_checkpoints_v1", {}
                    ),
                )
            finally:
                controller.storage.close()

    def test_persisted_action_only_mutating_preparation_is_retired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="persisted-action-only-preparation")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                phase = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "prepare_combat",
                        "objective": "Legacy equipment and food preparation.",
                        "success_criteria": [
                            {
                                "id": "equip-returned",
                                "kind": "phase_action_succeeded",
                                "tools": ["equip_best"],
                            },
                            {
                                "id": "cast-returned",
                                "kind": "phase_action_succeeded",
                                "tools": ["cast"],
                            },
                        ],
                        "abandon_predicates": [],
                        "context": {},
                    },
                    mode="start",
                )

                outcome = controller._evaluate_campaign_phase(
                    goal, run, phase, SimulatedBroker().observe()
                )

                self.assertTrue(outcome.failed)
                self.assertEqual("failed", outcome.phase["status"])
                self.assertEqual(
                    "invalid_prepare_combat_phase_outcome",
                    outcome.detail["grounding_blocker"]["kind"],
                )
                self.assertIn(
                    "observable equipment, inventory, or capacity state",
                    outcome.detail["reason"],
                )
            finally:
                controller.storage.close()

    def test_research_prefers_recent_successful_farm_tactic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                broker.vitals["health"] = {"current": 33, "max": 33}
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="research-reuse-success",
                        title="Reach 100 max HP",
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
                observation = broker.observe()
                run = controller.storage.ensure_campaign_run(goal)
                successful = controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "farm",
                        "objective": "Farm slime in room 583 to reach 33 HP.",
                        "success_criteria": [],
                        "abandon_predicates": [],
                        "budget": {"max_actions": 120, "max_minutes": 180},
                        "context": {
                            "room": 583,
                            "target": "slime",
                            "use_safe_spots": True,
                        },
                        "rationale": "Grounded progression farm.",
                    },
                    mode="start",
                )
                controller.storage.transition_campaign_phase(
                    successful["id"], "succeeded"
                )
                research = controller.storage.create_campaign_phase(
                    run,
                    {
                        **controller.campaign.fallback_phase(goal, observation),
                        "context": {
                            "deterministic_fallback": True,
                            "next_hp_milestone": 34,
                            "avoid_rooms": [583],
                        },
                    },
                    mode="start",
                )
                attempt_id = controller.storage.create_phase_attempt(
                    research["id"],
                    semantic_action="hunting_grounds",
                    signature="level-33-grounds",
                    expected_effect={"progression_candidates": "returned"},
                )
                controller.storage.update_phase_attempt(
                    attempt_id,
                    "succeeded",
                    result={
                        "for_level": 33,
                        "prey": [
                            {
                                "creature": "slime",
                                "best_room": 562,
                                "rooms": [562, 583],
                            }
                        ],
                    },
                )

                validation = controller._research_farm_recipe_validation(
                    goal, run, research, observation
                )

                self.assertEqual("selected", validation["status"])
                self.assertEqual(583, validation["recipe"]["room"])
                self.assertEqual("slime", validation["recipe"]["target"])
                self.assertEqual(
                    "recent_successful_tactic",
                    validation["recipe"]["selection_basis"],
                )
            finally:
                controller.storage.close()

    def test_farm_phase_follows_structured_route_one_live_hop_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                phase = {
                    "kind": "farm",
                    "context": {
                        "room": 1016,
                        "target": "mummy",
                        "use_safe_spots": True,
                        "route": {
                            "from": 1011,
                            "via": [{"to": 1012}, {"to": 1016}],
                        },
                    },
                }
                execution_plan = {
                    "steps": [
                        {
                            "id": "reach-farm",
                            "tool": "travel",
                            "outcome": "Reach Mausoleum room 1016 via Raza.",
                        },
                        {
                            "id": "finish-safe",
                            "tool": "travel",
                            "outcome": "Return to room 1011.",
                        },
                    ],
                    "safe_ending": {"room_id": 1011, "step_id": "finish-safe"},
                }
                at_inn = {
                    "look": {
                        "room": {"num": 1011, "name": "Raza Inn"},
                        "exits": [{"to": 1012, "reachable": True}],
                    }
                }
                at_raza = {
                    "look": {
                        "room": {"num": 1012, "name": "Raza"},
                        "exits": [{"to": 1016, "reachable": True}],
                    }
                }

                first = controller._structured_farm_route_action(
                    phase, at_inn, execution_plan
                )
                second = controller._structured_farm_route_action(
                    phase, at_raza, execution_plan
                )

                self.assertEqual({"to": 1012}, first["arguments"])
                self.assertEqual({"to": 1016}, second["arguments"])
                self.assertEqual("reach-farm", first["plan_step_id"])
                self.assertEqual("reach-farm", second["plan_step_id"])
                at_raza["look"]["exits"] = []
                self.assertIsNone(
                    controller._structured_farm_route_action(
                        phase, at_raza, execution_plan
                    )
                )
            finally:
                controller.storage.close()

    def test_research_phase_crosses_declared_live_first_exit_before_more_looks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                phase = {
                    "kind": "research_progression",
                    "success_criteria": [
                        {
                            "id": "in-raza",
                            "kind": "location_reached",
                            "room_id": 1012,
                        },
                        {
                            "id": "candidate",
                            "kind": "phase_action_succeeded",
                            "tools": ["hunting_grounds"],
                        },
                    ],
                    "context": {"start_room": 1011, "first_exit_room": 1012},
                }
                observation = {
                    "look": {
                        "room": {"num": 1011, "name": "Raza Inn"},
                        "exits": [
                            {
                                "kind": "go",
                                "to": 1012,
                                "reachable": True,
                                "stand_on": {"col": 6, "row": 1},
                            }
                        ],
                    },
                    "status": {"vitals": {"health": {"value": 24, "max": 24}}},
                }
                execution_plan = {
                    "steps": [
                        {
                            "id": "cross-exit",
                            "tool": "travel",
                            "outcome": "Travel from 1011 to 1012.",
                        },
                        {
                            "id": "find-prey",
                            "tool": "hunting_grounds",
                            "outcome": "Find eligible prey after reaching Raza.",
                        },
                        {
                            "id": "finish-safe",
                            "tool": "travel",
                            "outcome": "Return to room 1011.",
                        },
                    ],
                    "safe_ending": {"room_id": 1011, "step_id": "finish-safe"},
                }

                action = controller._structured_research_progression_action(
                    phase, observation, execution_plan
                )

                self.assertEqual("travel", action["tool"])
                self.assertEqual({"to": 1012}, action["arguments"])
                self.assertEqual("cross-exit", action["plan_step_id"])
            finally:
                controller.storage.close()

    def test_research_phase_collects_progression_evidence_after_first_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                phase = {
                    "kind": "research_progression",
                    "success_criteria": [
                        {
                            "id": "candidate",
                            "kind": "phase_action_succeeded",
                            "tools": ["hunting_grounds"],
                        }
                    ],
                    "context": {"start_room": 1011, "first_exit_room": 1012},
                }
                observation = {
                    "look": {
                        "room": {"num": 1012, "name": "Raza"},
                        "vitals": {"health": {"value": 24, "max": 24}},
                    },
                    "status": {"vitals": {"health": {"value": 24, "max": 24}}},
                }
                execution_plan = {
                    "steps": [
                        {
                            "id": "find-prey",
                            "tool": "hunting_grounds",
                            "outcome": "Find eligible prey after reaching Raza.",
                        },
                        {
                            "id": "finish-safe",
                            "tool": "travel",
                            "outcome": "Return to room 1011.",
                        },
                    ],
                    "safe_ending": {"room_id": 1011, "step_id": "finish-safe"},
                }

                action = controller._structured_research_progression_action(
                    phase, observation, execution_plan
                )

                self.assertEqual("hunting_grounds", action["tool"])
                self.assertEqual({"for_level": 24, "limit": 6}, action["arguments"])
                self.assertEqual("find-prey", action["plan_step_id"])
            finally:
                controller.storage.close()

    def test_deterministic_fallback_executes_required_hunting_grounds_despite_prey_alias(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                phase = {
                    "kind": "research_progression",
                    "success_criteria": [
                        {
                            "id": "candidate",
                            "kind": "phase_action_succeeded",
                            "tools": ["hunting_grounds"],
                        }
                    ],
                    "context": {"deterministic_fallback": True},
                }
                observation = {
                    "look": {
                        "room": {"num": 1012, "name": "Raza"},
                        "vitals": {"health": {"value": 24, "max": 24}},
                    },
                    "status": {"vitals": {"health": {"value": 24, "max": 24}}},
                }
                execution_plan = {
                    "steps": [
                        {
                            "id": "find-prey",
                            "tool": "prey",
                            "outcome": "Find eligible prey for the current level.",
                        },
                        {
                            "id": "finish-safe",
                            "tool": "travel",
                            "outcome": "Return to room 1011.",
                        },
                    ],
                    "safe_ending": {"room_id": 1011, "step_id": "finish-safe"},
                }

                action = controller._structured_research_progression_action(
                    phase, observation, execution_plan
                )

                self.assertEqual("hunting_grounds", action["tool"])
                self.assertEqual({"for_level": 24, "limit": 6}, action["arguments"])
                self.assertEqual("find-prey", action["plan_step_id"])
            finally:
                controller.storage.close()

    def test_manager_research_executes_required_hunting_grounds_despite_prey_alias(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                phase = {
                    "kind": "research_progression",
                    "success_criteria": [
                        {
                            "id": "candidate",
                            "kind": "phase_action_succeeded",
                            "tools": ["hunting_grounds"],
                        }
                    ],
                    "context": {},
                }
                observation = {
                    "look": {
                        "room": {"num": 106, "name": "Brownestone Inn"},
                        "vitals": {"health": {"value": 34, "max": 34}},
                    },
                    "status": {
                        "vitals": {"health": {"value": 34, "max": 34}}
                    },
                }
                execution_plan = {
                    "steps": [
                        {
                            "id": "find-prey",
                            "tool": "prey",
                            "outcome": "Find eligible prey for the current level.",
                        },
                        {
                            "id": "finish-safe",
                            "tool": "travel",
                            "outcome": "Remain safely at Brownestone Inn.",
                        },
                    ],
                    "safe_ending": {"room_id": 106, "step_id": "finish-safe"},
                }

                action = controller._structured_research_progression_action(
                    phase, observation, execution_plan
                )

                self.assertEqual("hunting_grounds", action["tool"])
                self.assertEqual({"for_level": 34, "limit": 6}, action["arguments"])
                self.assertEqual("find-prey", action["plan_step_id"])
            finally:
                controller.storage.close()

    def test_research_completion_selects_non_quarantined_alternate_and_hands_off(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                broker.vitals["health"] = {"current": 27, "max": 27}
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="research-recipe-handoff",
                        title="Reach 100 max HP",
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
                observation = broker.observe()
                run = controller.storage.ensure_campaign_run(goal)
                research = controller.storage.create_campaign_phase(
                    run,
                    {
                        **controller.campaign.fallback_phase(goal, observation),
                        "context": {
                            "deterministic_fallback": True,
                            "next_hp_milestone": 28,
                            "avoid_rooms": [568, 586, 603],
                        },
                    },
                    mode="start",
                )
                controller.storage.set_runtime(
                    "farm_tactic_quarantine_v1",
                    {
                        "568": {
                            "room": 568,
                            "target": "centipede",
                            "use_safe_spots": True,
                            "reasons": ["safe spot failed survivability"],
                        },
                        "586": {
                            "room": 586,
                            "target": "giant rat",
                            "use_safe_spots": True,
                            "reasons": ["combat death"],
                        },
                        "603": {
                            "room": 603,
                            "target": "giant rat",
                            "use_safe_spots": True,
                            "reasons": ["safe spot failed survivability"],
                        },
                    },
                )
                attempt_id = controller.storage.create_phase_attempt(
                    research["id"],
                    semantic_action="hunting_grounds",
                    signature="level-27-grounds",
                    expected_effect={"progression_candidates": "returned"},
                )
                controller.storage.update_phase_attempt(
                    attempt_id,
                    "succeeded",
                    result={
                        "for_level": 27,
                        "prey": [
                            {
                                "creature": "centipede",
                                "best_room": 568,
                                "rooms": [568, 545, 593, 554, 586, 574],
                            },
                            {
                                "creature": "giant rat",
                                "best_room": 586,
                                "rooms": [586, 535, 603, 575],
                            },
                        ],
                    },
                )
                controller._safe_ending_reached = lambda *_: {  # type: ignore[method-assign]
                    "met": True
                }

                result = controller._reconcile_existing_campaign_phase(
                    goal, observation
                )

                self.assertTrue(result["research_handoff"])
                self.assertEqual("succeeded", result["phase"]["status"])
                farm = result["next_phase"]
                self.assertEqual("farm", farm["kind"])
                self.assertEqual(545, farm["context"]["room"])
                self.assertEqual("centipede", farm["context"]["target"])
                self.assertTrue(farm["context"]["use_safe_spots"])
                self.assertEqual(0.60, farm["context"]["flee_below"])
                self.assertEqual(100, farm["context"]["fight_above_vigor"])
                self.assertEqual(28, farm["context"]["next_hp_milestone"])
                self.assertEqual(
                    farm["id"],
                    controller.storage.active_campaign_phase(run["id"])["id"],
                )
                self.assertEqual(
                    1,
                    len(
                        controller.storage.events(
                            kinds=["campaign.research.recipe_selected"]
                        )["events"]
                    ),
                )
            finally:
                controller.storage.close()

    def test_research_lookup_does_not_complete_without_usable_farm_recipe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                broker.vitals["health"] = {"current": 27, "max": 27}
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="research-no-usable-recipe",
                        title="Reach 100 max HP",
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
                observation = broker.observe()
                run = controller.storage.ensure_campaign_run(goal)
                controller.storage.set_runtime(
                    "farm_tactic_quarantine_v1",
                    {
                        "568": {
                            "room": 568,
                            "target": "centipede",
                            "use_safe_spots": True,
                            "guidance": "Choose a safer grounded room.",
                            "reasons": ["verified live over-level hazard"],
                        }
                    },
                )
                research = controller.storage.create_campaign_phase(
                    run,
                    {
                        **controller.campaign.fallback_phase(goal, observation),
                        "context": {
                            "deterministic_fallback": True,
                            "next_hp_milestone": 28,
                            "avoid_rooms": [568],
                        },
                    },
                    mode="start",
                )
                attempt_id = controller.storage.create_phase_attempt(
                    research["id"],
                    semantic_action="hunting_grounds",
                    signature="only-excluded-room",
                    expected_effect={"progression_candidates": "returned"},
                )
                exhausted_result = {
                    "for_level": 27,
                    "prey": [
                        {
                            "creature": "centipede",
                            "best_room": 568,
                            "rooms": [568],
                        }
                    ],
                }
                controller.storage.update_phase_attempt(
                    attempt_id,
                    "succeeded",
                    result=exhausted_result,
                )
                controller._safe_ending_reached = lambda *_: {  # type: ignore[method-assign]
                    "met": True
                }

                outcome = controller._evaluate_campaign_phase(
                    goal, run, research, observation
                )

                self.assertFalse(outcome.completed)
                self.assertTrue(outcome.failed)
                self.assertTrue(outcome.detail["action_criteria_met"])
                self.assertFalse(outcome.detail["all_met"])
                self.assertEqual(
                    "no_usable_candidate",
                    outcome.detail["recipe_validation"]["status"],
                )
                self.assertIsNone(
                    controller.storage.active_campaign_phase(run["id"])
                )
                finished = next(
                    phase
                    for phase in controller.storage.campaign_phases(run["id"])
                    if phase["id"] == research["id"]
                )
                self.assertEqual(
                    "no_usable_candidate",
                    finished["context"]["recipe_validation"]["status"],
                )
                self.assertNotIn("farm_recipe", finished["context"])

                repeated = controller.storage.create_campaign_phase(
                    run,
                    {
                        **controller.campaign.fallback_phase(goal, observation),
                        "context": {
                            "deterministic_fallback": True,
                            "next_hp_milestone": 28,
                            "avoid_rooms": [568],
                        },
                    },
                    mode="start",
                )
                repeated_attempt = controller.storage.create_phase_attempt(
                    repeated["id"],
                    semantic_action="hunting_grounds",
                    signature="same-excluded-room-again",
                    expected_effect={"progression_candidates": "returned"},
                )
                controller.storage.update_phase_attempt(
                    repeated_attempt,
                    "succeeded",
                    result=exhausted_result,
                )

                reconciled = controller._reconcile_existing_campaign_phase(
                    goal, observation
                )

                self.assertFalse(reconciled["goal_blocked"])
                self.assertTrue(reconciled["strategic_goal_preserved"])
                self.assertEqual(
                    "active", controller.storage.goal(goal["id"])["status"]
                )
                self.assertEqual(
                    "no_usable_farm_recipe",
                    reconciled["external_blocker"]["kind"],
                )
            finally:
                controller.storage.close()

    def test_post_death_farm_gate_pushes_flask_support_and_resumes_recipe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                broker.vitals["health"] = {"current": 27, "max": 27}
                broker.inventory_items.append({"id": 9, "name": "flask", "amount": 1})
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="farm-healing-support",
                        title="Reach 100 max HP",
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
                run = controller.storage.ensure_campaign_run(goal)
                farm = controller.campaign.apply_manager_decision(
                    run,
                    goal,
                    {
                        "decision": "start_phase",
                        "phase": {
                            "kind": "farm",
                            "objective": "Farm centipedes in room 545 to reach 28 HP.",
                            "success_criteria": [
                                {
                                    "id": "hp-28",
                                    "kind": "numeric_threshold",
                                    "metric": "status.vitals.health.max",
                                    "operator": ">=",
                                    "value": 28,
                                }
                            ],
                            "abandon_predicates": [],
                            "budget": {"max_actions": 120, "max_minutes": 180},
                            "context": {
                                "room": 545,
                                "target": "centipede",
                                "use_safe_spots": True,
                                "flee_below": 0.60,
                                "fight_above_vigor": 100,
                            },
                            "rationale": "Use the validated research recipe.",
                        },
                    },
                    observation=broker.observe(),
                )
                controller.storage.set_runtime(
                    "combat_outcomes_v1",
                    [
                        {
                            "id": "recent-death",
                            "occurred_at": "2026-08-11T00:00:00Z",
                            "target": "giant rat",
                            "outcome": "died",
                            "died": True,
                            "killed": False,
                        }
                    ],
                )

                support = controller._ensure_farm_healing_support_phase(
                    goal, broker.observe()
                )

                self.assertEqual("acquire_item", support["kind"])
                self.assertEqual(farm["id"], support["parent_phase_id"])
                self.assertEqual(
                    {
                        "id": "post-death-healing-flasks-4",
                        "kind": "inventory_contains",
                        "item": "flask",
                        "count": 4,
                    },
                    support["success_criteria"][0],
                )
                paused_farm = next(
                    phase
                    for phase in controller.storage.campaign_phases(run["id"])
                    if phase["id"] == farm["id"]
                )
                self.assertEqual("paused", paused_farm["status"])
                self.assertIsNone(
                    controller._ensure_farm_healing_support_phase(
                        goal, broker.observe()
                    )
                )

                supplied = broker.observe()
                supplied["inventory"]["items"] = [
                    {"id": 10, "name": "flask", "amount": 4}
                ]
                completed = controller.campaign.evaluate_phase(
                    goal,
                    run,
                    support,
                    supplied,
                )

                self.assertTrue(completed.completed)
                resumed = controller.storage.active_campaign_phase(run["id"])
                self.assertEqual(farm["id"], resumed["id"])
                self.assertEqual("active", resumed["status"])
            finally:
                controller.storage.close()

    def test_return_phase_uses_one_way_raza_exit_at_hp_25(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 52)
                goal = {
                    "title": "Increase Max HP to 25 and Leave Raza",
                    "objective": "Raise max HP to 25, then leave Raza.",
                    "success_criteria": [
                        {
                            "id": "left_raza",
                            "kind": "event_occurred",
                            "event_kind": "raza.left",
                            "after_cursor": 0,
                        }
                    ],
                }
                phase = {
                    "kind": "return_home",
                    "objective": "Leave Raza for a safe mainland room.",
                }
                observation = {
                    "look": {
                        "room": {"num": 1016, "name": "Mausoleum"},
                        "vitals": {"health": {"value": 25, "max": 25}},
                    },
                    "status": {"vitals": {"health": {"value": 25, "max": 25}}},
                }

                action = controller._structured_raza_exit_action(
                    goal, phase, observation, None
                )

                self.assertEqual("leave_raza", action["tool"])
                self.assertEqual({"then_travel_to": 52}, action["arguments"])
            finally:
                controller.storage.close()

    def test_verified_raza_exit_completes_goal_in_any_safe_mainland_room(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = RazaExitBroker()
                controller.broker = broker
                source_verify_safe_rooms(controller, 52)
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="raza-exit-event",
                        title="Increase Max HP to 25 and Leave Raza",
                        objective="Raise max HP to 25, then leave Raza.",
                        success_criteria=[
                            {
                                "id": "hp_25",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 25,
                            },
                            {
                                "id": "left_raza",
                                "kind": "event_occurred",
                                "event_kind": "raza.left",
                                "after_cursor": 0,
                            },
                        ],
                    )
                )["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "leave_raza",
                        "arguments": {"then_travel_to": 52},
                        "rationale": "Graduate through the one-way museum portal.",
                        "expected_observation": {
                            "event_kind": "raza.left",
                            "room_id": 52,
                        },
                    },
                )

                self.assertTrue(result["completion"]["all_met"])
                self.assertEqual("succeeded", controller.storage.goal(goal["id"])["status"])
                self.assertEqual(
                    1,
                    len(
                        controller.storage.events(
                            kinds=["raza.left"], goal_id=goal["id"]
                        )["events"]
                    ),
                )
                self.assertEqual("Tos Inn", broker.room["name"])
            finally:
                controller.storage.close()

    def test_research_phase_does_not_cross_an_unobserved_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                phase = {
                    "kind": "research_progression",
                    "success_criteria": [],
                    "context": {"start_room": 1011, "first_exit_room": 1012},
                }
                observation = {
                    "look": {"room": {"num": 1011, "name": "Raza Inn"}, "exits": []}
                }
                execution_plan = {
                    "steps": [
                        {
                            "id": "cross-exit",
                            "tool": "travel",
                            "outcome": "Travel from 1011 to 1012.",
                        }
                    ],
                    "safe_ending": {"room_id": 1011, "step_id": "finish-safe"},
                }

                self.assertIsNone(
                    controller._structured_research_progression_action(
                        phase, observation, execution_plan
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

    def test_progression_lookup_cache_is_scoped_to_campaign_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="phase-scoped-evidence")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                phase_data = {
                    "kind": "research_progression",
                    "objective": "Find a farm recipe.",
                    "success_criteria": [
                        {
                            "id": "recipe",
                            "kind": "phase_action_succeeded",
                            "tools": ["hunting_grounds"],
                        }
                    ],
                    "abandon_predicates": [],
                    "budget": {"max_actions": 10, "max_minutes": 10},
                    "context": {},
                    "rationale": "Collect bounded evidence.",
                }
                first_phase = controller.storage.create_campaign_phase(
                    run, phase_data, mode="start"
                )
                observation = {"look": {"room": {"num": 1012, "name": "Raza"}}}
                arguments = {"room": 1016}
                result = {"room": 1016, "generates": [{"creature": "mummy"}]}

                first = controller._repeated_evidence_reason(
                    "hunting_grounds", arguments, result, observation
                )
                replay = controller._repeated_evidence_reason(
                    "hunting_grounds", arguments, result, observation
                )
                controller.storage.transition_campaign_phase(
                    first_phase["id"], "succeeded"
                )
                controller.storage.create_campaign_phase(
                    run, phase_data, mode="start"
                )
                next_phase = controller._repeated_evidence_reason(
                    "hunting_grounds", arguments, result, observation
                )

                self.assertIsNone(first)
                self.assertIn("identical evidence lookup", replay or "")
                self.assertIsNone(next_phase)
            finally:
                controller.storage.close()

    def test_legacy_unscoped_evidence_lesson_is_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="repair-unscoped-evidence")
                )["goal"]
                reason = (
                    "Preparation failure budget exhausted without verified goal "
                    "progress: repeated identical evidence lookup returned no new evidence"
                )
                lesson = controller.learning.defer_goal(
                    goal,
                    broker.observe(),
                    tool="hunting_grounds",
                    arguments={"room": 1016},
                    reason=reason,
                    classification="ineffective_tactic",
                    scope="tactic",
                    block=False,
                )["lesson"]
                controller.storage.set_runtime(
                    "blocked_actions",
                    [
                        {
                            "tool": "hunting_grounds",
                            "arguments": {"room": 1016},
                            "reason": reason,
                        }
                    ],
                )

                repaired = controller._repair_unscoped_evidence_lookup_lessons()

                self.assertEqual([lesson["id"]], [item["id"] for item in repaired])
                self.assertEqual(
                    "resolved",
                    controller.storage.goal_lesson(lesson["id"])["status"],
                )
                self.assertEqual(
                    [], controller.storage.get_runtime("blocked_actions", [])
                )
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

    def test_identical_read_only_shop_catalogue_replay_invalidates_purchase_tactic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                observation = {
                    "look": {"room": {"num": 52, "name": "Bhrama & Falcon"}}
                }
                arguments = {"seller": 10}
                result = {"items": [{"id": 348, "name": "mace", "cost": 40}]}

                first = controller._repeated_evidence_reason(
                    "shop", arguments, result, observation
                )
                second = controller._repeated_evidence_reason(
                    "shop", arguments, result, observation
                )

                self.assertIsNone(first)
                self.assertIn("identical evidence lookup", second or "")
                self.assertTrue(
                    controller._failure_invalidates_plan("shop", second)
                )
                guidance = controller._no_progress_guidance("shop", second or "")
                self.assertIn("read-only success is evidence, not progress", guidance)

                # A real purchase remains a mutation and is never classified
                # as a repeated catalogue lookup by this guard.
                self.assertIsNone(
                    controller._repeated_evidence_reason(
                        "shop",
                        {"seller": 10, "buy_ids": [348]},
                        result,
                        observation,
                    )
                )
            finally:
                controller.storage.close()

    def test_server_refusal_message_is_no_progress(self) -> None:
        reason = BotController._no_progress_reason(
            {"verb": "get", "messages": ["You're unable to pick up Paddock."]},
            {},
            tool="act",
        )
        self.assertEqual("You're unable to pick up Paddock.", reason)

    def test_travel_cycle_is_failure_even_when_the_last_room_changed(self) -> None:
        reason = BotController._no_progress_reason(
            {
                "arrived": False,
                "reason": "gave up after 25 hops",
                "log": [
                    {"from": "Main gate", "to": "Cor Noth", "ok": True},
                    {"from": "Cor Noth", "to": "The Flatlands", "ok": True},
                    {"from": "The Flatlands", "to": "Main gate", "ok": True},
                    {"from": "Main gate", "to": "Cor Noth", "ok": True},
                ],
                "now": {"room": {"num": 584, "name": "The Flatlands"}},
            },
            {"look": {"room": {"num": 574, "name": "Main gate"}}},
            tool="travel",
            arguments={"to": 153},
        )

        self.assertIn("travel route cycled", reason or "")
        self.assertTrue(BotController._failure_invalidates_plan("travel", reason))
        self.assertIn(
            "different source-verified safe ending",
            BotController._no_progress_guidance("travel", reason or ""),
        )

    def test_linear_partial_travel_can_resume_from_the_new_room(self) -> None:
        reason = BotController._no_progress_reason(
            {
                "arrived": False,
                "reason": "gave up after 25 hops",
                "log": [
                    {"from": "Room A", "to": "Room B", "ok": True},
                    {"from": "Room B", "to": "Room C", "ok": True},
                ],
                "now": {"room": {"num": 3, "name": "Room C"}},
            },
            {"look": {"room": {"num": 1, "name": "Room A"}}},
            tool="travel",
            arguments={"to": 99},
        )

        self.assertIsNone(reason)

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
        reason = 'Frisconar says, "Come back when you have enough money for the flask."'
        self.assertTrue(BotController._failure_invalidates_plan("shop", reason))
        context = BotController._failure_context(
            "shop",
            reason,
            {"inventory": {"items": []}},
        )
        self.assertEqual("purchase_funds_insufficient", context["kind"])
        self.assertIn("Invalidate the current purchase tactic", context["purpose"])

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

    def test_live_no_need_sale_refusal_invalidates_only_the_execution_plan(self) -> None:
        reason = 'Caramo tells you, "I simply have no need for that."'

        context = BotController._failure_context("sell", reason, {})
        guidance = BotController._no_progress_guidance("sell", reason)

        self.assertEqual("merchant_rejected_sale", (context or {}).get("kind"))
        self.assertIn("only this item/merchant tactic", (context or {}).get("purpose", ""))
        self.assertIn("Do not retry the same item with this merchant", guidance)
        self.assertTrue(BotController._failure_invalidates_plan("sell", reason))
        self.assertFalse(BotController._failure_invalidates_plan("travel", reason))

        archaic_reason = 'Pritchett tells you, "Whyfore dost you offer me that?"'
        archaic_context = BotController._failure_context("sell", archaic_reason, {})
        archaic_guidance = BotController._no_progress_guidance("sell", archaic_reason)
        self.assertEqual("merchant_rejected_sale", (archaic_context or {}).get("kind"))
        self.assertIn("Do not retry the same item with this merchant", archaic_guidance)
        self.assertTrue(BotController._failure_invalidates_plan("sell", archaic_reason))

    def test_intrinsic_item_refusal_blocks_only_exact_item_across_merchants(self) -> None:
        reason = (
            'Meidei tells you, "I cannot see how you could bear to part with a mace! '
            'I certainly couldn\'t be the one to take it off your hands."'
        )
        context = BotController._failure_context("sell", reason, {})
        guidance = BotController._no_progress_guidance("sell", reason)

        self.assertEqual("item_not_npc_transferable", (context or {}).get("kind"))
        self.assertEqual(
            "server_can_be_given_to_npc_check", (context or {}).get("source")
        )
        self.assertIn("before evaluating buyer preference", (context or {}).get("purpose", ""))
        self.assertIn("Do not try this item id with another merchant", guidance)
        self.assertIn("do not repeat buyer discovery", guidance)

        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="intrinsic-item-sale-refusal")
                )["goal"]
                observation = {
                    "look": {"room": {"num": 103, "name": "Raza Inn"}},
                    "inventory": {
                        "items": [
                            {"id": 7887, "name": "mace", "in_use": True},
                            {"id": 11420, "name": "mace"},
                        ]
                    },
                    "equipment": {"equipped": [{"id": 7887, "name": "mace"}]},
                }
                controller._record_blocked_action(
                    goal,
                    observation,
                    "sell",
                    {"to": 674, "items": [7887], "confirm": False},
                    reason,
                )
                elsewhere = copy.deepcopy(observation)
                elsewhere["look"]["room"] = {"num": 106, "name": "Other shop"}

                self.assertIsNotNone(
                    controller._blocked_action(
                        goal,
                        elsewhere,
                        "sell",
                        {"to": 999, "items": [7887], "confirm": False},
                    )
                )
                self.assertIsNone(
                    controller._blocked_action(
                        goal,
                        elsewhere,
                        "sell",
                        {"to": 999, "items": [11420], "confirm": False},
                    )
                )
                with self.assertRaisesRegex(
                    ModelError, "exact instance cannot be given to any NPC"
                ):
                    controller._guard_prepare_combat_sale(
                        goal,
                        {"kind": "prepare_combat", "context": {}},
                        "sell",
                        {"to": 999, "items": [7887], "confirm": False},
                        observation,
                    )

                reused = [
                    {
                        "id": "sell-blocked-mace",
                        "tool": "sell",
                        "outcome": "Sell exact mace item id 7887 to merchant 999.",
                        "verification": "Carried shillings increase after selling id 7887.",
                    }
                ]
                replacement = copy.deepcopy(reused)
                replacement[0]["outcome"] = "Sell exact mace item id 11420."
                replacement[0]["verification"] = (
                    "Carried shillings increase after selling id 11420."
                )
                self.assertIn(
                    "cannot be given to any NPC",
                    controller._sale_recovery_plan_error(goal, reused) or "",
                )
                self.assertIsNone(
                    controller._sale_recovery_plan_error(goal, replacement)
                )
                self.assertIn("item_not_npc_transferable", PLANNER_SYSTEM)
                self.assertIn("Never call merchants", PLANNER_SYSTEM)
            finally:
                controller.storage.close()

    def test_sale_refusal_replan_requires_buyer_discovery_before_sale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                broker = SimulatedBroker()
                for tool_name in ("merchants", "sell"):
                    broker.tools[tool_name] = Tool(
                        tool_name,
                        f"Test {tool_name} tool.",
                        {"type": "object", "properties": {"agent": {}}},
                    )
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="sale-refusal-plan-grounding")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                controller.storage.create_campaign_phase(
                    run,
                    {
                        "kind": "prepare_combat",
                        "objective": "Raise funds for equipment.",
                        "success_criteria": [
                            {
                                "id": "equipment-known",
                                "kind": "state_equals",
                                "path": "equipment.known",
                                "value": True,
                            }
                        ],
                        "context": {},
                    },
                    mode="start",
                )
                controller.last_observation = broker.observe()
                controller._set_planner_feedback(
                    goal,
                    "D'Franco rejected the prior sale.",
                    failure_context={
                        "kind": "merchant_rejected_sale",
                        "required_response": "Change buyer using live evidence.",
                    },
                )
                ungrounded = with_safe_ending(
                    {
                        "summary": "Assume a different merchant will buy the sword.",
                        "steps": [
                            {
                                "id": "sell-next",
                                "outcome": "Sell the rusty sword to a weapon buyer.",
                                "tool": "sell",
                                "verification": "Carried currency increases.",
                            }
                        ],
                        "assumptions": ["A different buyer exists."],
                    },
                    100,
                )

                with self.assertRaisesRegex(
                    ModelError, "buyer-discovery step before its next sell"
                ):
                    controller._store_execution_plan(
                        goal,
                        ungrounded,
                        grounding={"valid": True},
                        revision=False,
                    )

                # A subsequent planner rejection may replace the immediate
                # feedback, but it must not erase the verified sale refusal.
                controller._clear_planner_feedback()
                legacy = controller._store_execution_plan(
                    goal,
                    ungrounded,
                    grounding={"valid": True},
                    revision=False,
                )
                controller._record_blocked_action(
                    goal,
                    broker.observe(),
                    "sell",
                    {"to": 736, "items": [7525]},
                    "D'Franco refused to buy the mace.",
                )
                controller._clear_planner_feedback()
                self.assertIsNone(controller._execution_plan(goal))
                invalidations = controller.storage.events(
                    kinds=["planner.plan.invalidated"]
                )["events"]
                self.assertTrue(invalidations)
                self.assertIn(
                    "previous merchant rejected",
                    invalidations[-1]["data"]["reason"],
                )
                self.assertEqual(legacy["goal_id"], goal["id"])
                with self.assertRaisesRegex(
                    ModelError, "buyer-discovery step before its next sell"
                ):
                    controller._store_execution_plan(
                        goal,
                        ungrounded,
                        grounding={"valid": True},
                        revision=False,
                    )

                grounded = copy.deepcopy(ungrounded)
                grounded["summary"] = "Discover a verified buyer before selling."
                grounded["steps"].insert(
                    0,
                    {
                        "id": "find-buyer",
                        "outcome": "Find merchants whose buying rule matches rusty sword.",
                        "tool": "merchants",
                        "verification": "Merchant results list a concrete buyer candidate.",
                    },
                )
                combined_lookup = copy.deepcopy(grounded)
                combined_lookup["steps"][0]["outcome"] = (
                    "Find a compatible buyer and then travel to its room."
                )
                combined_lookup["steps"][0]["verification"] = (
                    "Current room matches the discovered merchant room."
                )
                with self.assertRaisesRegex(
                    ModelError, "assigns room movement to merchants"
                ):
                    controller._store_execution_plan(
                        goal,
                        combined_lookup,
                        grounding={"valid": True},
                        revision=False,
                    )
                stored = controller._store_execution_plan(
                    goal,
                    grounded,
                    grounding={"valid": True},
                    revision=False,
                )
                self.assertEqual("merchants", stored["steps"][0]["tool"])
                self.assertIn(
                    "buyer discovery must precede the next sell",
                    PLANNER_SYSTEM,
                )

                controller.storage.emit_event(
                    "action.no_progress",
                    "Repeated buyer discovery returned known evidence",
                    goal_id=goal["id"],
                    data={
                        "tool": "merchants",
                        "arguments": {
                            "agent": "primary",
                            "buys": "rusty sword",
                        },
                        "reason": (
                            "repeated identical evidence lookup returned no new evidence"
                        ),
                        "result": {
                            "buys_anything": [
                                {"merchant": "Assassin", "room": 110}
                            ],
                            "rules_mentioning": [],
                        },
                    },
                )
                controller._clear_planner_feedback()
                self.assertIsNone(controller._execution_plan(goal))
                migration_feedback = controller._planner_feedback(goal)
                self.assertIn(
                    "omits the repeated merchants lookup",
                    migration_feedback["message"],
                )
                self.assertNotIn(
                    "with a merchants buyer-discovery step",
                    migration_feedback["message"],
                )
                controller._clear_planner_feedback()
                consumed = controller._store_execution_plan(
                    goal,
                    ungrounded,
                    grounding={"valid": True},
                    revision=True,
                )
                self.assertEqual("sell", consumed["steps"][0]["tool"])

                repeated_lookup = copy.deepcopy(grounded)
                repeated_lookup["steps"][0]["outcome"] = (
                    "Find merchants that buy rusty sword."
                )
                with self.assertRaisesRegex(
                    ModelError, "already completed with candidate results"
                ):
                    controller._store_execution_plan(
                        goal,
                        repeated_lookup,
                        grounding={"valid": True},
                        revision=True,
                    )

                refused_buyer = copy.deepcopy(ungrounded)
                refused_buyer["steps"][0]["outcome"] = (
                    "Sell the rusty sword to D'Franco merchant 736."
                )
                with self.assertRaisesRegex(
                    ModelError, "merchant 736.*already rejected"
                ):
                    controller._store_execution_plan(
                        goal,
                        refused_buyer,
                        grounding={"valid": True},
                        revision=True,
                    )
            finally:
                controller.storage.close()

    def test_tactical_context_retains_goal_blocked_actions_after_feedback_clears(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="blocked-tactic-context")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                controller.storage.set_runtime(
                    "blocked_actions",
                    [
                        {
                            "goal_id": "other-goal",
                            "tool": "sell",
                            "arguments": {"to": 1, "items": [2]},
                            "room": 10,
                            "reason": "irrelevant",
                        },
                        {
                            "goal_id": goal["id"],
                            "tool": "sell",
                            "arguments": {"to": 736, "items": [7525, 7526]},
                            "room": 106,
                            "reason": 'Pritchett tells you, "Whyfore dost you offer me that?"',
                            "suppressed_count": 1,
                        },
                    ],
                )

                context = controller._campaign_context(run, None, audience="tactical")

                self.assertEqual(
                    [
                        {
                            "tool": "sell",
                            "arguments": {"to": 736, "items": [7525, 7526]},
                            "room": 106,
                            "reason": 'Pritchett tells you, "Whyfore dost you offer me that?"',
                            "suppressed_count": 1,
                        }
                    ],
                    context["verified_no_progress_tactics"],
                )
                self.assertIn(
                    "do not restore an exact failed tool/argument/room tactic",
                    context["instructions"],
                )
            finally:
                controller.storage.close()

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

    def test_silent_room_transition_is_refreshed_and_retried_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SilentTransitionBroker(recover=True)
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="silent-movement-bounded-retry")
                )["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "travel",
                        "arguments": {"to": 54},
                        "rationale": "Travel through the verified neighbouring exit.",
                    },
                )

                self.assertEqual("travel", result["action"])
                self.assertEqual(54, broker.room["num"])
                self.assertEqual(2, broker.travel_attempts)
                self.assertEqual(
                    ["rest", "look", "travel", "look", "travel"],
                    [name for name, _ in broker.calls][-5:],
                )
                self.assertEqual(
                    1,
                    len(
                        controller.storage.events(kinds=["action.movement_retried"])[
                            "events"
                        ]
                    ),
                )
                self.assertEqual(
                    [], controller.storage.events(kinds=["action.no_progress"])["events"]
                )
                self.assertEqual([], controller.storage.goal_lessons())
                self.assertEqual([], controller.storage.get_runtime("blocked_actions", []))
            finally:
                controller.storage.close()

    def test_repeated_silent_transition_is_not_recorded_as_a_blocked_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SilentTransitionBroker(recover=False)
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="persistent-silent-movement")
                )["goal"]

                result = controller._execute(
                    goal,
                    broker.observe(),
                    {
                        "tool": "travel",
                        "arguments": {"to": 54},
                        "rationale": "Travel through the verified neighbouring exit.",
                    },
                )

                self.assertTrue(result["transient_failure"])
                self.assertFalse(result["route_disproved"])
                self.assertEqual(2, broker.travel_attempts)
                self.assertEqual(
                    1,
                    len(
                        controller.storage.events(kinds=["action.transient_failure"])[
                            "events"
                        ]
                    ),
                )
                self.assertEqual(
                    [], controller.storage.events(kinds=["action.no_progress"])["events"]
                )
                self.assertEqual([], controller.storage.goal_lessons())
                self.assertEqual([], controller.storage.get_runtime("blocked_actions", []))
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

    def test_silent_transition_route_lessons_are_resolved_after_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SilentTransitionBroker(recover=False)
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="silent-transition-lesson-repair")
                )["goal"]
                deferred = controller.learning.defer_goal(
                    goal,
                    broker.observe(),
                    tool="travel",
                    arguments={"agent": "primary", "to": 54},
                    reason=SilentTransitionBroker.SILENT_REASON,
                    classification="route_unavailable",
                    scope="tactic",
                )
                controller._record_blocked_action(
                    goal,
                    broker.observe(),
                    "travel",
                    {"agent": "primary", "to": 54},
                    SilentTransitionBroker.SILENT_REASON,
                )

                repaired = controller._repair_position_unknown_lessons()

                self.assertEqual(1, len(repaired))
                lesson = controller.storage.goal_lesson(deferred["lesson"]["id"])
                self.assertEqual("resolved", lesson["status"])
                self.assertEqual([], controller.storage.get_runtime("blocked_actions"))
            finally:
                controller.storage.close()

    def test_transient_route_repair_clears_unlocked_stagnation_and_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SilentTransitionBroker(recover=False)
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="full-transient-route-repair")
                )["goal"]
                deferred = controller.learning.defer_goal(
                    goal,
                    broker.observe(),
                    tool="autopilot",
                    arguments={
                        "action": "start",
                        "mode": "farm",
                        "hunt": "slime",
                        "assigned_room": 583,
                    },
                    reason=SilentTransitionBroker.SILENT_REASON,
                    classification="route_unavailable",
                    scope="tactic",
                    block=False,
                )
                lesson_id = deferred["lesson"]["id"]
                controller.storage.update_goal_lesson(lesson_id, "unlocked")
                key = f"{goal['id']}|583|slime"
                controller.storage.set_runtime(
                    "farm_tactic_stagnation_v1",
                    {
                        key: {
                            "goal_id": goal["id"],
                            "assigned_room": 583,
                            "target": "slime",
                            "last_error": SilentTransitionBroker.SILENT_REASON,
                        }
                    },
                )
                controller.storage.set_runtime(
                    f"background_farm_route_failure_handled_v1:{goal['id']}", True
                )
                controller.storage.set_runtime(
                    RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY,
                    {goal["id"]: {"repeat_count": 1}},
                )
                run = controller.storage.ensure_campaign_run(goal)
                controller.storage.update_campaign_memory(
                    run["id"],
                    external_blocker={
                        "kind": "no_usable_farm_recipe",
                        "status": "candidate",
                    },
                )

                repaired = controller._repair_position_unknown_lessons()

                self.assertEqual([lesson_id], [item["id"] for item in repaired])
                self.assertEqual(
                    "resolved", controller.storage.goal_lesson(lesson_id)["status"]
                )
                self.assertNotIn(
                    key,
                    controller.storage.get_runtime("farm_tactic_stagnation_v1", {}),
                )
                self.assertNotIn(
                    goal["id"],
                    controller.storage.get_runtime(
                        RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY, {}
                    ),
                )
                self.assertFalse(
                    controller.storage.get_runtime(
                        f"background_farm_route_failure_handled_v1:{goal['id']}"
                    )
                )
                self.assertIsNone(
                    controller.storage.campaign_run(goal["id"])["external_blocker"]
                )
            finally:
                controller.storage.close()

    def test_repair_requeues_only_unbacked_no_recipe_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="requeue-repaired-no-recipe-blocker")
                )["goal"]
                controller.storage.set_runtime(
                    RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY,
                    {goal["id"]: {"repeat_count": 2}},
                )
                controller.storage.block_goal(
                    goal["id"],
                    reason="all retained farm candidates were excluded",
                    blocked_reason="no_usable_farm_recipe",
                )

                controller._repair_position_unknown_lessons()
                self.assertEqual("blocked", controller.storage.goal(goal["id"])["status"])

                controller.storage.set_runtime(
                    RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY, {}
                )
                controller._repair_position_unknown_lessons()
                self.assertEqual("blocked", controller.storage.goal(goal["id"])["status"])

                controller._reconcile_blocked_farm_exhaustion(
                    SimulatedBroker().observe()
                )

                self.assertEqual("active", controller.storage.goal(goal["id"])["status"])
                events = controller.storage.goal_events(
                    goal["id"], kinds=["goal.queued", "goal.active"], limit=10
                )
                self.assertEqual(
                    ["goal.queued", "goal.active"],
                    [item["kind"] for item in events[-2:]],
                )
            finally:
                controller.storage.close()

    def test_source_overlevel_spawn_rejects_research_farm_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                broker.vitals["health"] = {"current": 35, "max": 35}
                observation = broker.observe()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="source-room-overlevel-candidate")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                controller.knowledge = SimpleNamespace(
                    corpus_version="source-risk-corpus",
                    get=lambda entity_id: {
                        "status": "found",
                        "entity": {
                            "id": entity_id,
                            "spawn_table": {
                                "spawns": [
                                    {
                                        "creature_id": "creature:ant",
                                        "creature": "ant",
                                        "level": 2,
                                        "role": "monster",
                                        "chance": 30,
                                    },
                                    {
                                        "creature_id": "creature:fungusbeast",
                                        "creature": "fungus beast",
                                        "level": 50,
                                        "role": "monster",
                                        "chance": 70,
                                    },
                                    {
                                        "creature_id": "creature:ogre",
                                        "creature": "ogre",
                                        "level": 60,
                                        "role": "monster",
                                        "chance": 5,
                                        "citation": "kod/object/active/holder/monster",
                                    },
                                ]
                            },
                        },
                    },
                )
                controller._research_farm_candidates = lambda _phase: (  # type: ignore[method-assign]
                    [
                        {
                            "room": 563,
                            "target": "ant",
                            "attempt_id": "source-risk-attempt",
                            "source": {"risk": "fungus beasts occupy the room"},
                        }
                    ],
                    [{"id": "source-risk-attempt", "result": {"for_level": 35}}],
                )

                validation = controller._research_farm_recipe_validation(
                    goal,
                    run,
                    {"id": "source-risk-phase", "kind": "research", "context": {}},
                    observation,
                )

                self.assertEqual("no_usable_candidate", validation["status"])
                blocker = validation["rejected"][0]["blocker"]
                self.assertEqual("source_room_overlevel_hostile", blocker["kind"])
                self.assertEqual(50, blocker["danger_limit"])
                self.assertEqual(["ogre"], [item["name"] for item in blocker["hostiles"]])
            finally:
                controller.storage.close()

    def test_blocked_farm_goal_reconciles_against_current_candidate_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                broker.vitals["health"] = {"current": 35, "max": 35}
                observation = broker.observe()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="reconcile-farm-exhaustion")
                )["goal"]
                controller.storage.block_goal(
                    goal["id"],
                    reason="all candidates were excluded",
                    blocked_reason="no_usable_farm_recipe",
                )
                unrelated = controller.storage.submit_goal(
                    goal_payload(request_id="unrelated-blocked-goal")
                )["goal"]
                controller.storage.block_goal(
                    unrelated["id"],
                    reason="operator dependency remains",
                    blocked_reason="operator_dependency",
                )
                controller.storage.set_runtime(
                    RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY,
                    {
                        goal["id"]: {
                            "fingerprint": "old-quarantine-fingerprint",
                            "repeat_count": 2,
                            "candidate_count": 1,
                            "rejected": [
                                {
                                    "room": 563,
                                    "target": "ant",
                                    "blocker": {
                                        "kind": "quarantined_farm_phase",
                                        "use_safe_spots": True,
                                    },
                                }
                            ],
                        }
                    },
                )

                hazardous = True

                def get_room(entity_id: str) -> dict[str, object]:
                    spawns = (
                        [
                            {
                                "creature_id": "creature:fungusbeast",
                                "creature": "fungus beast",
                                "level": 60,
                                "role": "monster",
                            }
                        ]
                        if hazardous
                        else []
                    )
                    return {
                        "status": "found",
                        "entity": {"id": entity_id, "spawn_table": {"spawns": spawns}},
                    }

                controller.knowledge = SimpleNamespace(
                    corpus_version="reconcile-corpus", get=get_room
                )

                first = controller._reconcile_blocked_farm_exhaustion(observation)

                self.assertEqual("still_blocked", first[0]["status"])
                self.assertEqual("blocked", controller.storage.goal(goal["id"])["status"])
                refreshed = controller.storage.get_runtime(
                    RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY, {}
                )[goal["id"]]
                self.assertEqual(
                    "source_room_overlevel_hostile",
                    refreshed["rejected"][0]["blocker"]["kind"],
                )

                hazardous = False
                second = controller._reconcile_blocked_farm_exhaustion(observation)

                self.assertEqual("requeued", second[0]["status"])
                self.assertEqual("active", controller.storage.goal(goal["id"])["status"])
                self.assertEqual(
                    "blocked", controller.storage.goal(unrelated["id"])["status"]
                )
                self.assertNotIn(
                    goal["id"],
                    controller.storage.get_runtime(
                        RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY, {}
                    ),
                )
            finally:
                controller.storage.close()

    def test_legacy_controller_blocks_are_requeued_and_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="repair-controller-block")
                )["goal"]
                controller.storage.block_goal(
                    goal["id"],
                    reason="legacy tactical conclusion",
                    blocked_reason="prerequisite_not_met",
                )

                repaired = controller._repair_controller_goal_blocks()

                self.assertEqual([goal["id"]], [item["id"] for item in repaired])
                self.assertEqual(
                    "active", controller.storage.goal(goal["id"])["status"]
                )
                self.assertEqual([], controller.storage.goals(["blocked"]))
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
                stop = next(
                    arguments
                    for name, arguments in broker.calls
                    if name == "autopilot" and arguments.get("action") == "stop"
                )
                self.assertTrue(stop["hard"])
            finally:
                controller.storage.close()

    def test_waiting_survival_keeper_above_rest_floor_yields_control(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_mode = "survive"
                broker.farm_activity = "waiting"
                broker.vitals["health"] = {"value": 25, "max": 28}
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="quiescent-survival-yields")
                )["goal"]

                result = controller._manage_background_farm(
                    goal, broker.observe(), {"all_met": False}
                )

                self.assertTrue(result["background_keeper_stopping"])
                stop = next(
                    arguments
                    for name, arguments in broker.calls
                    if name == "autopilot" and arguments.get("action") == "stop"
                )
                self.assertTrue(stop["hard"])
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
                self.assertEqual(41, blocker["hostiles"][0]["danger_limit"])
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
                self.assertEqual(
                    {
                        "mode": "any",
                        "conditions": [
                            {
                                "kind": "numeric_at_least",
                                "field": "max_health",
                                "value": 35,
                            },
                            {"kind": "corpus_changed", "from": "test-corpus"},
                        ],
                    },
                    lesson["retry_when"],
                )
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

    def test_legacy_equipment_hash_change_cannot_release_survivability_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                observation = SimulatedBroker().observe()
                observation["status"]["vitals"]["health"] = {
                    "current": 34,
                    "max": 34,
                }
                observation["look"]["vitals"]["health"] = {
                    "current": 34,
                    "max": 34,
                }
                controller.storage.set_runtime(
                    "combat_outcomes_v1",
                    [
                        {
                            "occurred_at": "2026-08-04T22:20:00.000Z",
                            "room": {"id": 584, "name": "The Flatlands"},
                            "target": "ant",
                            "health_after": {"max": 35, "value": 4},
                            "equipment_hash": "short-sword-equipped",
                        }
                    ],
                )
                controller.storage.set_runtime(
                    "farm_tactic_quarantine_v1",
                    {
                        "584": {
                            "room": 584,
                            "assigned_room": 584,
                            "target": "ant",
                            "quarantined_at": "2026-08-04T22:22:00.000Z",
                            "reasons": ["repeated retreat episodes reached the safety limit"],
                        }
                    },
                )

                released = controller._repair_capability_unlocked_farm_quarantines(
                    observation
                )

                self.assertEqual([], released)
                self.assertIn(
                    "584",
                    controller.storage.get_runtime(
                        "farm_tactic_quarantine_v1", {}
                    ),
                )
            finally:
                controller.storage.close()

    def test_threshold_only_quarantine_releases_at_old_or_current_policy(self) -> None:
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
                        "554": {
                            "room": 554,
                            "target": "centipede",
                            "flee_threshold": 0.6,
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

                self.assertCountEqual([557, 554], [item["room"] for item in released])
                remaining = controller.storage.get_runtime(
                    "farm_tactic_quarantine_v1", {}
                )
                self.assertNotIn("557", remaining)
                self.assertNotIn("554", remaining)
                self.assertIn("535", remaining)
                self.assertIn("575", remaining)
                former_policy = next(
                    item for item in released if item["room"] == 557
                )
                self.assertEqual(0.8, former_policy["prior_flee_threshold"])
                self.assertEqual(0.6, former_policy["current_flee_threshold"])
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

    def test_manual_fight_uses_margin_above_disengage_instead_of_full_health(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="manual-combat-start-margin")
                )["goal"]
                broker = CombatBroker()
                observation = broker.observe()
                observation["status"]["vitals"]["health"] = {
                    "current": 85,
                    "max": 100,
                }
                observation["look"]["vitals"]["health"] = {
                    "current": 85,
                    "max": 100,
                }

                manual = controller._combat_preflight(
                    "fight",
                    {"target": "giant rat", "disengage_at": 0.7},
                    observation,
                    goal,
                )
                autonomous = controller._combat_preflight(
                    "autopilot",
                    {"action": "start", "mode": "farm", "hunt": "giant rat"},
                    observation,
                    goal,
                )

                self.assertNotIn("recover_health", {item["kind"] for item in manual})
                self.assertIn("recover_health", {item["kind"] for item in autonomous})

                observation["status"]["vitals"]["health"]["current"] = 79
                observation["look"]["vitals"]["health"]["current"] = 79
                low = controller._combat_preflight(
                    "fight",
                    {"target": "giant rat", "disengage_at": 0.7},
                    observation,
                    goal,
                )
                blocker = next(item for item in low if item["kind"] == "recover_health")
                self.assertAlmostEqual(0.8, blocker["minimum_start_fraction"])
                self.assertIn("one-swing fight", blocker["guidance"])
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

    def test_two_unauthorized_plan_revisions_force_bounded_replanning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                controller.broker = SimulatedBroker()
                controller.model = InvalidRevisionModel()  # type: ignore[assignment]
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="bound-unauthorized-revisions")
                )["goal"]
                controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {
                            "summary": "Drop the requested item.",
                            "steps": [
                                {
                                    "id": "drop-item",
                                    "outcome": "Drop the requested item.",
                                    "tool": "act",
                                    "verification": "The item is absent from inventory.",
                                }
                            ],
                        },
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )

                first = controller.turn()
                second = controller.turn()

                self.assertTrue(first["plan_rejected"])
                self.assertFalse(first["plan_invalidated"])
                self.assertTrue(second["plan_rejected"])
                self.assertTrue(second["plan_invalidated"])
                self.assertIsNone(controller._execution_plan(goal))
            finally:
                controller.storage.close()

    def test_fresh_action_issues_revision_authorization_and_accepts_echo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                controller.broker = SimulatedBroker()
                controller.model = AuthorizedRevisionModel()  # type: ignore[assignment]
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="authorized-plan-revision")
                )["goal"]
                original = controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {
                            "summary": "Inspect then complete the bounded goal.",
                            "steps": [
                                {
                                    "id": "read-inventory",
                                    "outcome": "Read the current inventory.",
                                    "tool": "inventory",
                                    "verification": "Inventory contents are reported.",
                                }
                            ],
                        },
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )
                controller._record_plan_action(
                    goal,
                    step_id="read-inventory",
                    tool="inventory",
                    arguments={"scope": "all"},
                    result={"items": []},
                )

                recorded = controller._execution_plan(goal)
                authorization = controller._plan_revision_authorization(
                    goal, recorded, None
                )
                self.assertEqual(
                    {"scope": "all"},
                    authorization["source"]["arguments"],
                )

                result = controller.turn()

                self.assertTrue(result["planned"])
                self.assertNotEqual(
                    original["summary"], result["execution_plan"]["summary"]
                )
                self.assertIsNone(result["execution_plan"]["last_action"])
                self.assertIsNone(controller._planner_feedback(goal))
                self.assertIn(
                    "exact prior tool arguments", PLANNER_SYSTEM
                )
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

    def test_invalid_planner_actions_replan_without_model_degradation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                simulator = SimulatedBroker()
                controller.broker = simulator
                invalid = {
                    "decision": "act",
                    "tool": "act",
                    "arguments": {"verb": "look"},
                    "rationale": "Confirm the current room.",
                    "expected_observation": {"room": "known"},
                    "proposal": None,
                    "plan_step_id": "drop-item",
                }
                controller.model = DecisionSequenceModel(
                    [invalid, invalid]
                )  # type: ignore[assignment]
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="bounded-invalid-planner-actions")
                )["goal"]
                controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {
                            "summary": "Drop the requested item.",
                            "steps": [
                                {
                                    "id": "drop-item",
                                    "outcome": "Drop the requested item.",
                                    "tool": "act",
                                    "verification": "The item is absent from inventory.",
                                }
                            ],
                        },
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )

                first = controller.turn()
                second = controller.turn()

                self.assertIn("planner_action_rejected", first, first)
                self.assertTrue(first["planner_action_rejected"], first)
                self.assertIn("consecutive_invalid_actions", first, first)
                self.assertEqual(1, first["consecutive_invalid_actions"], first)
                self.assertFalse(first["plan_invalidated"])
                self.assertTrue(second["planner_action_rejected"])
                self.assertEqual(2, second["consecutive_invalid_actions"])
                self.assertTrue(second["plan_invalidated"])
                self.assertIsNone(controller._execution_plan(goal))
                self.assertEqual("healthy", controller.dependencies["model"])
                self.assertFalse(
                    any(name == "act" for name, _ in simulator.calls)
                )
            finally:
                controller.storage.close()

    def test_valid_action_resets_invalid_action_strikes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                source_verify_safe_rooms(controller, 100)
                simulator = SimulatedBroker()
                controller.broker = simulator
                invalid = {
                    "decision": "act",
                    "tool": "act",
                    "arguments": {"verb": "look"},
                    "rationale": "Use an invalid observation verb.",
                    "expected_observation": {},
                    "proposal": None,
                    "plan_step_id": "drop-item",
                }
                valid = {
                    "decision": "act",
                    "tool": "inventory",
                    "arguments": {},
                    "rationale": "Read the inventory with its proper tool.",
                    "expected_observation": {"inventory": "reported"},
                    "proposal": None,
                    "plan_step_id": "read-inventory",
                }
                controller.model = DecisionSequenceModel(
                    [invalid, valid, invalid]
                )  # type: ignore[assignment]
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="reset-invalid-action-strikes")
                )["goal"]
                controller._store_execution_plan(
                    goal,
                    with_safe_ending(
                        {
                            "summary": "Inspect inventory and drop the requested item.",
                            "steps": [
                                {
                                    "id": "read-inventory",
                                    "outcome": "Read the carried inventory.",
                                    "tool": "inventory",
                                    "verification": "Inventory contents are reported.",
                                },
                                {
                                    "id": "drop-item",
                                    "outcome": "Drop the requested item.",
                                    "tool": "act",
                                    "verification": "The item is absent from inventory.",
                                },
                            ],
                        },
                        100,
                    ),
                    grounding=controller.knowledge.validate_goal(goal),
                    revision=False,
                )

                first = controller.turn()
                middle = controller.turn()
                third = controller.turn()

                self.assertEqual(1, first["consecutive_invalid_actions"])
                self.assertEqual("inventory", middle["action"])
                self.assertEqual(1, third["consecutive_invalid_actions"])
                self.assertFalse(third["plan_invalidated"])
                self.assertIsNotNone(controller._execution_plan(goal))
            finally:
                controller.storage.close()

    def test_malformed_farm_contract_pauses_without_poisoning_goal_family(self) -> None:
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
                self.assertTrue(result["strategic_goal_preserved"])
                self.assertEqual("paused", controller.storage.goal(submitted["id"])["status"])
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
                stop = next(
                    arguments
                    for name, arguments in broker.calls
                    if name == "autopilot" and arguments.get("action") == "stop"
                )
                self.assertTrue(stop["hard"])
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
                        "what": "could not get back to the assigned room",
                        "going_to": 557,
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

    def test_running_farm_exact_route_failure_stops_and_pauses_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_room = 557
                broker.farm_hunt = "groundworm larva"
                broker.room = {"num": 568, "name": "Lake of Jala's Song"}
                broker.farm_placement = {
                    "assigned_room": 557,
                    "standing_where_assigned": False,
                    "failed": 3,
                    "why_not": [
                        {"room": 557, "why": "no route from 568 to 557 in the graph"}
                    ],
                }
                broker.farm_journal = [
                    {
                        "pass": 12,
                        "what": "could not get back to the assigned room",
                        "going_to": 557,
                        "why": "no route from 568 to 557 in the graph",
                    }
                ]
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="running-failed-farm-route",
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
                controller.storage.set_runtime(
                    "background_farm_owner_v1",
                    {
                        "goal_id": goal["id"],
                        "assigned_room": 557,
                        "hunt": "groundworm larva",
                        "origin_room": 568,
                    },
                )
                controller.storage.set_runtime(
                    f"background_farm_snapshot_v2:{goal['id']}",
                    {"pass_floor": 10},
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
                self.assertFalse(broker.farm_running)
                self.assertTrue(
                    any(
                        name == "autopilot" and arguments.get("action") == "stop"
                        for name, arguments in broker.calls
                    )
                )
                self.assertEqual("route_unavailable", result["lesson"]["classification"])
            finally:
                controller.storage.close()

    def test_failed_retreat_to_inn_is_not_an_assignment_route_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_room = 586
                broker.farm_hunt = "giant rat"
                broker.room = {"num": 150, "name": "Cor Noth"}
                broker.farm_did = {
                    "kills": 12,
                    "deaths": 0,
                    "withdrawals": 0,
                }
                broker.farm_placement = {
                    "assigned_room": 586,
                    "standing_where_assigned": False,
                    "failed": 0,
                    "why_not": [],
                    "relocations": 5,
                    "returned_to_assignment": 5,
                }
                broker.farm_journal = [
                    {
                        "pass": 157,
                        "room": 586,
                        "what": "clearing the room so it can spawn again",
                    },
                    {
                        "pass": 158,
                        "room": 153,
                        "what": "could not reach any inn - falling back to a local wall",
                        "why": "the safety retreat could not finish",
                    },
                ]
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="retreat-is-not-assignment-route-failure",
                        objective="Raise max HP to 100.",
                        success_criteria=[
                            {
                                "id": "max-hp-100",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 100,
                            }
                        ],
                        constraints={
                            "operator_notes": (
                                "hunt=giant rat; assigned_room=586; "
                                "use_safe_spots=true"
                            )
                        },
                    )
                )["goal"]
                controller.storage.set_runtime(
                    "background_farm_owner_v1",
                    {
                        "goal_id": goal["id"],
                        "assigned_room": 586,
                        "hunt": "giant rat",
                        "origin_room": 52,
                    },
                )
                controller.storage.set_runtime(
                    f"background_farm_snapshot_v2:{goal['id']}",
                    {"pass_floor": 150},
                )
                status = broker.call_tool(
                    "autopilot", {"agent": "primary", "action": "status"}
                )

                failure = controller._stopped_farm_route_failure(
                    goal, broker.observe(), status
                )

                self.assertIsNone(failure)
                self.assertEqual(
                    [],
                    controller._farm_assignment_route_failure_records(
                        status, assigned_room=586, minimum_pass=150
                    ),
                )
            finally:
                controller.storage.close()

    def test_transient_keeper_reply_loss_is_not_promoted_after_stall_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_room = 583
                broker.farm_hunt = "slime"
                broker.room = {"num": 562, "name": "The Lowlands"}
                broker.farm_stalled = {
                    "idle_passes": 7,
                    "since_seconds": 32,
                    "why": "room capped by creatures we will not fight",
                }
                broker.farm_placement = {
                    "assigned_room": 583,
                    "standing_where_assigned": False,
                    "failed": 1,
                    "why_not": [
                        {
                            "room": 583,
                            "why": SilentTransitionBroker.SILENT_REASON,
                        }
                    ],
                }
                broker.farm_journal = [
                    {
                        "pass": 22,
                        "what": "could not get back to the assigned room",
                        "going_to": 583,
                        "why": SilentTransitionBroker.SILENT_REASON,
                    }
                ]
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="transient-keeper-route-after-stall",
                        objective="Raise max HP to 100.",
                        success_criteria=[
                            {
                                "id": "max-hp-100",
                                "kind": "numeric_threshold",
                                "metric": "status.vitals.health.max",
                                "operator": ">=",
                                "value": 100,
                            }
                        ],
                        constraints={
                            "operator_notes": "hunt=slime; assigned_room=583"
                        },
                    )
                )["goal"]
                controller.storage.set_runtime(
                    "background_farm_owner_v1",
                    {
                        "goal_id": goal["id"],
                        "assigned_room": 583,
                        "hunt": "slime",
                        "origin_room": 562,
                    },
                )
                controller.storage.set_runtime(
                    f"background_farm_snapshot_v2:{goal['id']}",
                    {
                        "pass_floor": 20,
                        "counters": dict(broker.farm_did),
                        "launch_counters": dict(broker.farm_did),
                        "origin_room": 562,
                    },
                )
                observation = broker.observe()

                stopped = controller._manage_background_farm(
                    goal,
                    observation,
                    controller.criteria.evaluate(goal, observation),
                )
                inert_turn = controller._manage_background_farm(
                    goal,
                    observation,
                    controller.criteria.evaluate(goal, observation),
                )

                self.assertTrue(stopped["background_farm_stopped"])
                self.assertIsNone(inert_turn)
                self.assertTrue(
                    controller.storage.get_runtime(
                        f"background_farm_route_failure_handled_v1:{goal['id']}"
                    )
                )
                stagnations = controller.storage.get_runtime(
                    "farm_tactic_stagnation_v1", {}
                )
                self.assertIn(f"{goal['id']}|562|slime", stagnations)
                self.assertNotIn(f"{goal['id']}|583|slime", stagnations)
                self.assertEqual([], controller.storage.goal_lessons(goal_id=goal["id"]))
            finally:
                controller.storage.close()

    def test_successful_assignment_returns_repair_false_route_stagnation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.room = {"num": 153, "name": "Cibilo Creek Inn"}
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="repair-false-assignment-route-failure",
                        objective="Raise max HP to 100.",
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
                lesson = controller.learning.defer_goal(
                    goal,
                    broker.observe(),
                    tool="autopilot",
                    arguments={
                        "action": "start",
                        "mode": "farm",
                        "hunt": "giant rat",
                        "assigned_room": 586,
                    },
                    reason=(
                        "The keeper could not route from room 52 to "
                        "assigned_room=586: keeper route placement failed"
                    ),
                    classification="route_unavailable",
                    scope="tactic",
                    block=False,
                )["lesson"]
                key = f"{goal['id']}|586|giant rat"
                record = {
                    "goal_id": goal["id"],
                    "room": 150,
                    "assigned_room": 586,
                    "requested_assigned_room": 586,
                    "stalled_in_transit": True,
                    "target": "giant rat",
                    "last_error": "keeper route placement failed",
                    "placement": {
                        "assigned_room": 586,
                        "failed": 0,
                        "why_not": [],
                        "returned_to_assignment": 5,
                    },
                }
                controller.storage.set_runtime(
                    "farm_tactic_stagnation_v1", {key: record}
                )
                controller.storage.set_runtime(
                    f"background_farm_route_failure_handled_v1:{goal['id']}",
                    True,
                )

                repaired = controller._repair_disproved_farm_route_stagnations()

                self.assertEqual([record], repaired)
                self.assertEqual(
                    {},
                    controller.storage.get_runtime("farm_tactic_stagnation_v1", {}),
                )
                self.assertEqual(
                    "resolved",
                    controller.storage.goal_lesson(lesson["id"])["status"],
                )
                self.assertFalse(
                    controller.storage.get_runtime(
                        f"background_farm_route_failure_handled_v1:{goal['id']}"
                    )
                )
                self.assertFalse(controller._farm_stagnation_blocks(record))
            finally:
                controller.storage.close()

    def test_inert_farm_preserves_exact_route_failure_for_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_room = 557
                broker.farm_hunt = "groundworm larva"
                broker.farm_inert = {"inert": True, "why": "asked to stop"}
                broker.room = {"num": 568, "name": "Lake of Jala's Song"}
                broker.farm_placement = {
                    "assigned_room": 557,
                    "standing_where_assigned": False,
                    "failed": 2,
                    "why_not": [
                        {"room": 557, "why": "no route from 568 to 557 in the graph"}
                    ],
                }
                broker.farm_journal = [
                    {
                        "pass": 22,
                        "what": "could not get back to the assigned room",
                        "going_to": 557,
                        "why": "no route from 568 to 557 in the graph",
                    }
                ]
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="inert-failed-farm-route",
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
                controller.storage.set_runtime(
                    "background_farm_owner_v1",
                    {
                        "goal_id": goal["id"],
                        "assigned_room": 557,
                        "hunt": "groundworm larva",
                        "origin_room": 568,
                    },
                )
                controller.storage.set_runtime(
                    f"background_farm_snapshot_v2:{goal['id']}",
                    {"pass_floor": 20},
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
                # It was already inert, so route diagnosis must not issue a
                # redundant second stop.
                self.assertFalse(
                    any(
                        name == "autopilot" and arguments.get("action") == "stop"
                        for name, arguments in broker.calls
                    )
                )
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
                phase_blocker = controller._campaign_phase_grounding_blocker(
                    {
                        "kind": "farm",
                        "context": {
                            "room": 575,
                            "target": "giant rat",
                            "use_safe_spots": True,
                        },
                    },
                    broker.observe(),
                    goal_id=goal["id"],
                )
                self.assertEqual("stagnated_farm_phase", phase_blocker["kind"])

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

    def test_stagnation_uses_launch_deltas_not_lifetime_deaths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_room = 586
                broker.room = {"num": 586, "name": "The Sweet Grass Prairies"}
                broker.farm_did = {
                    "kills": 102,
                    "deaths": 1,
                    "withdrawals": 0,
                }
                broker.farm_stalled = {
                    "idle_passes": 7,
                    "since_seconds": 32,
                    "why": "room capped by creatures we will not fight",
                }
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="launch-scoped-stagnation")
                )["goal"]
                baseline = {
                    "kills": 100,
                    "deaths": 1,
                    "withdrawals": 0,
                    "mulligans": 0,
                    "logoffs": 0,
                    "deaths_in_safe_spot": 0,
                    "deaths_in_proven_safe_spot": 0,
                }
                controller.storage.set_runtime(
                    f"background_farm_snapshot_v2:{goal['id']}",
                    {
                        "counters": baseline,
                        "launch_counters": baseline,
                        "pass_floor": 0,
                    },
                )

                stopped = controller._manage_background_farm(
                    goal,
                    broker.observe(),
                    controller.criteria.evaluate(goal, broker.observe()),
                )

                self.assertTrue(stopped["background_farm_stopped"])
                key = f"{goal['id']}|586|giant rat"
                stagnations = controller.storage.get_runtime(
                    "farm_tactic_stagnation_v1", {}
                )
                record = stagnations[key]
                self.assertEqual(2, record["deltas"]["kills"])
                self.assertEqual(0, record["deltas"]["deaths"])
                self.assertEqual("launch", record["evidence_scope"])
                record["recorded_at"] = "2000-01-01T00:00:00.000Z"
                self.assertFalse(controller._farm_stagnation_blocks(record))
                # Legacy records lack launch deltas. Their raw cumulative
                # ``did.deaths`` must not create a permanent safety verdict.
                legacy = {
                    **record,
                    "deltas": {},
                    "did": {"kills": 102, "deaths": 1},
                }
                self.assertFalse(controller._farm_stagnation_blocks(legacy))
            finally:
                controller.storage.close()

    def test_new_background_farm_stall_gets_a_recovery_grace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_stalled = {
                    "idle_passes": 5,
                    "since_seconds": 2,
                    "why": "broke off without a kill",
                }
                broker.farm_did = {"kills": 12, "deaths": 0, "withdrawals": 0}
                broker.farm_room = 575
                broker.farm_flee_below = 0.6
                broker.room = {"num": 575, "name": "The King's Way"}
                controller.broker = broker
                controller.model = FixedModel()  # type: ignore[assignment]
                payload = goal_payload(request_id="transient-farm-stall")
                payload["success_criteria"] = [
                    {
                        "id": "hp-101",
                        "kind": "numeric_threshold",
                        "metric": "status.vitals.health.max",
                        "operator": ">=",
                        "value": 101,
                    }
                ]
                goal = controller.storage.submit_goal(
                    payload
                )["goal"]

                observation = broker.observe()
                result = controller._manage_background_farm(
                    goal,
                    observation,
                    controller.criteria.evaluate(goal, observation),
                )

                self.assertTrue(result["background_farm_monitoring"])
                self.assertFalse(
                    any(
                        name == "autopilot" and args.get("action") == "stop"
                        for name, args in broker.calls
                    )
                )
                self.assertEqual(
                    {},
                    controller.storage.get_runtime("farm_tactic_stagnation_v1", {}),
                )
                self.assertEqual("active", controller.storage.goal(goal["id"])["status"])
            finally:
                controller.storage.close()

    def test_transient_productive_stagnation_is_repaired_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="repair-transient-stagnation")
                )["goal"]
                key = f"{goal['id']}|1016|mummy"
                record = {
                    "goal_id": goal["id"],
                    "room": 1016,
                    "assigned_room": 1016,
                    "requested_assigned_room": 1016,
                    "stalled_in_transit": False,
                    "target": "mummy",
                    "count": 1,
                    "recorded_at": "2000-01-01T00:00:00.000Z",
                    "stalled": {
                        "idle_passes": 6,
                        "since_seconds": 2,
                        "why": "broke off without a kill",
                    },
                    "last_error": None,
                    "did": {"kills": 34, "deaths": 0},
                }
                controller.storage.set_runtime(
                    "farm_tactic_stagnation_v1", {key: record}
                )

                repaired = controller._repair_transient_farm_stagnations()

                self.assertEqual([record], repaired)
                self.assertEqual(
                    {},
                    controller.storage.get_runtime("farm_tactic_stagnation_v1", {}),
                )
                self.assertFalse(controller._farm_stagnation_blocks(record))
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

    def test_distinct_survival_incidents_never_block_strategic_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_running = False
                broker.vitals["health"] = {"current": 20, "max": 100}
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="survival-incidents-preserve-goal")
                )["goal"]

                for _ in range(
                    controller.config.learning.survival_interrupt_budget + 1
                ):
                    controller.storage.set_runtime("survival_incident_v1", None)
                    result = controller.turn()
                    self.assertTrue(result["survival_interrupt"])
                    self.assertFalse(result.get("goal_blocked", False))

                self.assertEqual(
                    "active", controller.storage.goal(goal["id"])["status"]
                )
                events = controller.storage.goal_events(
                    goal["id"], kinds=["survival.interrupt"], limit=20
                )
                self.assertGreaterEqual(
                    len(events),
                    controller.config.learning.survival_interrupt_budget,
                )
            finally:
                controller.storage.close()

    def test_single_farm_flee_threshold_crossing_recovers_without_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_room = 535
                broker.room = {"num": 535, "name": "West Merchant Way through Ilerian Woods"}
                broker.vitals["health"] = {"current": 19, "max": 26}
                broker.farm_did.update({"kills": 4, "withdrawals": 0})
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

                self.assertTrue(result["background_farm_recovering"])
                self.assertEqual(1, result["retreat_incident_count"])
                self.assertEqual("farm", broker.farm_mode)
                self.assertEqual("active", controller.storage.goal(goal["id"])["status"])
                self.assertEqual(
                    {}, controller.storage.get_runtime("farm_tactic_quarantine_v1", {})
                )
                self.assertEqual(4, controller.learning.combat_summary()["by_target"][0]["kills"])
                self.assertEqual(
                    [],
                    controller.storage.goal_lessons(
                        statuses=["deferred"], goal_id=goal["id"]
                    ),
                )
            finally:
                controller.storage.close()

    def test_repeated_farm_retreats_quarantine_exact_tactic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = BackgroundFarmBroker()
                broker.farm_room = 535
                broker.room = {
                    "num": 535,
                    "name": "West Merchant Way through Ilerian Woods",
                }
                broker.vitals["health"] = {"current": 19, "max": 26}
                controller.broker = broker
                goal = controller.storage.submit_goal(
                    goal_payload(
                        request_id="repeated-keeper-retreat",
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

                first = controller.turn()
                broker.vitals["health"] = {"current": 26, "max": 26}
                controller.turn()
                broker.vitals["health"] = {"current": 19, "max": 26}
                broker.farm_did["withdrawals"] = 1
                repeated = controller.turn()

                self.assertTrue(first["background_farm_recovering"])
                self.assertTrue(repeated["switched_to_survival"])
                self.assertEqual("survive", broker.farm_mode)
                quarantine = controller.storage.get_runtime(
                    "farm_tactic_quarantine_v1", {}
                )
                self.assertIn("535", quarantine)
                self.assertIn(
                    "repeated retreat episodes",
                    quarantine["535"]["reasons"][0],
                )
                lesson = controller.storage.goal_lessons(
                    statuses=["deferred"], goal_id=goal["id"]
                )[0]
                self.assertEqual("ineffective_tactic", lesson["classification"])
                self.assertEqual("tactic", lesson["scope"])
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

                first = controller.turn()
                broker.farm_did["withdrawals"] = 2
                result = controller.turn()

                self.assertTrue(first["background_farm_recovering"])
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

    def test_single_transit_retreat_recovers_without_lesson_or_quarantine(self) -> None:
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

                self.assertTrue(result["background_farm_recovering"])
                quarantine = controller.storage.get_runtime("farm_tactic_quarantine_v1", {})
                self.assertNotIn("575", quarantine)
                self.assertEqual(
                    [],
                    controller.storage.goal_lessons(
                        statuses=["deferred"], goal_id=goal["id"]
                    ),
                )
                self.assertNotIn(
                    "background_farm.survival_handoff",
                    {
                        event["kind"]
                        for event in controller.storage.goal_events(
                            goal["id"], limit=20
                        )
                    },
                )
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
                self.assertFalse(result["goal_blocked"])
                self.assertTrue(result["strategic_goal_preserved"])
                self.assertEqual("active", controller.storage.goal(goal["id"])["status"])
                lessons = controller.storage.goal_lessons(
                    statuses=["deferred"], limit=20
                )
                self.assertEqual("tactic", lessons[0]["scope"])
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

    def test_campaign_tactic_ledger_deduplicates_research_rejections(self) -> None:
        rejected = {
            "room": 583,
            "target": "slime",
            "blocker": {
                "kind": "quarantined_farm_phase",
                "evidence": {
                    "reasons": ["repeated safe-spot failures"],
                    "large_raw_snapshot": "x" * 20_000,
                },
            },
        }
        history = [
            {
                "id": f"research-{index}",
                "kind": "research_progression",
                "status": "failed",
                "context": {
                    "recipe_validation": {
                        "status": "no_usable_candidate",
                        "fingerprint": "same-candidate-set",
                        "candidate_count": 1,
                        "rejected": [copy.deepcopy(rejected)],
                    }
                },
            }
            for index in range(8)
        ]

        projected = CampaignCoordinator._tactic_ledger(history, None)

        self.assertEqual(1, len(projected["unique_rejected_candidates"]))
        self.assertEqual(1, len(projected["research_candidate_sets"]))
        self.assertNotIn("large_raw_snapshot", repr(projected))

    def test_research_retry_gate_requires_material_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                observation = broker.observe()
                observation["equipment"] = {
                    "known": True,
                    "equipped": [{"id": 1, "name": "mace"}],
                    "wielding": ["mace"],
                    "fresh_ms": 100,
                    "changed_ms": 500,
                }
                observation["abilities"] = {
                    "skills": [{"id": 2, "name": "mace fighting", "ability": 30}],
                    "spells": [],
                    "freshness": {"age_ms": {"skills": 100, "spells": 200}},
                }
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="research-retry-state")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                baseline = controller._research_retry_state(goal, observation)
                record = {
                    "fingerprint": "same-candidate-set",
                    "repeat_count": 3,
                    "phase_ids": ["one", "two", "three"],
                    "candidate_count": 1,
                    "rejected": [],
                    "retry_state_schema": RESEARCH_RETRY_STATE_SCHEMA_VERSION,
                    "retry_state": baseline,
                    "retry_state_fingerprint": controller._research_retry_state_fingerprint(
                        goal, observation
                    ),
                }
                controller.storage.set_runtime(
                    RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY,
                    {goal["id"]: record},
                )
                controller.storage.update_campaign_memory(
                    run["id"],
                    external_blocker={"kind": "no_usable_farm_recipe", **record},
                )

                unchanged = controller._research_retry_gate(goal, observation)
                self.assertFalse(unchanged["allowed"])
                stored = controller.storage.get_runtime(
                    RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY, {}
                )[goal["id"]]
                self.assertTrue(stored["retry_state_fingerprint"])

                volatile_only = copy.deepcopy(observation)
                volatile_only["equipment"]["fresh_ms"] = 900
                volatile_only["equipment"]["changed_ms"] = 1300
                volatile_only["abilities"]["freshness"]["age_ms"] = {
                    "skills": 900,
                    "spells": 1000,
                }
                self.assertEqual(
                    controller._research_retry_state_fingerprint(goal, observation),
                    controller._research_retry_state_fingerprint(goal, volatile_only),
                )

                changed = copy.deepcopy(observation)
                changed["inventory"]["items"].append(
                    {"id": 2, "name": "leather armor", "amount": 1}
                )
                reopened = controller._research_retry_gate(goal, changed)

                self.assertTrue(reopened["allowed"])
                self.assertTrue(reopened["state_changed"])
                retained = controller.storage.get_runtime(
                    RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY, {}
                )[goal["id"]]
                self.assertEqual(3, retained["repeat_count"])
                self.assertEqual(
                    controller._research_retry_state_fingerprint(goal, observation),
                    retained["retry_state_fingerprint"],
                )
                self.assertEqual(
                    controller._research_retry_state_fingerprint(goal, changed),
                    retained["retry_pending_state_fingerprint"],
                )
                self.assertIsNone(
                    controller.storage.campaign_run(goal["id"])["external_blocker"]
                )
                self.assertTrue(
                    controller._research_retry_gate(goal, changed)["allowed"]
                )

                repeated = controller._record_research_recipe_exhaustion(
                    goal,
                    run,
                    {"id": "four"},
                    {
                        "fingerprint": "same-candidate-set",
                        "candidate_count": 1,
                        "rejected": [],
                    },
                    changed,
                )
                self.assertEqual(4, repeated["repeat_count"])
                self.assertFalse(
                    controller._research_retry_gate(goal, changed)["allowed"]
                )
            finally:
                controller.storage.close()

    def test_legacy_research_exhaustion_gets_one_bounded_migration_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                observation = SimulatedBroker().observe()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="legacy-research-retry-migration")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                record = {
                    "fingerprint": "legacy-candidate-set",
                    "repeat_count": 2,
                    "phase_ids": ["one", "two"],
                    "candidate_count": 0,
                    "rejected": [],
                }
                controller.storage.set_runtime(
                    RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY,
                    {goal["id"]: record},
                )
                controller.storage.update_campaign_memory(
                    run["id"],
                    external_blocker={"kind": "no_usable_farm_recipe", **record},
                )

                migrated = controller._research_retry_gate(goal, observation)
                still_pending = controller._research_retry_gate(goal, observation)

                consumed = controller._record_research_recipe_exhaustion(
                    goal,
                    run,
                    {"id": "three"},
                    {
                        "status": "no_usable_candidate",
                        "candidate_count": 0,
                        "rejected": [],
                    },
                    observation,
                )
                repeated = controller._research_retry_gate(goal, observation)

                self.assertTrue(migrated["allowed"])
                self.assertTrue(migrated["migration_retry"])
                self.assertTrue(still_pending["allowed"])
                self.assertTrue(still_pending["migration_retry"])
                self.assertFalse(repeated["allowed"])
                self.assertEqual(3, consumed["repeat_count"])
                stored = controller.storage.get_runtime(
                    RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY, {}
                )[goal["id"]]
                self.assertEqual(
                    RESEARCH_RETRY_STATE_SCHEMA_VERSION,
                    stored["retry_state_schema"],
                )
                self.assertNotIn("retry_migration_pending_at", stored)
            finally:
                controller.storage.close()

    def test_semantically_identical_empty_research_counts_across_raw_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                observation = SimulatedBroker().observe()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="semantic-research-exhaustion")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                controller.storage.set_runtime(
                    RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY,
                    {
                        goal["id"]: {
                            # Legacy records persisted the raw-result hash and
                            # did not carry a canonical semantic identity.
                            "fingerprint": "raw-result-and-avoid-set-one",
                            "repeat_count": 1,
                            "phase_ids": ["research-one"],
                            "candidate_count": 0,
                            "rejected": [],
                        }
                    },
                )
                second = controller._record_research_recipe_exhaustion(
                    goal,
                    run,
                    {"id": "research-two"},
                    {
                        "status": "no_usable_candidate",
                        "fingerprint": "different-raw-result-and-avoid-set",
                        "candidate_count": 0,
                        "rejected": [],
                    },
                    observation,
                )
                third = controller._record_research_recipe_exhaustion(
                    goal,
                    run,
                    {"id": "research-three"},
                    {
                        "status": "no_usable_candidate",
                        "fingerprint": "third-raw-result-variant",
                        "candidate_count": 0,
                        "rejected": [],
                    },
                    observation,
                )

                self.assertEqual(2, second["repeat_count"])
                self.assertEqual(3, third["repeat_count"])
                self.assertEqual(second["fingerprint"], third["fingerprint"])
                self.assertEqual(
                    ["research-one", "research-two"], second["phase_ids"]
                )
                self.assertFalse(
                    controller._research_retry_gate(goal, observation)["allowed"]
                )
            finally:
                controller.storage.close()

    def test_research_failures_do_not_reopen_gate_but_removed_route_blocker_does(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                observation = SimulatedBroker().observe()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="negative-research-evidence")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                baseline = controller._research_retry_state(goal, observation)
                record = {
                    "fingerprint": "closed-candidate-set",
                    "semantic_identity": {
                        "status": "no_usable_candidate",
                        "candidate_count": 0,
                        "rejected": [],
                    },
                    "repeat_count": 2,
                    "phase_ids": ["one", "two"],
                    "candidate_count": 0,
                    "rejected": [],
                    "retry_state_schema": RESEARCH_RETRY_STATE_SCHEMA_VERSION,
                    "retry_state": baseline,
                    "retry_state_fingerprint": controller._research_retry_state_fingerprint(
                        goal, observation
                    ),
                }
                controller.storage.set_runtime(
                    RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY,
                    {goal["id"]: record},
                )
                controller.storage.update_campaign_memory(
                    run["id"],
                    external_blocker={"kind": "no_usable_farm_recipe", **record},
                )

                controller.learning.defer_goal(
                    goal,
                    observation,
                    tool="prey",
                    arguments={"max_health": 34, "purpose": "advance"},
                    reason="repeated identical evidence lookup returned no new evidence",
                    classification="ineffective_tactic",
                    scope="tactic",
                    block=False,
                )
                controller.storage.set_runtime(
                    "blocked_actions",
                    [
                        {
                            "goal_id": goal["id"],
                            "tool": "prey",
                            "arguments": {"max_health": 34},
                            "room": 106,
                            "reason": "identical evidence lookup",
                        }
                    ],
                )

                self.assertEqual(
                    controller._research_retry_state_fingerprint(goal, observation),
                    record["retry_state_fingerprint"],
                )
                self.assertFalse(
                    controller._research_retry_gate(goal, observation)["allowed"]
                )

                route = controller.learning.defer_goal(
                    goal,
                    observation,
                    tool="travel",
                    arguments={"to": 563},
                    reason="route to 563 is unavailable from room 106",
                    classification="route_unavailable",
                    scope="tactic",
                    block=False,
                )["lesson"]
                narrowed = controller._research_retry_gate(goal, observation)
                self.assertFalse(narrowed["allowed"])

                controller.storage.update_goal_lesson(route["id"], "resolved")
                reopened = controller._research_retry_gate(goal, observation)
                self.assertTrue(reopened["allowed"])
                self.assertIn(
                    "blocking_evidence_removed", reopened["enabling_changes"]
                )
            finally:
                controller.storage.close()

    def test_campaign_manager_retries_invalid_context_label_as_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="manager-tool-correction")
                )["goal"]
                calls: list[dict[str, object]] = []

                def manage_campaign(**kwargs: object) -> dict[str, object]:
                    calls.append(kwargs)
                    tool = "room_info" if len(calls) == 1 else "hunting_grounds"
                    return {
                        "decision": "start_phase",
                        "phase": {
                            "kind": "research_progression",
                            "objective": "Collect one grounded progression result.",
                            "targets": [
                                {
                                    "id": "research-result",
                                    "type": "phase_action_succeeded",
                                    "tools": [tool],
                                }
                            ],
                            "abandon_predicates": [],
                            "budget": {"max_actions": 8, "max_minutes": 30},
                            "context": {},
                            "rationale": "Inspect progression evidence.",
                        },
                        "rationale": "Choose bounded research.",
                        "evidence": [],
                    }

                controller.model = SimpleNamespace(
                    manage_campaign=manage_campaign
                )  # type: ignore[assignment]

                _, phase, _ = controller._campaign_turn_state(
                    goal, SimulatedBroker().observe(), {}
                )

                self.assertEqual(2, len(calls))
                first_context = calls[0]["campaign_context"]
                self.assertIsInstance(first_context, dict)
                self.assertIn(
                    "hunting_grounds",
                    first_context["phase_capabilities"]["research_progression"],
                )
                self.assertNotIn(
                    "room_info",
                    first_context["phase_capabilities"]["research_progression"],
                )
                feedback = calls[1]["campaign_context"]["manager_feedback"]
                self.assertIn("room_info", feedback["validation_error"])
                self.assertEqual("research_progression", phase["kind"])
                self.assertEqual(
                    ["hunting_grounds"], phase["success_criteria"][0]["tools"]
                )
                self.assertEqual(
                    1,
                    len(
                        controller.storage.events(
                            kinds=["campaign.manager.revision_requested"]
                        )["events"]
                    ),
                )
            finally:
                controller.storage.close()

    def test_invalid_manager_output_cannot_reopen_exhausted_research(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                observation = SimulatedBroker().observe()
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="invalid-manager-closed-research")
                )["goal"]
                run = controller.storage.ensure_campaign_run(goal)
                state_fingerprint = controller._research_retry_state_fingerprint(
                    goal, observation
                )
                state = controller._research_retry_state(goal, observation)
                record = {
                    "fingerprint": "closed-candidate-set",
                    "repeat_count": 2,
                    "phase_ids": ["one", "two"],
                    "candidate_count": 1,
                    "rejected": [],
                    "retry_state_schema": RESEARCH_RETRY_STATE_SCHEMA_VERSION,
                    "retry_state": state,
                    "retry_state_fingerprint": state_fingerprint,
                }
                controller.storage.set_runtime(
                    RESEARCH_RECIPE_EXHAUSTION_RUNTIME_KEY,
                    {goal["id"]: record},
                )
                controller.storage.update_campaign_memory(
                    run["id"],
                    external_blocker={"kind": "no_usable_farm_recipe", **record},
                )
                calls: list[dict[str, object]] = []

                def invalid_manager(**kwargs: object) -> dict[str, object]:
                    calls.append(kwargs)
                    return {
                        "decision": "start_phase",
                        "phase": {
                            "kind": "research_progression",
                            "objective": "Repeat the closed lookup.",
                            "targets": [
                                {
                                    "id": "invalid-tool",
                                    "type": "phase_action_succeeded",
                                    "tools": ["room_options_by_candidate"],
                                }
                            ],
                            "abandon_predicates": [],
                            "budget": {"max_actions": 8, "max_minutes": 30},
                            "context": {},
                        },
                        "rationale": "Invalid test response.",
                        "evidence": [],
                    }

                controller.model = SimpleNamespace(
                    manage_campaign=invalid_manager
                )  # type: ignore[assignment]

                _, phase, _ = controller._campaign_turn_state(
                    goal, observation, {}
                )

                self.assertEqual(2, len(calls))
                self.assertEqual("prepare_combat", phase["kind"])
                self.assertTrue(phase["context"]["research_exhaustion_support"])
                self.assertEqual(
                    0,
                    len(
                        [
                            item
                            for item in controller.storage.campaign_phases(run["id"])
                            if item["kind"] == "research_progression"
                        ]
                    ),
                )
            finally:
                controller.storage.close()


if __name__ == "__main__":
    unittest.main()
