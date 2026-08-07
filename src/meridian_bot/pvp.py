from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

from .broker import BrokerError, Tool, ToolCallError
from .config import BotConfig


PVP_TOOL_NAME = "pvp_engage"
PVP_SEEK_TOOL_NAME = "pvp_seek"

# Source-verified wilderness road circuit outside Tos, Cor Noth, and Barloque.
# These rooms have no permanent ROOM_NO_COMBAT, ROOM_NO_PK,
# ROOM_GUILD_PK_ONLY, or ROOM_SAFE_DEATH flag in the pinned KOD class database.
# City streets require guild eligibility, while Tos public interiors prohibit
# combat entirely, so neither belongs in the unguilded default patrol.
DEFAULT_PVP_SEARCH_ROOMS = (575, 574, 583, 593, 603)

PVP_HARD_BLOCKING_ROOM_FLAGS = frozenset({"ROOM_NO_COMBAT", "ROOM_NO_PK"})


class ToolCaller(Protocol):
    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float = 180,
        mutation: bool = False,
    ) -> Any: ...


PVP_TOOL = Tool(
    PVP_TOOL_NAME,
    (
        "Engage one explicitly named PLAYER with a deterministic, health-aware combat loop. Use this "
        "instead of `fight`, which intentionally excludes players, and instead of manually issuing a "
        "long chain of attacks. The target must currently be visible and is matched exactly by name or "
        "object id. The coordinator suspends background autopilot, disables the "
        "ordinary safety flag, refreshes the target position every round, approaches, optionally casts "
        "self and targeted spells, makes server-paced swings, and checks your health after every round. "
        "At the disengage threshold it optionally casts an untargeted escape spell and takes a reachable "
        "exit. It restores safety and fallback autopilot afterward. The independent social listener stays "
        "active, but it is configured never to turn the character toward a speaker. Player disappearance "
        "cannot prove a kill, so the result says left_or_defeated rather than guessing. This is ordinary "
        "client play; the server validates every action. No operator approval is involved."
    ),
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "agent": {"type": "string"},
            "target": {
                "type": "string",
                "minLength": 1,
                "description": "exact visible player name, or that player's numeric object id",
            },
            "max_rounds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "maximum observe/position/cast/swing rounds; default 6",
            },
            "swings_per_round": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
                "description": "server-paced swings before health is checked again; default 1",
            },
            "disengage_at": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 0.95,
                "description": "own-health fraction at or below which to disengage; defaults to the configured recovery threshold",
            },
            "self_spells": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string"},
                "description": "untargeted buffs or setup spells to cast once before engaging",
            },
            "target_spells": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string"},
                "description": "player-targeted spells to cycle through, at most one per combat round",
            },
            "escape_spell": {
                "type": "string",
                "description": "untargeted spell to try when disengaging; default blink, empty string disables",
            },
            "flee_to_exit": {
                "type": "boolean",
                "description": "after the escape spell, take the nearest reachable room exit; default true",
            },
            "equip": {
                "type": "boolean",
                "description": "wield the broker's best available weapon before combat; default true",
            },
            "loot": {
                "type": "boolean",
                "description": "if the attacked player vanishes, attempt to collect available drops and include them in the durable transaction log; default true",
            },
        },
        "required": ["agent", "target"],
    },
)


PVP_SEEK_TOOL = Tool(
    PVP_SEEK_TOOL_NAME,
    (
        "Search for a PLAYER through ordinary world travel, then engage immediately when that player "
        "is freshly visible. Global `who` establishes only whether a named candidate is online; it never "
        "authorizes an attack. The coordinator patrols multiple exact rooms, records search coverage, "
        "checks each room locally, and collapses target acquisition and pvp_engage into one deterministic "
        "action so a moving player is not lost during another LLM turn. If target is omitted, the first "
        "locally visible non-excluded player is eligible. Before travel, source-derived room flags remove "
        "no-combat, no-PK, unverified guild-only, and (for loot hunts) safe-death rooms. The default patrol "
        "covers the source-verified wilderness road circuit outside Tos, Cor Noth, and Barloque: 575, "
        "574, 583, 593, and 603. This uses only ordinary broker travel/look/who/combat calls."
    ),
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "agent": {"type": "string"},
            "target": {
                "type": "string",
                "minLength": 1,
                "description": "optional exact online player name or numeric object id; omit to seek any eligible visible player",
            },
            "exclude_targets": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "minLength": 1},
                "description": "player names or ids not eligible during this patrol",
            },
            "rooms": {
                "type": "array",
                "minItems": 2,
                "maxItems": 12,
                "items": {"type": "integer", "minimum": 1},
                "description": "ordered grounded room ids to patrol; effective source-derived combat flags are enforced and the wilderness-road default is 575, 574, 583, 593, and 603",
            },
            "max_passes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
                "description": "maximum passes over the patrol; default 1",
            },
            "dwell_seconds": {
                "type": "integer",
                "minimum": 0,
                "maximum": 30,
                "description": "seconds to watch each room with server-paced local looks; default 6",
            },
            "max_rounds": PVP_TOOL.schema["properties"]["max_rounds"],
            "swings_per_round": PVP_TOOL.schema["properties"]["swings_per_round"],
            "disengage_at": PVP_TOOL.schema["properties"]["disengage_at"],
            "self_spells": PVP_TOOL.schema["properties"]["self_spells"],
            "target_spells": PVP_TOOL.schema["properties"]["target_spells"],
            "escape_spell": PVP_TOOL.schema["properties"]["escape_spell"],
            "flee_to_exit": PVP_TOOL.schema["properties"]["flee_to_exit"],
            "equip": PVP_TOOL.schema["properties"]["equip"],
            "loot": PVP_TOOL.schema["properties"]["loot"],
        },
        "required": ["agent"],
    },
)


class PvpCoordinator:
    def __init__(
        self,
        config: BotConfig,
        broker: Callable[[], ToolCaller],
        *,
        room_policy: Callable[[int], dict[str, Any] | None] | None = None,
        guild_eligible: Callable[[], bool] | None = None,
    ):
        self.config = config
        self._broker = broker
        self._room_policy = room_policy
        self._guild_eligible = guild_eligible

    @property
    def tool(self) -> Tool:
        return PVP_TOOL

    def planner_tool(self) -> dict[str, Any]:
        return self.tool.planner_view()

    def planner_tools(self) -> list[dict[str, Any]]:
        return [PVP_SEEK_TOOL.planner_view(), PVP_TOOL.planner_view()]

    def tool_for(self, name: str) -> Tool:
        if name == PVP_SEEK_TOOL_NAME:
            return PVP_SEEK_TOOL
        if name == PVP_TOOL_NAME:
            return PVP_TOOL
        raise ValueError(f"unknown PvP coordinator tool: {name}")

    def validate(self, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments.get("target"), str):
            raise ValueError("pvp_engage.target must be a string containing an exact player name or object id")
        target = str(arguments.get("target", "")).strip()
        if not target:
            raise ValueError("pvp_engage.target must be a non-empty exact player name or object id")
        self._bounded_int(arguments, "max_rounds", default=6, minimum=1, maximum=20)
        self._bounded_int(arguments, "swings_per_round", default=1, minimum=1, maximum=3)
        threshold = self._threshold(arguments)
        if not 0 < threshold <= 0.95:
            raise ValueError("pvp_engage.disengage_at must be greater than 0 and at most 0.95")
        for field in ("self_spells", "target_spells"):
            spells = arguments.get(field, [])
            if not isinstance(spells, list) or len(spells) > 4 or any(not isinstance(item, str) or not item.strip() for item in spells):
                raise ValueError(f"pvp_engage.{field} must be an array of at most four non-empty spell names")
        for field in ("flee_to_exit", "equip", "loot"):
            if field in arguments and not isinstance(arguments[field], bool):
                raise ValueError(f"pvp_engage.{field} must be a boolean")
        if "escape_spell" in arguments and not isinstance(arguments["escape_spell"], str):
            raise ValueError("pvp_engage.escape_spell must be a string")

    def validate_seek(self, arguments: dict[str, Any]) -> None:
        target = arguments.get("target")
        if target is not None and (not isinstance(target, str) or not target.strip()):
            raise ValueError("pvp_seek.target must be a non-empty exact player name or object id when supplied")
        excluded = arguments.get("exclude_targets", [])
        if not isinstance(excluded, list) or len(excluded) > 20 or any(
            not isinstance(item, str) or not item.strip() for item in excluded
        ):
            raise ValueError("pvp_seek.exclude_targets must contain at most 20 non-empty player names or ids")
        rooms = arguments.get("rooms", list(DEFAULT_PVP_SEARCH_ROOMS))
        if (
            not isinstance(rooms, list)
            or not 2 <= len(rooms) <= 12
            or any(isinstance(room, bool) or not isinstance(room, int) or room < 1 for room in rooms)
        ):
            raise ValueError("pvp_seek.rooms must contain 2-12 positive numeric room ids")
        self._bounded_int(arguments, "max_passes", default=1, minimum=1, maximum=3, prefix=PVP_SEEK_TOOL_NAME)
        self._bounded_int(arguments, "dwell_seconds", default=6, minimum=0, maximum=30, prefix=PVP_SEEK_TOOL_NAME)
        combat = {
            key: value
            for key, value in arguments.items()
            if key not in {"rooms", "max_passes", "dwell_seconds", "exclude_targets"}
        }
        combat.setdefault("target", target or "candidate-selected-after-local-observation")
        self.validate(combat)

    def seek(self, arguments: dict[str, Any], *, timeout: float = 360) -> dict[str, Any]:
        """Patrol grounded rooms and engage only a freshly local player.

        This composition is deliberately controller-owned.  The global player
        list is eligibility evidence, while every attack target comes from a
        fresh ordinary-client ``look`` in the current room.
        """

        self.validate_seek(arguments)
        agent = str(arguments["agent"])
        requested_target = str(arguments.get("target") or "").strip()
        excluded = {str(item).strip().casefold() for item in arguments.get("exclude_targets", [])}
        requested_rooms = list(
            dict.fromkeys(int(room) for room in arguments.get("rooms", DEFAULT_PVP_SEARCH_ROOMS))
        )
        rooms, skipped_rooms, guild_eligible = self._eligible_route(
            requested_rooms,
            loot=arguments.get("loot", True) is not False,
        )
        max_passes = self._bounded_int(
            arguments, "max_passes", default=1, minimum=1, maximum=3, prefix=PVP_SEEK_TOOL_NAME
        )
        dwell_seconds = self._bounded_int(
            arguments, "dwell_seconds", default=6, minimum=0, maximum=30, prefix=PVP_SEEK_TOOL_NAME
        )
        deadline = time.monotonic() + max(30.0, float(timeout))
        report: dict[str, Any] = {
            "coordinator": PVP_SEEK_TOOL_NAME,
            "target_requested": requested_target or None,
            "engaged": False,
            "outcome": "searching",
            "search": {
                "requested_route": requested_rooms,
                "route": rooms,
                "passes_requested": max_passes,
                "dwell_seconds": dwell_seconds,
                "completed_patrol": False,
                "rooms_visited": [],
                "route_failures": [],
                "online_candidates": [],
                "skipped_rooms": skipped_rooms,
                "guild_eligibility_verified": guild_eligible,
            },
            "cleanup": {},
        }
        if len(rooms) < 2:
            report.update(
                outcome="no_eligible_search_rooms",
                reason="source-derived combat rules left fewer than two eligible patrol rooms",
            )
            return report
        autopilot_suspended = False
        try:
            self._call("autopilot", {"agent": agent, "action": "stop"}, deadline=deadline, mutation=True)
            autopilot_suspended = True
            who = self._optional_call("who", {"agent": agent}, deadline=deadline, mutation=False)
            online = self._online_players(who)
            report["search"]["online_candidates"] = online
            if requested_target and not self._candidate_online(online, requested_target):
                report.update(
                    outcome="target_offline",
                    reason="the named target is not in the ordinary global online-player list",
                )
                return report

            for pass_number in range(1, max_passes + 1):
                for room_id in rooms:
                    if time.monotonic() >= deadline:
                        report.update(outcome="search_timeout", reason="the bounded patrol time expired")
                        return report
                    view = self._look(agent, deadline)
                    current_room_id = self._room_id(view)
                    travel_result: Any = None
                    if str(current_room_id) != str(room_id):
                        try:
                            travel_result = self._call(
                                "travel", {"agent": agent, "to": room_id}, deadline=deadline, mutation=True
                            )
                        except (BrokerError, ToolCallError, ValueError) as exc:
                            failure = {
                                "pass": pass_number,
                                "requested_room_id": room_id,
                                "actual_room_id": current_room_id,
                                "actual_room_name": self._room_name(view),
                                "arrived": False,
                                "error": str(exc)[:500],
                            }
                            report["search"]["route_failures"].append(failure)
                            report["search"]["rooms_visited"].append(
                                {
                                    "pass": pass_number,
                                    "requested_room_id": room_id,
                                    "room_id": current_room_id,
                                    "room_name": self._room_name(view),
                                    "arrived": False,
                                    "error": str(exc)[:500],
                                    "visible_players": [],
                                }
                            )
                            report.update(
                                outcome="travel_error",
                                reason=(
                                    f"travel to patrol room {room_id} failed before the patrol completed: "
                                    f"{str(exc)[:400]}"
                                ),
                                route_failure=failure,
                            )
                            return report
                        view = self._look(agent, deadline)
                        actual_room_id = self._room_id(view)
                        if (
                            isinstance(travel_result, dict)
                            and travel_result.get("arrived") is False
                            and str(actual_room_id) != str(room_id)
                        ):
                            travel_log = (
                                travel_result.get("log")
                                if isinstance(travel_result.get("log"), list)
                                else []
                            )
                            failed_hop = next(
                                (
                                    item
                                    for item in reversed(travel_log)
                                    if isinstance(item, dict) and item.get("ok") is False
                                ),
                                None,
                            )
                            broker_reason = str(
                                travel_result.get("reason")
                                or (failed_hop or {}).get("reason")
                                or "broker travel did not reach the requested room"
                            )[:500]
                            failure = {
                                "pass": pass_number,
                                "requested_room_id": room_id,
                                "actual_room_id": actual_room_id,
                                "actual_room_name": self._room_name(view),
                                "arrived": False,
                                "reason": broker_reason,
                                "failed_hop": failed_hop,
                                "travel_log": travel_log,
                            }
                            report["search"]["route_failures"].append(failure)
                            report["search"]["rooms_visited"].append(
                                {
                                    "pass": pass_number,
                                    "requested_room_id": room_id,
                                    "room_id": actual_room_id,
                                    "room_name": self._room_name(view),
                                    "arrived": False,
                                    "travel_reason": broker_reason,
                                    "failed_hop": failed_hop,
                                    "visible_players": self._visible_players(view),
                                }
                            )
                            report.update(
                                outcome="route_unavailable",
                                reason=(
                                    f"patrol route could not reach requested room {room_id} from "
                                    f"{self._room_name(view) or actual_room_id}: {broker_reason}"
                                )[:700],
                                route_failure=failure,
                            )
                            return report

                    watch_started = time.monotonic()
                    while True:
                        health = self._health_fraction(view)
                        if health is not None and health <= self._threshold(arguments):
                            report.update(
                                outcome="search_aborted_low_health",
                                reason="health reached the disengage threshold during the patrol",
                            )
                            return report
                        visible = self._visible_players(view)
                        visit = {
                            "pass": pass_number,
                            "requested_room_id": room_id,
                            "room_id": self._room_id(view) or room_id,
                            "room_name": self._room_name(view),
                            "arrived": not isinstance(travel_result, dict)
                            or travel_result.get("arrived") is not False,
                            "visible_players": visible,
                        }
                        target = self._select_player(
                            view, requested=requested_target or None, excluded=excluded
                        )
                        if target is not None:
                            visit["target_acquired"] = self._identity(target)
                            report["search"]["rooms_visited"].append(visit)
                            combat_arguments = {
                                key: value
                                for key, value in arguments.items()
                                if key
                                not in {
                                    "rooms",
                                    "max_passes",
                                    "dwell_seconds",
                                    "exclude_targets",
                                }
                            }
                            combat_arguments["target"] = str(target.get("id") or target.get("name"))
                            engagement = self.engage(
                                combat_arguments,
                                timeout=max(10.0, deadline - time.monotonic()),
                                initial_view=view,
                                manage_autopilot=False,
                            )
                            report["engagement"] = engagement
                            report.update(
                                engaged=bool(engagement.get("engaged")),
                                outcome=engagement.get("outcome", "unknown"),
                                target=engagement.get("target", self._identity(target)),
                            )
                            return report
                        if time.monotonic() - watch_started >= dwell_seconds:
                            report["search"]["rooms_visited"].append(visit)
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            report["search"]["rooms_visited"].append(visit)
                            report.update(outcome="search_timeout", reason="the bounded patrol time expired")
                            return report
                        time.sleep(min(2.0, remaining))
                        view = self._look(agent, deadline)

            report["search"]["completed_patrol"] = True
            report.update(
                outcome="target_not_found",
                reason="no eligible player was locally visible during the completed multi-room patrol",
            )
            return report
        finally:
            if autopilot_suspended:
                restored = self._cleanup_call(
                    "autopilot", self._fallback_arguments(agent), deadline=deadline, mutation=True
                )
                report["cleanup"]["autopilot_restored"] = restored.get("ok", False)
                if not restored.get("ok"):
                    report["cleanup_errors"] = [restored]

    def engage(
        self,
        arguments: dict[str, Any],
        *,
        timeout: float = 180,
        initial_view: dict[str, Any] | None = None,
        manage_autopilot: bool = True,
    ) -> dict[str, Any]:
        self.validate(arguments)
        agent = str(arguments["agent"])
        requested_target = str(arguments["target"]).strip()
        max_rounds = self._bounded_int(arguments, "max_rounds", default=6, minimum=1, maximum=20)
        swings_per_round = self._bounded_int(arguments, "swings_per_round", default=1, minimum=1, maximum=3)
        disengage_at = self._threshold(arguments)
        self_spells = [str(item).strip() for item in arguments.get("self_spells", [])]
        target_spells = [str(item).strip() for item in arguments.get("target_spells", [])]
        escape_spell = str(arguments.get("escape_spell", "blink")).strip()
        flee_to_exit = arguments.get("flee_to_exit", True) is not False
        equip = arguments.get("equip", True) is not False
        loot = arguments.get("loot", True) is not False
        deadline = time.monotonic() + max(10.0, float(timeout))

        report: dict[str, Any] = {
            "coordinator": PVP_TOOL_NAME,
            "target_requested": requested_target,
            "engaged": False,
            "outcome": "not_started",
            "rounds": [],
            "disengage_at": disengage_at,
            "cleanup": {},
        }
        safety_disabled = False
        autopilot_suspended = False
        target_identity: dict[str, Any] | None = None
        attacked = False

        try:
            if manage_autopilot:
                self._call("autopilot", {"agent": agent, "action": "stop"}, deadline=deadline, mutation=True)
                autopilot_suspended = True

            view = initial_view if isinstance(initial_view, dict) else self._look(agent, deadline)
            target = self._find_player(view, requested_target)
            if target is None:
                report.update(
                    outcome="target_not_visible",
                    reason="the explicitly named player is not currently visible in this room",
                    visible_players=self._visible_players(view),
                )
            else:
                target_identity = self._identity(target)
                report["target"] = target_identity
                report["started_vitals"] = self._vitals(view)
                health = self._health_fraction(view)
                room_policy, room_reasons = self._room_policy_decision(
                    self._room_id(view),
                    loot=loot,
                    guild_eligible=bool(self._guild_eligible and self._guild_eligible()),
                )
                if room_reasons:
                    outcome = (
                        "guild_required"
                        if "ROOM_GUILD_PK_ONLY" in room_policy.get("flags", [])
                        else "safe_death_ineligible"
                        if "ROOM_SAFE_DEATH" in room_policy.get("flags", [])
                        else "player_combat_forbidden"
                        if "ROOM_NO_PK" in room_policy.get("flags", [])
                        else "combat_forbidden"
                    )
                    report.update(
                        outcome=outcome,
                        reason="; ".join(room_reasons),
                        refused_room={"id": self._room_id(view), "name": self._room_name(view)},
                        room_policy=room_policy,
                    )
                elif health is not None and health <= disengage_at:
                    report.update(
                        outcome="refused_low_health",
                        reason=f"health was already {round(health * 100)}%, at or below the disengage threshold",
                    )
                else:
                    if equip:
                        report["equipment"] = self._call(
                            "equip_best", {"agent": agent}, deadline=deadline, mutation=True
                        )
                    if self_spells:
                        report["self_spells"] = []
                        for spell in self_spells:
                            report["self_spells"].append(self._optional_cast(agent, spell, None, deadline))

                    report["safety"] = self._call(
                        "safety", {"agent": agent, "on": False}, deadline=deadline, mutation=True
                    )
                    safety_disabled = True
                    report["engaged"] = True

                    for number in range(1, max_rounds + 1):
                        view = self._look(agent, deadline)
                        health = self._health_fraction(view)
                        if health is not None and health <= disengage_at:
                            report["outcome"] = "disengaged_low_health"
                            report["disengagement"] = self._disengage(
                                agent,
                                view,
                                escape_spell=escape_spell,
                                flee_to_exit=flee_to_exit,
                                deadline=deadline,
                            )
                            break

                        target = self._find_player(view, requested_target, identity=target_identity)
                        if target is None:
                            report["outcome"] = "target_left_or_defeated" if attacked else "target_escaped_before_attack"
                            report["reason"] = (
                                "the player vanished after a server-accepted attack; the protocol cannot distinguish leaving from defeat"
                                if attacked
                                else "the freshly acquired player left before the server accepted an attack"
                            )
                            if attacked and loot:
                                report["loot"] = self._attempt_loot(agent, deadline)
                            break

                        round_report: dict[str, Any] = {
                            "round": number,
                            "target": self._identity(target),
                            "health_before": self._vitals(view).get("health"),
                        }
                        distance = self._number(target.get("distance"))
                        if distance is None or distance > 1.5:
                            round_report["approach"] = self._call(
                                "approach",
                                {"agent": agent, "target": target["id"], "distance": 1},
                                deadline=deadline,
                                mutation=True,
                            )

                        if target_spells:
                            spell = target_spells[(number - 1) % len(target_spells)]
                            round_report["spell"] = self._optional_cast(agent, spell, target["id"], deadline)

                        attack = self._optional_call(
                            "attack",
                            {"agent": agent, "target": target["id"], "swings": swings_per_round},
                            deadline=deadline,
                            mutation=True,
                        )
                        round_report["attack"] = attack
                        accepted_swings = self._accepted_swings(attack)
                        round_report["accepted_swings"] = accepted_swings
                        report["accepted_swings"] = int(report.get("accepted_swings", 0)) + accepted_swings
                        attacked = attacked or accepted_swings > 0
                        after_view = self._look(agent, deadline)
                        round_report["health_after"] = self._vitals(after_view).get("health")
                        report["rounds"].append(round_report)
                        refusal = self._attack_refusal(attack)
                        if refusal is not None:
                            report.update(
                                outcome=refusal["outcome"],
                                reason=refusal["reason"],
                                refused_room={
                                    "id": self._room_id(after_view),
                                    "name": self._room_name(after_view),
                                },
                            )
                            break
                        if isinstance(attack, dict) and attack.get("failed"):
                            report.update(outcome="attack_refused", reason=str(attack.get("error", "attack failed"))[:500])
                            break
                        if self._find_player(after_view, requested_target, identity=target_identity) is None:
                            report.update(
                                outcome="target_left_or_defeated" if attacked else "target_escaped_before_attack",
                                reason=(
                                    "the player vanished after a server-accepted attack; the protocol cannot distinguish leaving from defeat"
                                    if attacked
                                    else "the player left before the server accepted an attack"
                                ),
                            )
                            if attacked and loot:
                                report["loot"] = self._attempt_loot(agent, deadline)
                            break
                        health = self._health_fraction(after_view)
                        if health is not None and health <= disengage_at:
                            report["outcome"] = "disengaged_low_health"
                            report["disengagement"] = self._disengage(
                                agent,
                                after_view,
                                escape_spell=escape_spell,
                                flee_to_exit=flee_to_exit,
                                deadline=deadline,
                            )
                            break
                    else:
                        report["outcome"] = "round_limit_reached"

                    if report["outcome"] == "not_started":
                        report["outcome"] = "round_limit_reached"
        finally:
            cleanup_errors: list[dict[str, str]] = []
            if safety_disabled:
                restored = self._cleanup_call(
                    "safety", {"agent": agent, "on": True}, deadline=deadline, mutation=True
                )
                report["cleanup"]["safety_restored"] = restored.get("ok", False)
                if not restored.get("ok"):
                    cleanup_errors.append(restored)
            if autopilot_suspended:
                restored = self._cleanup_call(
                    "autopilot", self._fallback_arguments(agent), deadline=deadline, mutation=True
                )
                report["cleanup"]["autopilot_restored"] = restored.get("ok", False)
                if not restored.get("ok"):
                    cleanup_errors.append(restored)
            if cleanup_errors:
                report["cleanup_errors"] = cleanup_errors

        return report

    def _disengage(
        self,
        agent: str,
        view: dict[str, Any],
        *,
        escape_spell: str,
        flee_to_exit: bool,
        deadline: float,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"started_vitals": self._vitals(view)}
        if escape_spell:
            result["spell"] = self._optional_cast(agent, escape_spell, None, deadline)
            view = self._look(agent, deadline)
        if flee_to_exit:
            exits = [item for item in view.get("exits", []) if isinstance(item, dict) and item.get("reachable") is not False]
            exits.sort(key=lambda item: self._number(item.get("steps_away")) or 10_000)
            if exits:
                chosen = exits[0]
                destination = chosen.get("to", chosen.get("to_name"))
                if destination is not None:
                    result["exit"] = {
                        "chosen": {key: chosen.get(key) for key in ("to", "to_name", "kind", "steps_away")},
                        "result": self._optional_call(
                            "go_through",
                            {"agent": agent, "to": destination},
                            deadline=deadline,
                            mutation=True,
                        ),
                    }
            else:
                result["exit"] = {"result": {"failed": True, "error": "no reachable exit was visible"}}
        result["finished_vitals"] = self._vitals(self._look(agent, deadline))
        return result

    def _look(self, agent: str, deadline: float) -> dict[str, Any]:
        result = self._call(
            "look",
            {"agent": agent, "cached": False, "minimap": False},
            deadline=deadline,
            mutation=False,
        )
        if not isinstance(result, dict):
            raise ToolCallError("look returned a non-object during pvp_engage")
        return result

    def _optional_cast(self, agent: str, spell: str, target: Any, deadline: float) -> dict[str, Any]:
        arguments: dict[str, Any] = {"agent": agent, "spell": spell}
        if target is not None:
            arguments["target"] = target
        return self._optional_call("cast", arguments, deadline=deadline, mutation=True)

    def _optional_call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        deadline: float,
        mutation: bool,
    ) -> dict[str, Any]:
        try:
            result = self._call(name, arguments, deadline=deadline, mutation=mutation)
            return result if isinstance(result, dict) else {"result": result}
        except (BrokerError, ToolCallError, ValueError) as exc:
            return {"failed": True, "error": str(exc)[:500], "tool": name}

    def _cleanup_call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        deadline: float,
        mutation: bool,
    ) -> dict[str, Any]:
        errors: list[str] = []
        # Every cleanup operation is an idempotent setting, so a bounded retry is
        # safer than leaving safety/background state ambiguous after a timeout.
        for attempt in (1, 2):
            try:
                result = self._call(name, arguments, deadline=deadline, mutation=mutation, cleanup=True)
                return {"ok": True, "tool": name, "attempts": attempt, "result": result}
            except (BrokerError, ToolCallError, ValueError) as exc:
                errors.append(str(exc)[:500])
        return {"ok": False, "tool": name, "attempts": 2, "error": errors[-1], "errors": errors}

    def _call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        deadline: float,
        mutation: bool,
        cleanup: bool = False,
    ) -> Any:
        remaining = deadline - time.monotonic()
        if remaining <= 0 and not cleanup:
            raise ToolCallError("pvp_engage exhausted its deterministic action budget")
        call_cap = 90.0 if name == "travel" else 30.0
        call_timeout = max(2.0, min(call_cap, remaining if remaining > 0 else 5.0))
        return self._broker().call_tool(name, arguments, timeout=call_timeout, mutation=mutation)

    def _fallback_arguments(self, agent: str) -> dict[str, Any]:
        mode = self.config.controller.fallback_mode
        arguments: dict[str, Any] = {"agent": agent, "action": "stop" if mode == "off" else "start"}
        if arguments["action"] == "start":
            arguments.update(
                mode=mode,
                rest_below=self.config.policy.rest_health_fraction,
                flee_below=self.config.policy.critical_health_fraction,
                break_out_via_logoff=False,
            )
            if mode in {"survive", "idle"}:
                arguments.update(hunt="", assigned_room=None)
        return arguments

    def _threshold(self, arguments: dict[str, Any]) -> float:
        default = max(self.config.policy.critical_health_fraction, self.config.policy.rest_health_fraction)
        return float(arguments.get("disengage_at", default))

    @staticmethod
    def _bounded_int(
        arguments: dict[str, Any],
        field: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
        prefix: str = PVP_TOOL_NAME,
    ) -> int:
        value = arguments.get(field, default)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{prefix}.{field} must be an integer from {minimum} to {maximum}")
        return value

    def _eligible_route(
        self,
        requested_rooms: list[int],
        *,
        loot: bool,
    ) -> tuple[list[int], list[dict[str, Any]], bool]:
        """Apply source-derived room combat rules before spending travel time."""

        guild_eligible = bool(self._guild_eligible and self._guild_eligible())
        route: list[int] = []
        skipped: list[dict[str, Any]] = []
        considered: set[int] = set()

        def consider(room_id: int) -> None:
            if len(route) >= 12:
                return
            considered.add(room_id)
            policy, reasons = self._room_policy_decision(
                room_id,
                loot=loot,
                guild_eligible=guild_eligible,
            )
            if reasons:
                skipped.append(
                    {
                        "room_id": room_id,
                        "room_name": policy.get("name"),
                        "flags": policy.get("flags", []),
                        "reasons": reasons,
                        "evidence": policy.get("evidence"),
                    }
                )
                return
            route.append(room_id)

        for room_id in requested_rooms:
            consider(room_id)
        # Preserve a valid explicit route. If rule filtering made it unusably
        # narrow, append the complete source-verified wilderness circuit.
        if len(route) < 2:
            for room_id in DEFAULT_PVP_SEARCH_ROOMS:
                if room_id not in considered:
                    consider(room_id)
        return route, skipped, guild_eligible

    def _room_policy_decision(
        self,
        room_id: Any,
        *,
        loot: bool,
        guild_eligible: bool,
    ) -> tuple[dict[str, Any], list[str]]:
        policy: dict[str, Any] = {}
        if self._room_policy is not None and room_id not in (None, ""):
            try:
                supplied = self._room_policy(int(room_id))
                if isinstance(supplied, dict):
                    policy = dict(supplied)
            except Exception as exc:
                # Knowledge degradation should not break cleanup or discard a
                # separately grounded live route.
                policy = {"known": False, "room_id": room_id, "error": str(exc)[:300]}
        flags = sorted(
            {
                str(flag).strip().upper()
                for flag in policy.get("flags", [])
                if str(flag).strip()
            }
        )
        policy["flags"] = flags
        reasons = [
            f"{flag} forbids ordinary player combat"
            for flag in flags
            if flag in PVP_HARD_BLOCKING_ROOM_FLAGS
        ]
        if "ROOM_GUILD_PK_ONLY" in flags and not guild_eligible:
            reasons.append("ROOM_GUILD_PK_ONLY requires verified guild eligibility")
        if loot and "ROOM_SAFE_DEATH" in flags:
            reasons.append("ROOM_SAFE_DEATH cannot satisfy a progression-and-loot hunt")
        return policy, reasons

    def _attempt_loot(self, agent: str, deadline: float) -> dict[str, Any]:
        result = self._optional_call(
            "loot", {"agent": agent, "max_items": 12}, deadline=deadline, mutation=True
        )
        items = self._loot_items(result)
        return {
            "attempted": True,
            "items_taken": items,
            "items_taken_count": sum(int(item.get("amount", 1) or 1) for item in items),
            "result": result,
        }

    @staticmethod
    def _loot_items(result: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("taken", "picked_up", "got", "items"):
            value = result.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
        return []

    @classmethod
    def _accepted_swings(cls, result: dict[str, Any]) -> int:
        if not isinstance(result, dict) or result.get("failed"):
            return 0
        if cls._attack_refusal(result) is not None:
            return 0
        swings = result.get("swings")
        if not isinstance(swings, list):
            return 0
        refused = (
            "no longer here",
            "not here",
            "not visible",
            "too far",
            "can't attack",
            "cannot attack",
            "unable to attack",
            "attack failed",
            "can't fight here",
            "cannot fight here",
            "only those in guilds",
            "only guild members",
            "cannot attack another player here",
        )
        accepted = 0
        for swing in swings:
            text = str(swing).casefold()
            if not any(marker in text for marker in refused):
                accepted += 1
        return accepted

    @staticmethod
    def _attack_refusal(result: dict[str, Any]) -> dict[str, str] | None:
        strings: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, str):
                strings.append(value)
            elif isinstance(value, dict):
                for nested in value.values():
                    collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)

        collect(result)
        markers = (
            ("guild_required", ("only those in guilds", "only guild members")),
            ("player_combat_forbidden", ("cannot attack another player here",)),
            ("combat_forbidden", ("can't fight here", "cannot fight here")),
            ("attack_refused", ("can't attack", "cannot attack", "unable to attack", "attack failed")),
        )
        for message in strings:
            folded = message.casefold()
            for outcome, phrases in markers:
                if any(phrase in folded for phrase in phrases):
                    return {"outcome": outcome, "reason": message[:500]}
        return None

    @classmethod
    def _select_player(
        cls,
        view: dict[str, Any],
        *,
        requested: str | None,
        excluded: set[str],
    ) -> dict[str, Any] | None:
        if requested:
            candidate = cls._find_player(view, requested)
            if candidate is None:
                return None
            identity = cls._identity(candidate)
            if str(identity.get("name", "")).casefold() in excluded or str(identity.get("id", "")).casefold() in excluded:
                return None
            return candidate
        candidates = [
            item
            for item in view.get("objects", [])
            if isinstance(item, dict)
            and item.get("is_player")
            and str(item.get("name", "")).casefold() not in excluded
            and str(item.get("id", "")).casefold() not in excluded
        ]
        candidates.sort(key=lambda item: (cls._number(item.get("distance")) or 10_000, str(item.get("name", ""))))
        return candidates[0] if candidates else None

    @staticmethod
    def _online_players(result: dict[str, Any]) -> list[dict[str, Any]]:
        players = result.get("players") if isinstance(result, dict) else None
        if not isinstance(players, list):
            return []
        return [
            {key: item.get(key) for key in ("id", "name") if item.get(key) is not None}
            for item in players
            if isinstance(item, dict)
        ]

    @staticmethod
    def _candidate_online(players: list[dict[str, Any]], requested: str) -> bool:
        if not players:
            # A broker without useful global who data cannot prove the player is
            # offline; continue with the lawful world search.
            return True
        folded = requested.casefold()
        return any(
            str(item.get("id", "")) == requested
            or str(item.get("name", "")).casefold() == folded
            for item in players
        )

    @staticmethod
    def _room_id(view: dict[str, Any]) -> Any:
        room = view.get("room") if isinstance(view, dict) else None
        return room.get("num") if isinstance(room, dict) else None

    @staticmethod
    def _room_name(view: dict[str, Any]) -> Any:
        room = view.get("room") if isinstance(view, dict) else None
        return room.get("name") if isinstance(room, dict) else room

    @classmethod
    def _find_player(
        cls,
        view: dict[str, Any],
        requested: str,
        *,
        identity: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        players = [item for item in view.get("objects", []) if isinstance(item, dict) and item.get("is_player")]
        if identity and identity.get("id") is not None:
            matched = next((item for item in players if item.get("id") == identity["id"]), None)
            if matched is not None:
                return matched
        if requested.isdigit():
            return next((item for item in players if str(item.get("id")) == requested), None)
        folded = requested.casefold()
        return next((item for item in players if str(item.get("name", "")).casefold() == folded), None)

    @staticmethod
    def _identity(player: dict[str, Any]) -> dict[str, Any]:
        return {
            key: player.get(key)
            for key in ("id", "name", "relation", "safety_on", "distance", "col", "row")
            if player.get(key) is not None
        }

    @classmethod
    def _visible_players(cls, view: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            cls._identity(item)
            for item in view.get("objects", [])
            if isinstance(item, dict) and item.get("is_player")
        ]

    @staticmethod
    def _vitals(value: dict[str, Any]) -> dict[str, Any]:
        vitals = value.get("vitals", {}) if isinstance(value, dict) else {}
        return vitals if isinstance(vitals, dict) else {}

    @classmethod
    def _health_fraction(cls, value: dict[str, Any]) -> float | None:
        health = cls._vitals(value).get("health")
        if not isinstance(health, dict):
            return None
        current = cls._number(health.get("value", health.get("current")))
        maximum = cls._number(health.get("max"))
        if current is None or maximum is None or maximum <= 0:
            return None
        return current / maximum

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)
