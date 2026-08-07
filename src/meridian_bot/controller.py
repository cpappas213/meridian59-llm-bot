from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .broker import BrokerClient, BrokerError, CONTROLLER_ONLY_TOOLS, Tool, ToolCallError
from .campaign import CampaignCoordinator
from .config import BotConfig
from .criteria import CriteriaEvaluator
from .knowledge import KNOWLEDGE_TOOL_NAME, KnowledgeBase, KnowledgeValidationError, normalize
from .contracts import parse_ability_metric
from .learning import (
    ARMOR_WORDS,
    WEAPON_WORDS,
    GoalDeferredError,
    GoalLearning,
    is_inventory_capacity_refusal,
)
from .model import ModelError, VllmClient
from .notifications import NotificationDispatcher
from .policy import PolicyEngine
from .pvp import PVP_SEEK_TOOL_NAME, PVP_TOOL_NAME, PvpCoordinator
from .storage import InvalidTransition, Storage
from .utils import canonical_json, contains_secret, deep_get, json_hash, redact, timestamp, uuid7


LOG = logging.getLogger(__name__)


# These broker calls ultimately move the character. Meridian's REST flag also
# sets NO_MOVE, and full health/vigor does not prove that the character has
# stood back up.  The wire protocol reports no resting bit, so the only
# reliable preflight is the ordinary STAND command itself.
MOVEMENT_TOOLS = {
    "travel",
    "walk_to",
    "go_through",
    "escape_underworld",
    "leave_raza",
}

# These calls can cross a room boundary under direct planner ownership.  A
# bounded keeper launch is deliberately excluded: once it starts, the keeper
# owns both the hazardous route and the combat loop.
FOREGROUND_ROOM_TRANSITION_TOOLS = {"travel", "go_through"}

TOS_BANK_ROOM_ID = 54
TOS_INN_ROOM_ID = 52
TOS_INNKEEPER_NAME = "paddock"
TOS_CHEESE_NAME = "wheel of cheese"
TOS_CHEESE_VIGOR = 30
RESTED_VIGOR_FLOOR = 80
# One cheese after ordinary rest reaches 110. This retains a useful combat
# buffer without forcing multi-minute stomach-drain waits between phases.
FARM_FIGHT_VIGOR = 100
PVP_TOOL_NAMES = frozenset({PVP_TOOL_NAME, PVP_SEEK_TOOL_NAME})
PVP_ROUTE_FAILURE_RUNTIME_KEY = "pvp_route_failure_v1"

# The broker's keeper retains this many shillings as walking money and does not
# expose that value through its public autopilot schema.  Sending a positive
# bank threshold below the float makes it walk to a bank but deposit nothing,
# then repeat the trip forever after returning to the farm.
BROKER_WALKING_MONEY = 400

# The keeper may continue a bounded farm until 60% health. Rest/recovery still
# begins earlier, and the separate emergency survival fallback remains more
# conservative. This is the operator-approved combat-risk boundary for farms.
FARM_FLEE_THRESHOLD = 0.60

# The ordinary client advertises ATTACK as an affordance for faction troops
# because the player is allowed to initiate combat with them.  That is not an
# aggression signal.  The game source treats neutral players as allies of the
# three political armies outside territory-claim regrouping, and treats a
# same-faction player as an ally as well.  Only positive live hostility
# evidence may promote one of these entities to a controller threat.
POLITICAL_FACTION_TROOP_ENTITY_IDS = frozenset(
    {
        "creature:duketroop",
        "creature:princesstroop",
        "creature:rebeltroop",
    }
)

LIVE_HOSTILITY_RELATIONS = frozenset({"enemy", "hostile", "aggressive"})

EXECUTION_PLAN_RUNTIME_KEY = "goal_execution_plans_v1"
EXECUTION_PLAN_SCHEMA_VERSION = 2
PURCHASE_PREFLIGHT_RUNTIME_KEY = "purchase_preflights_v1"
ONBOARDING_RUNTIME_KEY = "onboarding_v1"
GENERATED_CHARACTER_NAME_RE = re.compile(r"^User\d+$", re.IGNORECASE)


class OnboardingRequired(ValueError):
    """A gameplay goal cannot become durable before character setup finishes."""

    code = "ONBOARDING_REQUIRED"


class BotController:
    VERSION = "0.2.0"

    def __init__(self, config: BotConfig):
        self.config = config
        self.storage = Storage(config.database_path)
        self.knowledge = KnowledgeBase(config)
        self.broker = BrokerClient(config)
        self.pvp = PvpCoordinator(
            config,
            lambda: self.broker,
            room_policy=self._pvp_room_policy,
            guild_eligible=self._verified_guild_eligibility,
        )
        self.model = VllmClient(config)
        self.policy = PolicyEngine(config.policy)
        self.criteria = CriteriaEvaluator(self.storage)
        self.campaign = CampaignCoordinator(self.storage, self.criteria)
        self.learning = GoalLearning(config.learning, self.storage, lambda: self.knowledge.corpus_version)
        self.started_at = timestamp()
        self.state = "starting"
        self.last_heartbeat_at = self.started_at
        self.last_observation: dict[str, Any] | None = None
        self.dependencies = {
            "broker": "unknown",
            "model": "unknown",
            "social": "disabled" if not config.controller.conversation_enabled else "starting",
            "notifier": "unknown",
            "journal": "disabled",
            "knowledge": "healthy" if self.knowledge.available else "degraded",
        }
        self.warnings: list[str] = []
        self.stop_event = threading.Event()
        self._turn_lock = threading.Lock()
        # Ambient conversation remains responsive, but must not inject broker
        # traffic into an in-flight navigation or combat transaction.
        self._game_action_active = threading.Event()
        self._foreground_action: dict[str, Any] | None = None

        self._active_degradations: dict[str, str] = {}
        self.offline_diagnostics = False
        self._social_thread: threading.Thread | None = None
        self._notification_thread: threading.Thread | None = None
        self._last_conversation_reconcile_at = 0.0
        self._last_executive_refresh_at = 0.0
        self._visible_players: set[str] = set()
        self._pending_greetings: dict[str, dict[str, Any]] = {}
        self._financial_context_signature: str | None = None
        self._financial_context_value: dict[str, Any] | None = None
        saved_social = self.storage.get_runtime("social_presence_v1", {})
        saved_greeted = saved_social.get("greeted_at", {}) if isinstance(saved_social, dict) else {}
        saved_times = saved_social.get("greeting_times", []) if isinstance(saved_social, dict) else []
        self._greeted_at: dict[str, float] = {
            str(key): float(value)
            for key, value in (saved_greeted.items() if isinstance(saved_greeted, dict) else [])
            if isinstance(value, (int, float))
        }
        self._greeting_times: list[float] = [
            float(value)
            for value in (saved_times if isinstance(saved_times, list) else [])
            if isinstance(value, (int, float))
        ]
        saved_history = self.storage.get_runtime("conversation_history_v1", {})
        self._conversation_history: dict[str, list[dict[str, Any]]] = {
            str(key): [entry for entry in value if isinstance(entry, dict)]
            for key, value in (saved_history.items() if isinstance(saved_history, dict) else [])
            if isinstance(value, list)
        }
        self._pending_conversation_replies: dict[str, dict[str, Any]] = {}
        self._farm_full_scan_goals: set[str] = set()
        self.notifications = NotificationDispatcher(
            config,
            self.storage,
            assessor=self.model.assess_journal,
            context_provider=self._journal_assessment_context,
        )

    def _pvp_room_policy(self, room_id: int) -> dict[str, Any] | None:
        result = self.knowledge.get(f"location:{int(room_id)}")
        if result.get("status") != "found" or not isinstance(result.get("entity"), dict):
            return {"known": False, "room_id": int(room_id)}
        entity = result["entity"]
        facts = entity.get("facts") if isinstance(entity.get("facts"), dict) else {}
        evidence = entity.get("evidence") if isinstance(entity.get("evidence"), dict) else {}
        return {
            "known": True,
            "room_id": facts.get("room_id", int(room_id)),
            "name": entity.get("canonical_name"),
            "flags": facts.get("flags", []),
            "terrain": facts.get("terrain", []),
            "flag_evidence": facts.get("flag_evidence"),
            "evidence": {
                "source_tier": evidence.get("source_tier"),
                "source_ref": evidence.get("source_ref"),
                "corpus_version": evidence.get("corpus_version"),
            },
        }

    def _verified_guild_eligibility(self) -> bool:
        """Return true only when ordinary client state positively proves membership."""

        observation = self.last_observation or {}
        for path in (
            "status.guild",
            "status.guild_name",
            "status.character.guild",
            "look.self.guild",
            "look.you.guild",
        ):
            value = deep_get(observation, path)
            if isinstance(value, dict):
                if value.get("is_member") is True or value.get("member") is True:
                    return True
                if any(value.get(key) not in (None, "", 0, False) for key in ("id", "name", "guild_id", "guild_name")):
                    return True
            elif value not in (None, "", 0, False):
                if str(value).strip().casefold() not in {"none", "unguilded", "no guild"}:
                    return True
        # These viewer-relative relation bits require both players to be
        # guilded. They are valid positive evidence even when a secret guild's
        # name is absent from self-description.
        objects = deep_get(observation, "look.objects", [])
        return any(
            isinstance(item, dict)
            and item.get("is_player") is True
            and str(item.get("relation") or "").strip().casefold()
            in {"guildmate", "friend", "enemy"}
            for item in (objects if isinstance(objects, list) else [])
        )

    @staticmethod
    def _has_live_hostility_evidence(item: dict[str, Any]) -> bool:
        relation = str(item.get("relation") or "").strip().casefold()
        return relation in LIVE_HOSTILITY_RELATIONS or any(
            item.get(field) is True
            for field in (
                "hostile",
                "aggressive",
                "attacking_self",
                "targeting_self",
            )
        )

    def _repair_false_faction_troop_quarantines(self) -> list[dict[str, Any]]:
        """Remove legacy quarantines created from troop presence alone.

        Historical combat evidence remains immutable.  This only corrects the
        runtime gate when every recorded reason is the old attackable-is-hostile
        inference and all resolved threats are political faction troops.
        """
        raw = self.storage.get_runtime("farm_tactic_quarantine_v1", {})
        if not isinstance(raw, dict):
            return []
        quarantines = dict(raw)
        removed: list[dict[str, Any]] = []
        for key, value in list(quarantines.items()):
            if not isinstance(value, dict):
                continue
            threats = value.get("live_overlevel_hostiles")
            threats = threats if isinstance(threats, list) else []
            entity_ids = {
                str(item.get("entity_id") or "")
                for item in threats
                if isinstance(item, dict)
            }
            reasons = [
                str(reason).casefold()
                for reason in value.get("reasons", [])
                if reason is not None
            ]
            presence_only = bool(reasons) and all(
                "source-resolved hostile" in reason
                or ("soldier" in reason and "danger limit" in reason)
                for reason in reasons
            )
            if (
                entity_ids
                and entity_ids <= POLITICAL_FACTION_TROOP_ENTITY_IDS
                and presence_only
            ):
                removed.append(dict(value))
                quarantines.pop(key, None)
        if removed:
            self.storage.set_runtime("farm_tactic_quarantine_v1", quarantines)
            self.storage.emit_event(
                "background_farm.quarantine_corrected",
                "Removed faction-troop quarantines that lacked aggression evidence",
                severity="notice",
                interesting=True,
                data={
                    "rooms": [item.get("room") for item in removed],
                    "reason": (
                        "ordinary-client attackability is permission to initiate combat, "
                        "not proof that a political faction troop is hostile"
                    ),
                },
            )
        return removed

    def _repair_capability_unlocked_farm_quarantines(
        self, observation: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Release survivability quarantines after a verified capability gain.

        Farm quarantines are retry gates, not permanent room bans. Legacy
        records predate an explicit retry predicate, so compare them with the
        closest pre-quarantine combat observation for the same room and prey.
        Structural wall failures, live-world hazards, and deaths remain gated.
        """
        raw = self.storage.get_runtime("farm_tactic_quarantine_v1", {})
        if not isinstance(raw, dict):
            return []
        history = self.storage.get_runtime("combat_outcomes_v1", [])
        outcomes = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
        profile = self.learning.profile(observation)
        current_health = deep_get(
            observation,
            "status.vitals.health.max",
            deep_get(observation, "look.vitals.health.max"),
        )
        current_equipment = profile.get("equipment_hash")
        quarantines = dict(raw)
        released: list[dict[str, Any]] = []
        for key, record in list(quarantines.items()):
            if not isinstance(record, dict):
                continue
            reasons = [
                str(reason).casefold()
                for reason in record.get("reasons", [])
                if reason is not None
            ]
            if not reasons or any(
                marker in " ".join(reasons)
                for marker in (
                    "death",
                    "died",
                    "no safe spot",
                    "outside the fight's reach",
                    "overlevel hostile",
                )
            ):
                continue
            if all("safe spot" in reason for reason in reasons):
                continue
            quarantined_at = str(record.get("quarantined_at") or "")
            room = record.get("room", record.get("assigned_room"))
            target = str(record.get("target") or "").strip().casefold()
            candidates: list[dict[str, Any]] = []
            for outcome in outcomes:
                occurred_at = str(outcome.get("occurred_at") or "")
                if quarantined_at and occurred_at and occurred_at > quarantined_at:
                    continue
                outcome_room = outcome.get("room")
                if isinstance(outcome_room, dict):
                    outcome_room = outcome_room.get("id", outcome_room.get("name"))
                if str(outcome_room) != str(room):
                    continue
                if target and str(outcome.get("target") or "").strip().casefold() != target:
                    continue
                candidates.append(outcome)
            if not candidates:
                continue
            baseline = max(candidates, key=lambda item: str(item.get("occurred_at") or ""))
            baseline_health = deep_get(
                baseline,
                "health_after.max",
                deep_get(baseline, "health_before.max"),
            )
            baseline_equipment = baseline.get("equipment_hash")
            health_improved = (
                isinstance(current_health, (int, float))
                and isinstance(baseline_health, (int, float))
                and current_health > baseline_health
            )
            equipment_changed = bool(
                current_equipment
                and baseline_equipment
                and current_equipment != baseline_equipment
            )
            if not (health_improved or equipment_changed):
                continue
            released.append(
                {
                    **record,
                    "released_at": timestamp(),
                    "release_reason": "verified capability improved since quarantine",
                    "baseline_max_health": baseline_health,
                    "current_max_health": current_health,
                    "equipment_changed": equipment_changed,
                }
            )
            quarantines.pop(key, None)
        if not released:
            return []
        self.storage.set_runtime("farm_tactic_quarantine_v1", quarantines)
        suppression = self.storage.get_runtime("safety_suppression_v1")
        if isinstance(suppression, dict) and "quarantined_farm_tactic" in suppression.get(
            "blocker_kinds", []
        ):
            self.storage.set_runtime("safety_suppression_v1", None)
            self._clear_planner_feedback()
        self.storage.emit_event(
            "background_farm.quarantine_released",
            "Released farm quarantines after verified capability improvement",
            severity="notice",
            interesting=False,
            data={
                "rooms": [item.get("room") for item in released],
                "targets": [item.get("target") for item in released],
            },
        )
        return released

    def _repair_policy_obsolete_farm_quarantines(self) -> list[dict[str, Any]]:
        """Release records caused only by the former higher flee boundary."""
        raw = self.storage.get_runtime("farm_tactic_quarantine_v1", {})
        if not isinstance(raw, dict):
            return []
        quarantines = dict(raw)
        released: list[dict[str, Any]] = []
        for key, record in list(quarantines.items()):
            if not isinstance(record, dict):
                continue
            try:
                prior_threshold = float(record.get("flee_threshold"))
            except (TypeError, ValueError):
                continue
            reasons = [
                str(reason).strip().casefold()
                for reason in record.get("reasons", [])
                if str(reason).strip()
            ]
            deltas = record.get("deltas") if isinstance(record.get("deltas"), dict) else {}
            threshold_only = bool(reasons) and all(
                reason == "health reached the keeper flee threshold"
                for reason in reasons
            )
            consequential_failure = any(
                int(deltas.get(name, 0) or 0) > 0
                for name in ("deaths", "withdrawals")
            )
            if (
                prior_threshold <= FARM_FLEE_THRESHOLD
                or not threshold_only
                or consequential_failure
            ):
                continue
            released.append(
                {
                    **record,
                    "released_at": timestamp(),
                    "release_reason": "farm flee policy lowered by operator",
                    "prior_flee_threshold": prior_threshold,
                    "current_flee_threshold": FARM_FLEE_THRESHOLD,
                }
            )
            quarantines.pop(key, None)
        if not released:
            return []
        self.storage.set_runtime("farm_tactic_quarantine_v1", quarantines)
        suppression = self.storage.get_runtime("safety_suppression_v1")
        if isinstance(suppression, dict) and "quarantined_farm_tactic" in suppression.get(
            "blocker_kinds", []
        ):
            self.storage.set_runtime("safety_suppression_v1", None)
            self._clear_planner_feedback()
        self.storage.emit_event(
            "background_farm.quarantine_released",
            "Released threshold-only quarantines after farm flee policy changed",
            severity="notice",
            interesting=False,
            data={
                "rooms": [item.get("room") for item in released],
                "prior_thresholds": [
                    item.get("prior_flee_threshold") for item in released
                ],
                "current_threshold": FARM_FLEE_THRESHOLD,
            },
        )
        return released

    def _repair_recovered_farm_route_evidence(
        self, observation: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Release route failures disproved by live presence at the assignment."""
        current_room = self._observation_room(observation)
        if current_room is None:
            return []
        stagnations = self.storage.get_runtime("farm_tactic_stagnation_v1", {})
        stagnations = dict(stagnations) if isinstance(stagnations, dict) else {}
        repaired: list[dict[str, Any]] = []
        for goal in self.storage.goals(["paused"]):
            intent = self._goal_farm_intent(goal)
            assigned_room = intent.get("assigned_room")
            target = str(intent.get("hunt") or "").strip().casefold()
            if assigned_room is None or not target:
                continue
            stagnation_key = f"{goal['id']}|{assigned_room}|{target}"
            stagnation = stagnations.get(stagnation_key)
            live_arrival = str(current_room) == str(assigned_room)
            recorded_arrival = (
                isinstance(stagnation, dict)
                and str(stagnation.get("room")) == str(assigned_room)
                and stagnation.get("stalled_in_transit") is False
            )
            if not live_arrival and not recorded_arrival:
                continue
            lessons = [
                lesson
                for lesson in self.storage.goal_lessons(
                    statuses=["deferred", "unlocked"], goal_id=goal["id"], limit=50
                )
                if lesson.get("classification") == "route_unavailable"
                and str(
                    deep_get(
                        lesson,
                        "failed_state.failed_tactic.arguments.assigned_room",
                    )
                )
                == str(assigned_room)
            ]
            if not isinstance(stagnation, dict) and not lessons:
                continue
            stagnations.pop(stagnation_key, None)
            resolved_lessons = [
                self.storage.update_goal_lesson(
                    lesson["id"],
                    "resolved",
                    resolution_goal_id=goal["id"],
                    evidence={
                        "repair": (
                            "live ordinary-client observation is already in the assigned room"
                            if live_arrival
                            else "the controller's own failure record says the assigned room had already been reached"
                        ),
                        "room": current_room,
                        "recorded_room": (
                            stagnation.get("room")
                            if isinstance(stagnation, dict)
                            else None
                        ),
                        "at": timestamp(),
                    },
                )
                for lesson in lessons
            ]
            resumed = self.storage.manage_goal(
                {
                    "request_id": f"controller-route-recovered-{goal['id']}-{uuid7()}",
                    "goal_id": goal["id"],
                    "expected_version": goal.get("version"),
                    "action": "resume",
                    "reason": (
                        f"Controller verified the character reached assigned_room={assigned_room}; "
                        "the retained keeper route failure was historical, not terminal"
                    ),
                }
            ).get("goal")
            self.storage.set_runtime(
                f"background_farm_route_failure_handled_v1:{goal['id']}", True
            )
            repair = {
                "goal_id": goal["id"],
                "assigned_room": assigned_room,
                "target": target,
                "lesson_ids": [item["id"] for item in resolved_lessons],
                "resumed_status": resumed.get("status") if isinstance(resumed, dict) else None,
            }
            repaired.append(repair)
            self.storage.emit_event(
                "background_farm.route_recovered",
                "Released a retained route failure after live arrival was verified",
                severity="notice",
                interesting=True,
                goal_id=goal["id"],
                data=repair,
            )
        if repaired:
            self.storage.set_runtime("farm_tactic_stagnation_v1", stagnations)
        return repaired

    def _repair_position_unknown_lessons(self) -> list[dict[str, Any]]:
        """Resolve tactic lessons superseded by pre-movement relocalization."""
        repaired: list[dict[str, Any]] = []
        for lesson in self.storage.goal_lessons(statuses=["deferred"], limit=200):
            if lesson.get("scope") != "tactic" or "own position unknown" not in str(
                lesson.get("summary") or ""
            ).casefold():
                continue
            repaired.append(
                self.storage.update_goal_lesson(
                    lesson["id"],
                    "resolved",
                    evidence={
                        "repair": "controller refreshes ordinary-client look immediately before movement",
                        "at": timestamp(),
                    },
                )
            )
        blocked_actions = self.storage.get_runtime("blocked_actions", [])
        if isinstance(blocked_actions, list):
            retained = [
                item
                for item in blocked_actions
                if not (
                    isinstance(item, dict)
                    and "own position unknown"
                    in str(item.get("reason") or "").casefold()
                )
            ]
            if len(retained) != len(blocked_actions):
                self.storage.set_runtime("blocked_actions", retained)
        return repaired

    def _repair_invalid_farm_contract_lessons(self) -> list[dict[str, Any]]:
        """Release legacy goal gates caused only by repairable note syntax.

        Static game-reference failures remain durable. A malformed executable
        recipe is different: the corrected goal can be validated immediately,
        so waiting for a corpus or character-state change would make the lesson
        impossible to unlock.
        """
        repaired: list[dict[str, Any]] = []
        for lesson in self.storage.goal_lessons(statuses=["deferred"], limit=200):
            if lesson.get("scope") != "goal":
                continue
            original = self.storage.goal(str(lesson.get("goal_id") or ""))
            if not original:
                continue
            validation = self.knowledge.validate_goal(original)
            codes = {
                str(error.get("code") or "")
                for error in validation.get("errors", [])
                if isinstance(error, dict)
            }
            if codes != {"INVALID_FARM_OPERATOR_NOTES"}:
                continue
            repaired.append(
                self.storage.update_goal_lesson(
                    lesson["id"],
                    "resolved",
                    evidence={
                        "repair": "corrected farm key=value notes are validated before submission",
                        "at": timestamp(),
                    },
                )
            )
        return repaired

    def _repair_transit_goal_lessons(self) -> list[dict[str, Any]]:
        """Retire legacy whole-goal gates caused before a farm was reached."""
        repaired: list[dict[str, Any]] = []
        for lesson in self.storage.goal_lessons(
            statuses=["deferred", "unlocked"], limit=200
        ):
            summary = str(lesson.get("summary") or "").casefold()
            if not (
                lesson.get("scope") == "goal"
                and lesson.get("classification") == "insufficient_combat_power"
                and "hazardous transit" in summary
                and "before arrival" in summary
            ):
                continue
            repaired.append(
                self.storage.update_goal_lesson(
                    lesson["id"],
                    "resolved",
                    evidence={
                        "repair": (
                            "pre-arrival withdrawal is route/tactic evidence, not proof that the HP outcome is impossible"
                        ),
                        "at": timestamp(),
                    },
                )
            )
        return repaired

    def _record_bank_receipt(
        self,
        goal: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any] | None:
        before_currency = self._carried_currency(before)
        after_currency = self._carried_currency(after)
        if not (
            after_currency < before_currency
            or after_currency <= BROKER_WALKING_MONEY
        ):
            return None
        value = {
            "goal_id": goal["id"],
            "carried_currency_after": after_currency,
            "carried_currency_before": before_currency,
            "room": self._observation_room(after),
            "recorded_at": timestamp(),
        }
        self.storage.set_runtime("bank_before_hazard_receipt_v1", value)
        return value

    def _banking_resolved(
        self, goal: dict[str, Any], observation: dict[str, Any]
    ) -> bool:
        receipt = self.storage.get_runtime("bank_before_hazard_receipt_v1")
        if not isinstance(receipt, dict):
            return False
        # A successful deposit is verified character state, not intent owned by
        # one goal. Successor phases may reuse the walking float until carried
        # currency rises above the recorded post-bank amount.
        allowed = receipt.get("carried_currency_after")
        return isinstance(allowed, (int, float)) and self._carried_currency(
            observation
        ) <= int(allowed)

    def _complete_already_satisfied_bank_deposit(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        plan: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Turn a zero-currency deposit into verified preparation success."""
        result = {
            "action": "deposit",
            "amount": 0,
            "skipped": True,
            "already_satisfied": True,
            "reason": "no carried shillings; bank-before-hazard is already satisfied",
        }
        self._record_bank_receipt(goal, observation, observation)
        self._record_plan_action(
            goal,
            step_id=str(plan.get("plan_step_id") or "controller-owned-step"),
            tool="bank",
            result=result,
        )
        self.storage.emit_event(
            "action.succeeded",
            "Bank deposit preparation was already satisfied",
            interesting=False,
            goal_id=goal["id"],
            data={
                "tool": "bank",
                "arguments": redact(arguments),
                "result": result,
                "synthetic": True,
            },
        )
        self._clear_planner_feedback()
        self._clear_safety_suppression(goal["id"])
        self.last_observation = observation
        completion = self.criteria.evaluate(goal, observation)
        self.storage.set_goal_completion(
            goal["id"],
            completion,
            terminal="succeeded" if completion["all_met"] else None,
        )
        if completion["all_met"]:
            self.storage.emit_event(
                "goal.succeeded",
                f"Goal succeeded: {goal['title']}",
                interesting=True,
                goal_id=goal["id"],
                data={"completion": completion},
            )
            self.learning.record_success(goal)
        return {
            "action": "bank",
            "result": result,
            "already_satisfied": True,
            "completion": completion,
        }

    def _repair_bank_receipt(self) -> dict[str, Any] | None:
        """Adopt a verified pre-deployment bank deposit at the walking float."""
        goal = self.storage.active_goal()
        if goal is None:
            paused = self.storage.goals(["paused"])
            goal = (
                max(paused, key=lambda item: str(item.get("updated_at") or ""))
                if paused
                else None
            )
        observation = self.last_observation or {}
        room_name = str(deep_get(observation, "look.room.name", "")).casefold()
        carried = self._carried_currency(observation)
        if not goal or "bank" not in room_name or carried > BROKER_WALKING_MONEY:
            return None
        bank_events = self.storage.goal_events(
            goal["id"], kinds=["action.succeeded"], limit=50
        )
        has_goal_bank_event = any(
            isinstance(event.get("data"), dict)
            and event["data"].get("tool") == "bank"
            for event in bank_events
        )
        existing = self.storage.get_runtime("bank_before_hazard_receipt_v1")
        if has_goal_bank_event:
            value = {
                "goal_id": goal["id"],
                "carried_currency_after": carried,
                "carried_currency_before": None,
                "room": self._observation_room(observation),
                "recorded_at": timestamp(),
                "reconciled_after_restart": True,
            }
        elif self._banking_resolved(goal, observation) and isinstance(existing, dict):
            value = {**existing, "adopted_by_goal_id": goal["id"]}
        else:
            return None
        self.storage.set_runtime("bank_before_hazard_receipt_v1", value)
        suppression = self.storage.get_runtime("safety_suppression_v1")
        matching_false_stall = (
            isinstance(suppression, dict)
            and suppression.get("goal_id") == goal["id"]
            and suppression.get("blocker_kinds") == ["bank_before_hazard"]
        )
        if matching_false_stall:
            self.storage.set_runtime("safety_suppression_v1", None)
            self._clear_planner_feedback()
            if goal.get("status") == "paused":
                pauses = self.storage.goal_events(
                    goal["id"], kinds=["goal.paused"], limit=1
                )
                reason = str(
                    pauses[-1].get("data", {}).get("reason") if pauses else ""
                ).casefold()
                if "controller paused the goal after the same safety blocker" in reason:
                    resumed = self.storage.manage_goal(
                        {
                            "request_id": f"controller-bank-repair-{uuid7()}",
                            "goal_id": goal["id"],
                            "action": "resume",
                            "reason": (
                                "resumed after verified bank receipt proved the 400-shilling "
                                "walking float was already bank-safe"
                            ),
                        }
                    )["goal"]
                    value["resumed_goal_id"] = resumed.get("id")
        if self._banking_resolved(goal, observation):
            for lesson in self.storage.goal_lessons(
                statuses=["deferred"], limit=200
            ):
                summary = str(lesson.get("summary") or "").casefold()
                if (
                    lesson.get("scope") == "tactic"
                    and "deterministic safety suppression" in summary
                    and "bank" in summary
                ):
                    self.storage.update_goal_lesson(lesson["id"], "resolved")
            feedback = self.storage.get_runtime("planner_feedback")
            message = (
                str(feedback.get("message") or "").casefold()
                if isinstance(feedback, dict)
                else ""
            )
            blocked_tool = deep_get(feedback or {}, "blocked_action.tool")
            if blocked_tool == "autopilot" and "bank" in message:
                self._clear_planner_feedback()
        return value

    def _goal_advisories(
        self, goal: dict[str, Any], observation: dict[str, Any]
    ) -> list[dict[str, Any]]:
        values = self.policy.advisories(observation)
        if self._banking_resolved(goal, observation):
            values = [
                item
                for item in values
                if item.get("kind") not in {"bank_before_hazard", "consider_banking"}
            ]
        return values

    def startup(self, *, connect_game: bool = True) -> None:
        self.offline_diagnostics = not connect_game
        self.state = "reconciling"
        self.storage.upgrade_legacy_pvp_goal_criteria()
        if connect_game:
            health = self.broker.ensure_started()
            self.broker.capabilities(refresh=True)
            self.dependencies["broker"] = "healthy"
            if self.config.game.autojoin:
                self.broker.ensure_joined()
            self._set_fallback()
            if self.config.controller.conversation_enabled:
                self._start_conversation_listener()
            self.last_observation = self.broker.observe()
            self._record_character_progress(self.last_observation)
            self._repair_bank_receipt()
        else:
            health = {"sessions": []}
            self.dependencies["broker"] = "skipped"
            self.dependencies["social"] = "disabled"
        self.dependencies["journal"] = "healthy" if self.config.notifications.obsidian_enabled else "disabled"
        self.state = "running"
        self._repair_false_faction_troop_quarantines()
        previous_corpus = self.storage.get_runtime("knowledge_corpus_version")
        if previous_corpus != self.knowledge.corpus_version:
            self.storage.emit_event(
                "knowledge.corpus.updated",
                "Meridian 59 knowledge corpus is ready",
                severity="notice",
                interesting=True,
                data={
                    "previous_version": previous_corpus,
                    "corpus_version": self.knowledge.corpus_version,
                    "entity_count": self.knowledge.metadata().get("entity_count", 0),
                    "harness_revision": self.knowledge.metadata().get("harness_revision"),
                },
            )
            self.storage.set_runtime("knowledge_corpus_version", self.knowledge.corpus_version)
        if connect_game and self.last_observation:
            self.learning.repair_preparation_goal_lessons()
            self._repair_transit_goal_lessons()
            self._repair_open_goal_contracts()
            self._reconcile_inactive_goal_completions(self.last_observation)
            self.learning.backfill(self.last_observation)
            self._repair_capability_unlocked_farm_quarantines(self.last_observation)
            self._repair_policy_obsolete_farm_quarantines()
            self._repair_recovered_farm_route_evidence(self.last_observation)
            self._repair_position_unknown_lessons()
            self._repair_invalid_farm_contract_lessons()
            self.learning.refresh_unlocks(self.last_observation)
        if self.config.notifications.obsidian_enabled:
            try:
                self.notifications.refresh_executive_summary()
            except (OSError, ValueError) as exc:
                self.dependencies["journal"] = "unhealthy"
                self.warnings.append(f"executive summary refresh failed: {exc}")
        self.storage.emit_event(
            "controller.started",
            "Meridian 59 bot controller started",
            interesting=True,
            data={
                "version": self.VERSION,
                "broker_sessions": health.get("sessions", []),
                "knowledge_corpus_version": self.knowledge.corpus_version,
            },
        )
        self._start_notification_worker()
        if connect_game and self.config.controller.conversation_enabled:
            self._start_social_worker()

    def _repair_open_goal_contracts(self) -> dict[str, list[str]]:
        """Cancel invalid open goals and collapse equivalent live retries."""
        invalid: list[str] = []
        for goal in self.storage.goals(["active", "queued", "paused", "blocked"]):
            validation = self.knowledge.validate_goal(goal)
            if validation.get("valid") is not False:
                continue
            invalid.append(goal["id"])
            messages = [
                str(item.get("message") or item.get("code"))
                for item in validation.get("errors", [])[:3]
                if isinstance(item, dict)
            ]
            self.storage.manage_goal(
                {
                    "request_id": f"controller-invalid-contract-{goal['id']}-{uuid7()}",
                    "goal_id": goal["id"],
                    "action": "cancel",
                    "reason": (
                        "controller retired an invalid stored goal contract: "
                        + "; ".join(messages)
                    )[:1000],
                }
            )

        groups: dict[str, list[dict[str, Any]]] = {}
        for goal in self.storage.goals(["active", "queued", "paused", "blocked"]):
            groups.setdefault(self.learning.goal_family(goal), []).append(goal)
        duplicates: list[str] = []
        status_rank = {"active": 0, "queued": 1, "paused": 2, "blocked": 3}
        for family, goals in groups.items():
            if len(goals) < 2:
                continue
            best_rank = min(status_rank.get(str(item.get("status")), 9) for item in goals)
            keepers = [
                item
                for item in goals
                if status_rank.get(str(item.get("status")), 9) == best_rank
            ]
            keeper = max(
                keepers,
                key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            )
            retired: list[str] = []
            for goal in goals:
                if goal["id"] == keeper["id"]:
                    continue
                self.storage.manage_goal(
                    {
                        "request_id": f"controller-duplicate-goal-{goal['id']}-{uuid7()}",
                        "goal_id": goal["id"],
                        "action": "cancel",
                        "reason": f"superseded by canonical equivalent goal {keeper['id']}",
                    }
                )
                duplicates.append(goal["id"])
                retired.append(goal["id"])
            if retired:
                self.storage.emit_event(
                    "goal.duplicates_consolidated",
                    "Collapsed equivalent open goals into one canonical goal",
                    severity="notice",
                    interesting=True,
                    goal_id=keeper["id"],
                    data={
                        "goal_family": family,
                        "canonical_goal_id": keeper["id"],
                        "retired_goal_ids": retired,
                    },
                )
        return {"invalid": invalid, "duplicates": duplicates}

    def _reconcile_inactive_goal_completions(
        self, observation: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Retire paused/blocked goals whose outcomes are already verified.

        This is deliberately model-free.  Paused work can become complete while
        a recovery or successor goal changes the world, so every fresh broker
        observation gets one deterministic pass over inactive contracts.
        """
        completed: list[dict[str, Any]] = []
        for goal in self.storage.goals(["paused", "blocked"]):
            completion = self.criteria.evaluate(goal, observation)
            if not completion["all_met"]:
                continue
            prior_status = str(goal.get("status") or "inactive")
            done = self.storage.set_goal_completion(
                goal["id"],
                completion,
                terminal="succeeded",
                reason=(
                    "all deterministic criteria verified during inactive-goal "
                    "reconciliation"
                ),
            )
            self.storage.complete_campaign_run(goal["id"], status="succeeded")
            self.storage.emit_event(
                "goal.succeeded",
                f"Goal succeeded: {goal['title']}",
                interesting=True,
                goal_id=goal["id"],
                data={
                    "completion": completion,
                    "reconciled_from": prior_status,
                    "model_used": False,
                },
            )
            self.learning.record_success(done)
            completed.append(done)
        return completed

    @staticmethod
    def _keeper_is_inert(status: Any) -> bool:
        """Return whether a live keeper has yielded all game mutations.

        Newer harness builds keep the telemetry/watchdog loop alive after
        ``autopilot stop`` and expose that yielded state as ``inert``.  The
        legacy shape omitted the field and changed ``running`` to false.
        Accept both without mistaking an observing keeper for a competing
        controller.
        """
        if not isinstance(status, dict):
            return False
        inert = status.get("inert")
        return inert is True or (
            isinstance(inert, dict) and inert.get("inert") is True
        )

    @classmethod
    def _keeper_is_driving(cls, status: Any) -> bool:
        return (
            isinstance(status, dict)
            and status.get("running") is True
            and not cls._keeper_is_inert(status)
        )

    def _set_fallback(self) -> None:
        mode = self.config.controller.fallback_mode
        # A controller restart must not clobber a healthy, goal-owned farming
        # keeper that survived the process boundary. Re-adopt it when its target,
        # room, and safe-spot strategy still match the durable active goal; the
        # regular monitor will then resume evidence and safety supervision.
        if mode != "off":
            try:
                running = self.broker.call_tool(
                    "autopilot",
                    {"agent": self.config.game.agent, "action": "status"},
                    timeout=20,
                )
            except (BrokerError, ValueError):
                running = None
            active = self.storage.active_goal()
            if (
                self._keeper_is_driving(running)
                and running.get("mode") == "farm"
                and active
                and self._background_farm_mismatch(active, running) is None
            ):
                self.storage.emit_event(
                    "background_farm.recovered",
                    "Re-adopted the active goal's farming keeper after controller restart",
                    goal_id=active["id"],
                    data={
                        "activity": running.get("activity"),
                        "assigned_room": self._farm_assigned_room(running),
                        "hunt": self._farm_target(running),
                    },
                )
                return
        action = "stop" if mode == "off" else "start"
        args: dict[str, Any] = {"agent": self.config.game.agent, "action": action}
        if action == "start":
            args.update(
                {
                    "mode": mode,
                    "rest_below": self.config.policy.rest_health_fraction,
                    "flee_below": max(0.75, self.config.policy.rest_health_fraction),
                    "break_out_via_logoff": False,
                }
            )
            if mode in {"survive", "idle"}:
                # The upstream keeper intentionally preserves policy fields
                # across mode changes. A retained farm hunt/assignment can make
                # survive mode continue routing toward a hazardous room, so a
                # non-farming fallback must clear both explicitly.
                args.update({"hunt": "", "assigned_room": None, "bank_above": 0})
        self.broker.call_tool("autopilot", args, timeout=20, mutation=True)

    def _start_conversation_listener(self) -> None:
        self.broker.call_tool(
            "converse",
            {
                "agent": self.config.game.agent,
                "action": "start",
                # Every real speaker, including NPCs, should reach the configured persona
                # instead of being consumed by generic deterministic acknowledgements.
                "ack": False,
                "small_talk": False,
                "face_speaker": False,
                "escalate": True,
                "answer_peers": False,
                "replies_per_min": 20,
                "speaker_cooldown_ms": 2500,
                "per_speaker_per_min": 12,
            },
            timeout=15,
            mutation=True,
        )

    def _start_social_worker(self) -> None:
        if self.offline_diagnostics or not self.config.controller.conversation_enabled:
            self.dependencies["social"] = "disabled"
            return
        if self._social_thread and self._social_thread.is_alive():
            return
        self._social_thread = threading.Thread(
            target=self._social_loop,
            name="meridian-social-loop",
            daemon=True,
        )
        self._social_thread.start()

    def _start_notification_worker(self) -> None:
        enabled = self.config.notifications.windows_enabled or (
            self.config.notifications.obsidian_enabled
            and self.config.notifications.obsidian_vault_path is not None
        )
        if not enabled:
            self.dependencies["notifier"] = "disabled"
            return
        if self._notification_thread and self._notification_thread.is_alive():
            return
        self.dependencies["notifier"] = "starting"
        self._notification_thread = threading.Thread(
            target=self._notification_loop,
            name="meridian-notification-loop",
            daemon=True,
        )
        self._notification_thread.start()

    def _notification_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                delivered = self.notifications.dispatch_pending()
                state = "healthy" if not delivered["failed"] else "degraded"
                self.dependencies["notifier"] = state
                if self.config.notifications.obsidian_enabled:
                    self.dependencies["journal"] = state
                    if (
                        state == "healthy"
                        and time.monotonic() - self._last_executive_refresh_at >= 60.0
                    ):
                        # Keep the project note useful even when no milestone is
                        # emitted. This is an in-place current-state projection,
                        # not another journal entry.
                        self.notifications.refresh_executive_summary()
                        self._last_executive_refresh_at = time.monotonic()
            except Exception as exc:
                LOG.exception("notification dispatch failed")
                self.dependencies["notifier"] = "degraded"
                if self.config.notifications.obsidian_enabled:
                    self.dependencies["journal"] = "degraded"
                self.warnings = [*self.warnings[-9:], f"notification: {str(exc)[:300]}"]
            self.stop_event.wait(15.0)

    def _planner_tools(
        self, phase: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        tools = [tool for tool in self.broker.planner_tools() if tool.get("name") not in PVP_TOOL_NAMES]
        tools.extend(self.pvp.planner_tools())
        tools.append(self.knowledge.planner_tool())
        tools = self.campaign.tools_for_phase(phase, tools) if phase is not None else tools
        observation = self.last_observation
        if isinstance(observation, dict):
            # These tools have no planner-owned arguments, so an active tactic
            # lesson proves that every identical call in the same equipment
            # state would be redundant. Omitting the tool also prevents the planner
            # from replacing a still-actionable recovery plan with the exact
            # equip_best/wear_best call the lesson system will suppress.
            deferred_no_argument_tools = {
                name
                for name in ("equip_best", "wear_best")
                if self.learning.check_action(name, {}, observation) is not None
            }
            if deferred_no_argument_tools:
                tools = [
                    tool
                    for tool in tools
                    if str(tool.get("name") or "") not in deferred_no_argument_tools
                ]
        return tools

    def _execution_plan(self, goal: dict[str, Any]) -> dict[str, Any] | None:
        values = self.storage.get_runtime(EXECUTION_PLAN_RUNTIME_KEY, {})
        if not isinstance(values, dict):
            return None
        value = values.get(str(goal.get("id") or ""))
        contract = canonical_json(
            {
                key: goal.get(key)
                for key in ("title", "objective", "success_criteria", "constraints")
            }
        )
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != EXECUTION_PLAN_SCHEMA_VERSION
            or value.get("goal_contract") != contract
        ):
            return None
        run = self.storage.campaign_run(str(goal.get("id") or ""))
        phase = self.storage.active_campaign_phase(run["id"]) if run else None
        if phase is not None and value.get("phase_id") != phase["id"]:
            phase_context = phase.get("context") if isinstance(phase.get("context"), dict) else {}
            if value.get("phase_id") is None and phase_context.get("compatibility_phase") is True:
                value = {
                    **value,
                    "campaign_run_id": run.get("id") if run else None,
                    "phase_id": phase["id"],
                    "phase_kind": phase.get("kind"),
                    "updated_at": timestamp(),
                }
                values[str(goal.get("id") or "")] = value
                self.storage.set_runtime(EXECUTION_PLAN_RUNTIME_KEY, values)
            else:
                return None
        steps = value.get("steps")
        if isinstance(steps, list):
            repaired_steps = [
                step
                for step in steps
                if isinstance(step, dict) and step.get("tool") is not None
            ]
            constraints = goal.get("constraints") if isinstance(goal.get("constraints"), dict) else {}
            if constraints.get("bank_before_hazard") is True and self._carried_currency(
                self.last_observation or {}
            ) <= 0:
                repaired_steps = [
                    step
                    for step in repaired_steps
                    if not (
                        step.get("tool") == "bank"
                        and "deposit" in str(step.get("outcome") or "").casefold()
                    )
                ]
            if repaired_steps and len(repaired_steps) != len(steps):
                prior_normalizations = value.get("normalizations")
                normalizations = (
                    list(prior_normalizations)
                    if isinstance(prior_normalizations, list)
                    else []
                )
                normalizations.append(
                    {
                        "kind": "repaired_legacy_controller_owned_steps",
                        "before": len(steps),
                        "after": len(repaired_steps),
                    }
                )
                value = {
                    **value,
                    "steps": repaired_steps,
                    "normalizations": normalizations[-20:],
                    "updated_at": timestamp(),
                }
                values[str(goal.get("id") or "")] = value
                self.storage.set_runtime(EXECUTION_PLAN_RUNTIME_KEY, values)
        try:
            self._validate_direct_pvp_plan(
                goal,
                value.get("steps", []) if isinstance(value.get("steps"), list) else [],
                self.criteria.evaluate(goal, self.last_observation or {}),
            )
        except ModelError:
            # Retire a pre-enforcement plan silently. The next tactical turn
            # receives no verified plan and must produce a contract-conforming
            # replacement before any mutation can run.
            values.pop(str(goal.get("id") or ""), None)
            self.storage.set_runtime(EXECUTION_PLAN_RUNTIME_KEY, values)
            return None
        plan_normalizations = value.get("normalizations")
        repaired_legacy_steps = any(
            isinstance(item, dict)
            and item.get("kind") == "repaired_legacy_controller_owned_steps"
            for item in (
                plan_normalizations if isinstance(plan_normalizations, list) else []
            )
        )
        if repaired_legacy_steps:
            feedback = self._planner_feedback(goal)
            feedback_message = str((feedback or {}).get("message") or "").casefold()
            if "selected action tool did not match" in feedback_message:
                self._clear_planner_feedback()
        return value

    def _invalidate_execution_plan(self, goal: dict[str, Any], reason: str) -> bool:
        """Retire a plan whose factual execution assumption was disproved."""
        values = self.storage.get_runtime(EXECUTION_PLAN_RUNTIME_KEY, {})
        if not isinstance(values, dict):
            return False
        removed = values.pop(str(goal.get("id") or ""), None)
        if not isinstance(removed, dict):
            return False
        self.storage.set_runtime(EXECUTION_PLAN_RUNTIME_KEY, values)
        self.storage.emit_event(
            "planner.plan.invalidated",
            "Invalidated an execution plan after verified action failure",
            severity="warning",
            interesting=False,
            goal_id=goal.get("id"),
            data={
                "reason": str(reason)[:500],
                "plan_summary": str(removed.get("summary") or "")[:500],
                "last_action": redact(removed.get("last_action")),
            },
        )
        return True

    def _store_execution_plan(
        self,
        goal: dict[str, Any],
        raw_plan: Any,
        *,
        grounding: dict[str, Any],
        revision: bool,
    ) -> dict[str, Any]:
        if not isinstance(raw_plan, dict):
            raise ModelError("decision=plan requires an execution_plan object")
        summary = str(raw_plan.get("summary") or "").strip()
        steps = raw_plan.get("steps")
        assumptions = raw_plan.get("assumptions", [])
        plan_normalizations: list[dict[str, Any]] = []
        if isinstance(steps, list):
            actionable_steps = [
                step
                for step in steps
                if isinstance(step, dict) and step.get("tool") is not None
            ]
            if len(actionable_steps) != len(steps) and 1 <= len(actionable_steps) <= 8:
                plan_normalizations.append(
                    {
                        "kind": "removed_controller_owned_monitoring_steps",
                        "before": len(steps),
                        "after": len(actionable_steps),
                    }
                )
                steps = actionable_steps
            constraints = goal.get("constraints") if isinstance(goal.get("constraints"), dict) else {}
            if constraints.get("bank_before_hazard") is True and self._carried_currency(
                self.last_observation or {}
            ) <= 0:
                necessary_steps = [
                    step
                    for step in steps
                    if not (
                        isinstance(step, dict)
                        and step.get("tool") == "bank"
                        and "deposit" in str(step.get("outcome") or "").casefold()
                    )
                ]
                if len(necessary_steps) != len(steps) and necessary_steps:
                    plan_normalizations.append(
                        {
                            "kind": "removed_already_satisfied_zero_currency_deposit",
                            "before": len(steps),
                            "after": len(necessary_steps),
                        }
                    )
                    steps = necessary_steps
        if not summary or not isinstance(steps, list) or not 1 <= len(steps) <= 8:
            count = len(steps) if isinstance(steps, list) else "non-array"
            raise ModelError(
                f"execution_plan requires a summary and 1-8 ordered steps; received {count} step(s)"
            )
        if not isinstance(assumptions, list) or any(not isinstance(value, str) for value in assumptions):
            raise ModelError("execution_plan.assumptions must be an array of strings")
        run = self.storage.campaign_run(str(goal.get("id") or ""))
        phase = self.storage.active_campaign_phase(run["id"]) if run else None
        known_tools = {
            str(tool.get("name") or "") for tool in self._planner_tools(phase)
        }
        globally_known_tools = {
            str(tool.get("name") or "") for tool in self.broker.planner_tools()
        }
        globally_known_tools.update(PVP_TOOL_NAMES)
        globally_known_tools.add("knowledge_search")
        phase_steps = [
            step
            for step in steps
            if not (
                isinstance(step, dict)
                and isinstance(step.get("tool"), str)
                and step.get("tool") in globally_known_tools
                and step.get("tool") not in known_tools
            )
        ]
        if len(phase_steps) != len(steps) and phase_steps:
            removed = [
                {
                    "id": step.get("id"),
                    "tool": step.get("tool"),
                }
                for step in steps
                if isinstance(step, dict)
                and isinstance(step.get("tool"), str)
                and step.get("tool") in globally_known_tools
                and step.get("tool") not in known_tools
            ]
            plan_normalizations.append(
                {
                    "kind": "removed_out_of_phase_steps",
                    "removed": removed,
                    "before": len(steps),
                    "after": len(phase_steps),
                }
            )
            steps = phase_steps
        normalized_steps: list[dict[str, Any]] = []
        ids: set[str] = set()
        for index, raw_step in enumerate(steps):
            if not isinstance(raw_step, dict):
                raise ModelError(f"execution_plan step {index + 1} must be an object")
            step_id = str(raw_step.get("id") or "").strip()
            outcome = str(raw_step.get("outcome") or "").strip()
            verification = str(raw_step.get("verification") or "").strip()
            tool = raw_step.get("tool")
            if not step_id or step_id in ids or not outcome or not verification:
                raise ModelError(
                    "execution_plan steps require unique non-empty id, outcome, and verification"
                )
            if tool is not None and (not isinstance(tool, str) or tool not in known_tools):
                raise ModelError(f"execution_plan step {step_id} names unknown tool {tool!r}")
            ids.add(step_id)
            normalized_steps.append(
                {
                    "id": step_id,
                    "outcome": outcome[:600],
                    "tool": tool,
                    "verification": verification[:600],
                }
            )
        purchase = grounding.get("purchase_verification")
        if isinstance(purchase, dict) and not purchase.get("static_verified"):
            raise ModelError("execution plan cannot be verified because purchase feasibility is invalid")
        farm_intent = self._effective_farm_intent(goal)
        current_completion = self.criteria.evaluate(goal, self.last_observation or {})
        self._validate_direct_pvp_plan(goal, normalized_steps, current_completion)
        farm_work_remains = any(
            item.get("result", {}).get("met") is not True
            for item in self._health_progress_criteria(goal, current_completion)
        )
        launch_indexes = [
            index
            for index, step in enumerate(normalized_steps)
            if step.get("tool") == "autopilot"
            and re.search(r"\b(?:start|launch|farm)\b", step.get("outcome", ""), re.IGNORECASE)
        ]
        if not farm_work_remains and launch_indexes:
            raise ModelError(
                "the HP progression criterion is already met; the plan must not relaunch the farm keeper"
            )
        if (
            farm_work_remains
            and farm_intent.get("assigned_room") is not None
            and farm_intent.get("hunt")
        ):
            if not launch_indexes:
                raise ModelError(
                    "a structured farm goal plan requires one explicit autopilot launch step"
                )
            launch_index = launch_indexes[0]
            launch_text = canonical_json(normalized_steps[launch_index])
            assigned_room = str(farm_intent["assigned_room"])
            hunt_words = [
                word for word in normalize(farm_intent["hunt"]).split() if len(word) > 2
            ]
            if assigned_room not in launch_text or any(word not in normalize(launch_text) for word in hunt_words):
                raise ModelError(
                    "the farm launch step must name the goal-owned prey and exact assigned room"
                )
            current_room = deep_get(
                self.last_observation or {},
                "look.room.num",
                deep_get(self.last_observation or {}, "look.room_id"),
            )
            if str(current_room) != str(TOS_INN_ROOM_ID):
                prior_steps = normalized_steps[:launch_index]
                has_return_to_inn = any(
                    step.get("tool") == "travel"
                    and (
                        str(TOS_INN_ROOM_ID) in canonical_json(step)
                        or "tos inn" in normalize(canonical_json(step))
                        or "familiars" in normalize(canonical_json(step))
                    )
                    for step in prior_steps
                )
                if not has_return_to_inn:
                    raise ModelError(
                        "the farm plan must travel to Tos Inn room 52 before its autopilot launch step"
                    )
        value = {
            "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
            "goal_id": goal["id"],
            "goal_version": goal["version"],
            "campaign_run_id": run.get("id") if run else None,
            "phase_id": phase.get("id") if phase else None,
            "phase_kind": phase.get("kind") if phase else None,
            "goal_contract": canonical_json(
                {
                    key: goal.get(key)
                    for key in ("title", "objective", "success_criteria", "constraints")
                }
            ),
            "summary": summary[:1000],
            "steps": normalized_steps,
            "assumptions": [str(value).strip()[:500] for value in assumptions if str(value).strip()][:20],
            "normalizations": plan_normalizations,
            "revision_reason": str(raw_plan.get("revision_reason") or "").strip()[:1000] or None,
            "verification": {
                "status": "verified",
                "verified_at": timestamp(),
                "goal_contract_valid": bool(grounding.get("valid")),
                "knowledge_corpus": deep_get(grounding, "corpus.corpus_version"),
                "purchase_static_verified": (
                    bool(purchase.get("static_verified")) if isinstance(purchase, dict) else None
                ),
                "live_state_required_before_each_action": True,
            },
            "last_action": None,
            "updated_at": timestamp(),
        }
        values = self.storage.get_runtime(EXECUTION_PLAN_RUNTIME_KEY, {})
        values = dict(values) if isinstance(values, dict) else {}
        values[goal["id"]] = value
        active_ids = {
            item["id"]
            for item in self.storage.goals(["active", "queued", "paused", "blocked"])
        }
        values = {
            key: item
            for key, item in values.items()
            if key in active_ids or key == goal["id"]
        }
        self.storage.set_runtime(EXECUTION_PLAN_RUNTIME_KEY, values)
        self.storage.emit_event(
            "planner.plan.revised" if revision else "planner.plan.verified",
            ("Revised" if revision else "Verified") + f" execution plan: {summary[:180]}",
            interesting=False,
            goal_id=goal["id"],
            data={"plan": redact(value)},
        )
        return value

    def _record_plan_action(
        self,
        goal: dict[str, Any],
        *,
        step_id: str,
        tool: str,
        result: Any,
    ) -> None:
        values = self.storage.get_runtime(EXECUTION_PLAN_RUNTIME_KEY, {})
        if not isinstance(values, dict) or not isinstance(values.get(goal["id"]), dict):
            return
        value = dict(values[goal["id"]])
        value["last_action"] = {
            "step_id": step_id,
            "tool": tool,
            "observed_at": timestamp(),
            "result_summary": str(redact(result))[:1000],
        }
        value["updated_at"] = timestamp()
        values = dict(values)
        values[goal["id"]] = value
        self.storage.set_runtime(EXECUTION_PLAN_RUNTIME_KEY, values)

    @staticmethod
    def _purchase_plan(goal: dict[str, Any]) -> dict[str, Any] | None:
        constraints = goal.get("constraints")
        value = constraints.get("purchase_plan") if isinstance(constraints, dict) else None
        return value if isinstance(value, dict) else None

    def _purchase_preflight(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        *,
        force: bool = False,
    ) -> dict[str, Any] | None:
        purchase = self._purchase_plan(goal)
        if purchase is None:
            return None
        values = self.storage.get_runtime(PURCHASE_PREFLIGHT_RUNTIME_KEY, {})
        values = dict(values) if isinstance(values, dict) else {}
        prior = values.get(goal["id"])
        prior = prior if isinstance(prior, dict) else {}
        room_id = deep_get(observation, "look.room.num", deep_get(observation, "look.room_id"))
        target_room = int(purchase["room_id"])
        now_unix = time.time()
        base = {
            "goal_id": goal["id"],
            "goal_version": goal["version"],
            "offering_kind": purchase.get("offering_kind", "item"),
            "item": purchase["item"],
            "merchant_class": purchase["merchant_class"],
            "room_id": target_room,
            "maximum_price": purchase.get("maximum_price"),
            "static_verified": True,
            "live_verified": False,
            "checked_at": timestamp(),
            "checked_unix": now_unix,
        }
        if room_id != target_room:
            value = {**base, "status": "travel_required", "current_room_id": room_id}
            values[goal["id"]] = value
            self.storage.set_runtime(PURCHASE_PREFLIGHT_RUNTIME_KEY, values)
            return value
        if (
            not force
            and prior.get("status") == "live_verified"
            and now_unix - float(prior.get("checked_unix", 0) or 0) <= 60
        ):
            return prior
        if (
            not force
            and prior.get("status") in {"merchant_not_visible", "item_not_quoted"}
            and now_unix - float(prior.get("checked_unix", 0) or 0) < 5
        ):
            return prior
        attempts = int(prior.get("failed_checks", 0) or 0)
        try:
            local = self.broker.call_tool(
                "merchants",
                {"agent": self.config.game.agent, "here": True},
                timeout=10,
                mutation=False,
            )
            here = local.get("here", []) if isinstance(local, dict) else []
            merchant = next(
                (
                    value
                    for value in here
                    if isinstance(value, dict)
                    and str(value.get("merchant") or "").casefold()
                    == str(purchase["merchant_class"]).casefold()
                ),
                None,
            )
            if merchant is None and len(here) == 1 and isinstance(here[0], dict):
                merchant = here[0]
            if not isinstance(merchant, dict) or merchant.get("id") is None:
                value = {
                    **base,
                    "status": "merchant_not_visible",
                    "failed_checks": attempts + 1,
                    "reason": "the statically placed merchant is not visible in the target room",
                    "live_merchants": redact(here),
                }
            else:
                quote = self.broker.call_tool(
                    "shop",
                    {"agent": self.config.game.agent, "seller": merchant["id"]},
                    timeout=10,
                    mutation=False,
                )
                items = quote.get("items", []) if isinstance(quote, dict) else []
                quoted = [
                    item
                    for item in items
                    if isinstance(item, dict)
                    and normalize(item.get("name")) == normalize(purchase["item"])
                ]
                if not quoted:
                    value = {
                        **base,
                        "status": "item_not_quoted",
                        "failed_checks": attempts + 1,
                        "reason": "the exact planned item was absent from the fresh live shop quote",
                        "seller_id": merchant["id"],
                        "quoted_items": redact(items),
                    }
                else:
                    costs = [
                        item.get("cost")
                        for item in quoted
                        if isinstance(item.get("cost"), int) and not isinstance(item.get("cost"), bool)
                    ]
                    maximum = purchase.get("maximum_price")
                    over_limit = bool(
                        isinstance(maximum, int) and costs and min(costs) > maximum
                    )
                    value = {
                        **base,
                        "status": "price_exceeds_limit" if over_limit else "live_verified",
                        "live_verified": not over_limit,
                        "failed_checks": attempts + 1 if over_limit else 0,
                        "seller_id": merchant["id"],
                        "quoted_items": redact(quoted),
                        "authorized_buy_ids": [item.get("id") for item in quoted],
                        "minimum_price": min(costs) if costs else None,
                        "reason": "fresh live price exceeds purchase_plan.maximum_price"
                        if over_limit
                        else None,
                    }
        except (BrokerError, ToolCallError, TypeError, ValueError) as exc:
            value = {
                **base,
                "status": "verification_error",
                "failed_checks": attempts + 1,
                "reason": str(exc)[:500],
            }
        values[goal["id"]] = value
        self.storage.set_runtime(PURCHASE_PREFLIGHT_RUNTIME_KEY, values)
        return value

    def _purchase_action_blockers(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        tool: str,
        arguments: dict[str, Any],
    ) -> list[dict[str, Any]]:
        purchase = self._purchase_plan(goal)
        if purchase is None or tool != "shop" or not arguments.get("buy_ids"):
            return []
        preflight = self._purchase_preflight(goal, observation, force=False) or {}
        if preflight.get("status") != "live_verified":
            return [
                {
                    "kind": "purchase_plan_unverified",
                    "guidance": "reach the verified merchant room and obtain a fresh live merchant/stock/price quote before buying",
                    "preflight": redact(preflight),
                }
            ]
        if str(arguments.get("seller")) != str(preflight.get("seller_id")):
            return [
                {
                    "kind": "purchase_seller_mismatch",
                    "guidance": "use only the seller id from the fresh verified quote",
                }
            ]
        authorized = {int(value) for value in preflight.get("authorized_buy_ids", []) if isinstance(value, int)}
        requested = {int(value) for value in arguments.get("buy_ids", []) if isinstance(value, int)}
        if not requested or not requested.issubset(authorized):
            return [
                {
                    "kind": "purchase_item_mismatch",
                    "guidance": "buy_ids must refer only to the exact item authorized by purchase_plan and the fresh quote",
                }
            ]
        minimum_price = preflight.get("minimum_price")
        if isinstance(minimum_price, int) and self._carried_currency(observation) < minimum_price:
            return [
                {
                    "kind": "purchase_funds_insufficient",
                    "guidance": f"obtain at least {minimum_price} carried shillings before buying",
                    "required": minimum_price,
                    "carried": self._carried_currency(observation),
                }
            ]
        return []

    def _emit_shop_property_transaction(
        self,
        goal: dict[str, Any],
        result: dict[str, Any],
        *,
        correlation_id: str | None = None,
        policy_decision_id: str | None = None,
        recovered_from_event_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Record a verified shop purchase as a durable property transaction."""

        bought = result.get("bought")
        acquired = result.get("got")
        bought = bought if isinstance(bought, list) else []
        acquired = acquired if isinstance(acquired, list) else []
        if not bought and not acquired:
            return None
        item_count = len(acquired) or len(bought)
        return self.storage.emit_event(
            "property.transaction",
            f"Shop purchase completed: {item_count} item(s) acquired",
            severity="notice",
            interesting=True,
            goal_id=goal["id"],
            data={
                "transaction": "shop_buy",
                "seller": result.get("seller"),
                "buy_ids": redact(bought),
                "items_acquired": redact(acquired),
                "item_count": item_count,
                "protected_or_valuable": True,
                "approval_required": False,
                "recovered_from_event_id": recovered_from_event_id,
            },
            correlation_id=correlation_id,
            policy_decision_id=policy_decision_id,
        )

    def _reconcile_purchase_transaction(self, goal: dict[str, Any]) -> None:
        """Backfill the transaction event if a crash followed a successful shop call."""

        if self._purchase_plan(goal) is None:
            return
        transaction_criteria = [
            criterion
            for criterion in goal.get("success_criteria", [])
            if isinstance(criterion, dict)
            and criterion.get("kind") == "event_occurred"
            and criterion.get("event_kind") == "property.transaction"
        ]
        if not transaction_criteria:
            return
        goal_anchor = self.storage.goal_event_anchor(str(goal.get("id") or ""))
        effective_cursors = []
        for criterion in transaction_criteria:
            requested = int(criterion.get("after_cursor", 0) or 0)
            effective_cursors.append(
                min(requested, goal_anchor)
                if goal_anchor is not None
                else requested
            )
        after_cursor = max(effective_cursors, default=0)
        existing = self.storage.events(
            after_cursor=after_cursor,
            limit=1,
            kinds=["property.transaction"],
            goal_id=goal["id"],
        )
        if existing.get("events"):
            return
        successful_actions = self.storage.events(
            after_cursor=after_cursor,
            limit=200,
            kinds=["action.succeeded"],
            goal_id=goal["id"],
        ).get("events", [])
        shop_event = next(
            (
                event
                for event in reversed(successful_actions)
                if isinstance(event, dict)
                and deep_get(event, "data.tool") == "shop"
                and isinstance(deep_get(event, "data.result"), dict)
                and (
                    deep_get(event, "data.result.bought")
                    or deep_get(event, "data.result.got")
                )
            ),
            None,
        )
        if shop_event is None:
            return
        self._emit_shop_property_transaction(
            goal,
            deep_get(shop_event, "data.result"),
            correlation_id=shop_event.get("correlation_id"),
            policy_decision_id=shop_event.get("policy_decision_id"),
            recovered_from_event_id=shop_event.get("id"),
        )

    @staticmethod
    def _purchase_result_met(
        goal: dict[str, Any], completion: dict[str, Any]
    ) -> bool:
        """Whether the exact transaction outcome, rather than a visit, is verified."""

        constraints = goal.get("constraints")
        purchase = constraints.get("purchase_plan") if isinstance(constraints, dict) else None
        if not isinstance(purchase, dict):
            return False
        offering_kind = str(purchase.get("offering_kind", "item")).casefold()
        offering_name = normalize(purchase.get("item"))
        results = completion.get("criteria", [])
        results = results if isinstance(results, list) else []

        # A replacement-purchase goal can legitimately start with a broken copy
        # of the named item already in the pack.  In that case a goal-scoped
        # property.transaction criterion is the durable proof that a new copy
        # was actually bought.  Do not let the pre-existing inventory match
        # short-circuit funding, travel, and shopping before that event occurs.
        transaction_results = [
            results[index]
            for index, criterion in enumerate(goal.get("success_criteria", []))
            if isinstance(criterion, dict)
            and criterion.get("kind") == "event_occurred"
            and criterion.get("event_kind") == "property.transaction"
            and index < len(results)
            and isinstance(results[index], dict)
        ]
        transaction_verified = not transaction_results or all(
            result.get("met") is True for result in transaction_results
        )
        for index, criterion in enumerate(goal.get("success_criteria", [])):
            if not isinstance(criterion, dict) or index >= len(results):
                continue
            result = results[index]
            if not isinstance(result, dict) or result.get("met") is not True:
                continue
            if (
                offering_kind == "item"
                and criterion.get("kind") == "inventory_contains"
                and normalize(criterion.get("item")) == offering_name
            ):
                return transaction_verified
            parsed = parse_ability_metric(criterion.get("metric"))
            if (
                offering_kind in {"skill", "spell"}
                and parsed is not None
                and parsed[0] == offering_kind
                and normalize(parsed[1]) == offering_name
            ):
                return transaction_verified
        return False

    def _structured_purchase_controller_plan(
        self,
        goal: dict[str, Any],
        completion: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        purchase = self._purchase_plan(goal) or {}
        offering_kind = str(purchase.get("offering_kind", "item"))
        offering = str(purchase.get("item") or "planned offering")
        merchant = str(purchase.get("merchant_class") or "verified merchant")
        room_id = int(purchase.get("room_id") or 0)
        budget = purchase.get("maximum_price")
        budget_text = f"{budget} shillings" if isinstance(budget, int) else "the bounded price"
        plan = {
            "summary": (
                f"Fund, live-verify, and acquire the exact {offering_kind} {offering} from {merchant}, "
                "then verify the actual item/ability result before returning home."
            ),
            "steps": [
                {
                    "id": "travel-to-purchase-bank",
                    "outcome": f"Reach First Royal Bank of Tos (room {TOS_BANK_ROOM_ID}) if {budget_text} is not carried.",
                    "tool": "travel",
                    "verification": f"Current room id is {TOS_BANK_ROOM_ID}.",
                },
                {
                    "id": "withdraw-purchase-funds",
                    "outcome": f"Withdraw only the shortfall needed to carry at most {budget_text} for {offering}.",
                    "tool": "bank",
                    "verification": f"Carried shillings are at least {budget if isinstance(budget, int) else 'the authorized amount'}.",
                },
                {
                    "id": "travel-to-purchase-merchant",
                    "outcome": f"Travel with the bounded funds to {merchant} in exact room {room_id}.",
                    "tool": "travel",
                    "verification": f"Current room id is {room_id} and the expected merchant is visible.",
                },
                {
                    "id": "buy-planned-offering",
                    "outcome": f"Use a fresh quote and buy only exact {offering_kind} {offering} within {budget_text}.",
                    "tool": "shop",
                    "verification": (
                        f"The live {'ability list reports ' + offering + ' at 1 or above' if offering_kind in {'skill', 'spell'} else 'inventory contains ' + offering}."
                    ),
                },
                {
                    "id": "recover-purchase-go-exit",
                    "outcome": "If ordinary travel stalls while already standing on a reachable go exit, explicitly activate that exit once.",
                    "tool": "act",
                    "verification": "The current room changes through the externally directed reachable go exit.",
                },
                {
                    "id": "recover-purchase-route-hop",
                    "outcome": "If travel selects an unusable duplicate exit, use a live reachable go exit to the exact failed next-hop room.",
                    "tool": "go_through",
                    "verification": "The current room changes to the same next-hop room named by the failed travel log.",
                },
                {
                    "id": "return-purchase-to-tos-inn",
                    "outcome": "After the exact acquisition criterion is verified, travel back to Tos Inn (room 52).",
                    "tool": "travel",
                    "verification": "Current room id is 52.",
                },
                {
                    "id": "finish-purchase-at-tos-bar",
                    "outcome": "Walk to the verified bar-side square (8,8) inside Tos Inn.",
                    "tool": "walk_to",
                    "verification": "Current room id is 52 and position is column 8, row 8.",
                },
            ],
            "assumptions": [],
            "revision_reason": (
                "Use controller-owned funding and exact live quote binding instead of conversational trial and error."
            ),
        }
        farm_intent = self._effective_farm_intent(goal)
        farm_work_remains = completion is None or any(
            item["result"].get("met") is not True
            for item in self._health_progress_criteria(goal, completion)
        )
        if (
            farm_work_remains
            and farm_intent.get("assigned_room") is not None
            and farm_intent.get("hunt")
        ):
            # A combined acquire-then-farm goal still needs a plan that proves the
            # hazardous phase is goal-owned. Drop the two optional legacy route-repair
            # steps to stay within the eight-step contract; the broker now retries a
            # totally silent go exit itself, and a concrete route failure can still
            # cause a later state-specific plan revision.
            plan["steps"] = [
                step
                for step in plan["steps"]
                if step["id"]
                not in {"recover-purchase-go-exit", "recover-purchase-route-hop"}
            ]
            plan["steps"].append(
                {
                    "id": "launch-goal-keeper",
                    "outcome": (
                        "After buying and equipping the mace, launch the grounded farm "
                        f"for {farm_intent['hunt']} in assigned room "
                        f"{farm_intent['assigned_room']}."
                    ),
                    "tool": "autopilot",
                    "verification": (
                        "Keeper status reports the exact goal-owned prey and assigned room."
                    ),
                }
            )
            plan["summary"] = (
                f"Acquire the exact {offering_kind} {offering}, return to Tos Inn, "
                f"then launch the grounded {farm_intent['hunt']} farm phase."
            )
            plan["revision_reason"] = (
                "Compose the controller-owned purchase prerequisite with the explicit "
                "goal-owned farm launch required by this combined goal."
            )
        return plan

    def _structured_purchase_preparation_action(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        completion: dict[str, Any],
        preflight: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Fund and execute a grounded purchase/training transaction deterministically."""

        purchase = self._purchase_plan(goal)
        if purchase is None:
            return None
        current_room = deep_get(
            observation, "look.room.num", deep_get(observation, "look.room_id")
        )

        def action(
            tool: str,
            arguments: dict[str, Any],
            step_id: str,
            rationale: str,
        ) -> dict[str, Any]:
            return {
                "decision": "act",
                "tool": tool,
                "arguments": arguments,
                "rationale": rationale,
                "expected_observation": {},
                "proposal": None,
                "plan_step_id": step_id,
            }

        if self._purchase_result_met(goal, completion):
            if str(current_room) != str(TOS_INN_ROOM_ID):
                blocked_actions = self.storage.get_runtime("blocked_actions", [])
                blocked_actions = (
                    blocked_actions if isinstance(blocked_actions, list) else []
                )
                stalled_on_go_exit = any(
                    isinstance(blocked, dict)
                    and blocked.get("goal_id") == goal.get("id")
                    and blocked.get("tool") in {"travel", "go_through"}
                    and str(blocked.get("room")) == str(current_room)
                    and (
                        blocked.get("tool") == "go_through"
                        or str(deep_get(blocked, "arguments.to"))
                        == str(TOS_INN_ROOM_ID)
                    )
                    and "stood on the exit square and nothing happened"
                    in str(blocked.get("reason") or "").casefold()
                    for blocked in blocked_actions
                )
                exits = deep_get(observation, "look.exits", [])
                exits = exits if isinstance(exits, list) else []
                explicit_go_exit = next(
                    (
                        value
                        for value in exits
                        if isinstance(value, dict)
                        and value.get("kind") == "go"
                        and value.get("reachable") is True
                        and value.get("steps_away") == 0
                        and str(value.get("to")) != str(current_room)
                    ),
                    None,
                )
                if stalled_on_go_exit and explicit_go_exit is not None:
                    return action(
                        "act",
                        {"verb": "go"},
                        "recover-purchase-go-exit",
                        "Travel proved that the character is already on a reachable go exit; activate that exact exit once before resuming the route.",
                    )
                latest_route_failure = next(
                    (
                        event
                        for event in reversed(
                            self.storage.goal_events(
                                goal["id"], kinds=["action.no_progress"], limit=40
                            )
                        )
                        if isinstance(event, dict)
                        and deep_get(event, "data.tool") == "travel"
                        and str(deep_get(event, "data.arguments.to"))
                        == str(TOS_INN_ROOM_ID)
                        and str(deep_get(event, "data.room.num", deep_get(event, "data.room.id")))
                        == str(current_room)
                    ),
                    None,
                )
                route_log = deep_get(latest_route_failure or {}, "data.result.log", [])
                route_log = route_log if isinstance(route_log, list) else []
                failed_hop = next(
                    (
                        value
                        for value in route_log
                        if isinstance(value, dict)
                        and value.get("ok") is False
                        and str(value.get("to") or "").strip()
                    ),
                    None,
                )
                failed_hop_name = normalize(
                    failed_hop.get("to") if isinstance(failed_hop, dict) else ""
                )
                alternate_go_exits = [
                    value
                    for value in exits
                    if isinstance(value, dict)
                    and value.get("kind") == "go"
                    and value.get("reachable") is True
                    and isinstance(value.get("to"), int)
                    and normalize(value.get("to_name")) == failed_hop_name
                    and isinstance(value.get("stand_on"), dict)
                    and isinstance(value["stand_on"].get("col"), (int, float))
                    and isinstance(value["stand_on"].get("row"), (int, float))
                ]
                alternate_go_exits.sort(
                    key=lambda value: float(value.get("steps_away") or 1_000_000)
                )
                if alternate_go_exits:
                    alternate = alternate_go_exits[0]
                    return action(
                        "go_through",
                        {
                            "to": alternate["to"],
                            "col": alternate["stand_on"]["col"],
                            "row": alternate["stand_on"]["row"],
                        },
                        "recover-purchase-route-hop",
                        "Use the nearest live reachable go exit to the exact next-hop room that ordinary travel selected through an unusable duplicate edge.",
                    )
                return action(
                    "travel",
                    {"to": TOS_INN_ROOM_ID},
                    "return-purchase-to-tos-inn",
                    "The exact acquisition is verified; return to Tos Inn before final bar positioning.",
                )
            col = deep_get(
                observation,
                "status.position.col",
                deep_get(observation, "look.position.col"),
            )
            row = deep_get(
                observation,
                "status.position.row",
                deep_get(observation, "look.position.row"),
            )
            if str(col) != "8" or str(row) != "8":
                return action(
                    "walk_to",
                    {"col": 8, "row": 8},
                    "finish-purchase-at-tos-bar",
                    "Finish the completed acquisition phase at the verified Tos Inn bar square.",
                )
            return None

        budget = purchase.get("maximum_price")
        if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
            # Physical purchases may intentionally omit a cap. Paid abilities
            # cannot: goal validation rejects them before reaching this path.
            return None
        carried = self._carried_currency(observation)

        if carried < budget:
            if str(current_room) != str(TOS_BANK_ROOM_ID):
                return action(
                    "travel",
                    {"to": TOS_BANK_ROOM_ID},
                    "travel-to-purchase-bank",
                    "Obtain the authorized training/purchase budget before traveling to the merchant.",
                )
            return action(
                "bank",
                {"action": "withdraw", "amount": budget - carried},
                "withdraw-purchase-funds",
                "Withdraw exactly the remaining authorized budget before leaving the bank.",
            )

        target_room = int(purchase["room_id"])
        if str(current_room) != str(target_room):
            return action(
                "travel",
                {"to": target_room},
                "travel-to-purchase-merchant",
                "Carry the prepared bounded funds to the grounded merchant room.",
            )
        if not isinstance(preflight, dict) or preflight.get("status") != "live_verified":
            return None
        seller = preflight.get("seller_id")
        authorized = [
            value
            for value in preflight.get("authorized_buy_ids", [])
            if isinstance(value, int) and not isinstance(value, bool)
        ]
        if not isinstance(seller, int) or not authorized:
            return None
        purchase_arguments = {
            "agent": self.config.game.agent,
            "seller": seller,
            "buy_ids": [authorized[0]],
        }
        prior_purchase_failure = self._blocked_action(
            goal,
            observation,
            "shop",
            purchase_arguments,
        )
        if prior_purchase_failure and is_inventory_capacity_refusal(
            prior_purchase_failure.get("reason")
        ):
            # The deterministic purchase fast path owns ordinary funding,
            # travel, quote binding, and the first transaction attempt. Once
            # the server disproves that transaction in the current load state,
            # yield to the tactical planner. Its context contains the observed
            # refusal and carry evidence, but no controller-authored remedy.
            return None
        return action(
            "shop",
            {"seller": seller, "buy_ids": [authorized[0]]},
            "buy-planned-offering",
            "Buy only the exact offering bound to the fresh seller, item id, and price quote.",
        )

    def _available_tools(self) -> dict[str, Any]:
        tools = dict(self.broker.capabilities())
        # These intentionally shadow any future broker tools with the same
        # names: both are controller-owned compositions of ordinary calls.
        for name in PVP_TOOL_NAMES:
            tools[name] = self.pvp.tool_for(name)
        knowledge_tool = self.knowledge.planner_tool()
        tools[KNOWLEDGE_TOOL_NAME] = Tool(
            KNOWLEDGE_TOOL_NAME,
            str(knowledge_tool["description"]),
            dict(knowledge_tool["input_schema"]),
        )
        return tools

    def _normalize_combat_arguments(
        self,
        tool: str,
        arguments: dict[str, Any],
        observation: dict[str, Any] | None = None,
        *,
        allow_open_field: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        normalized = dict(arguments)
        before = dict(arguments)
        if tool == "fight":
            # The harness checks health between rounds, not between swings. One
            # swing per one-round call makes the controller's next observation
            # the safety boundary instead of allowing a four-hit burst.
            normalized.update(
                {
                    "rounds": 1,
                    "swings_per_round": 1,
                    "disengage_at": max(
                        self.config.policy.rest_health_fraction,
                        float(arguments.get("disengage_at", self.config.policy.rest_health_fraction)),
                    ),
                    "equip": True,
                    "loot": arguments.get("loot", True) is not False,
                }
            )
        elif tool == "autopilot" and arguments.get("action") == "start" and arguments.get("mode") == "farm":
            # Farming runs between controller turns, so its deterministic
            # keeper must carry the same safety envelope as a bounded fight.
            inventory_items = deep_get(observation or {}, "inventory.items", [])
            carried_slots = len(inventory_items) if isinstance(inventory_items, list) else 0
            requested_max_carry = int(arguments.get("max_carry", 14))
            # Override the upstream keeper default.  Omission means no special
            # cash-triggered trip; a positive threshold is used only when the
            # planner deliberately selected it from the disclosed finances.
            requested_bank_above = arguments.get("bank_above", 0)
            effective_bank_above = int(requested_bank_above)
            if 0 < effective_bank_above < BROKER_WALKING_MONEY:
                effective_bank_above = BROKER_WALKING_MONEY
            normalized.update(
                {
                    "rest_below": max(
                        self.config.policy.rest_health_fraction,
                        float(arguments.get("rest_below", self.config.policy.rest_health_fraction)),
                    ),
                    # This is an operator policy, not a model-selected risk
                    # preference. Normalize old queued recipes that still say
                    # 75-80% as well as overly aggressive lower proposals.
                    "flee_below": FARM_FLEE_THRESHOLD,
                    # Historical 140-vigor recipes made a fed character wait
                    # several minutes for stomach room before combat. Keep this
                    # controller-owned activity boundary consistent across old
                    # and new goals, just like the farm flee boundary above.
                    "fight_above_vigor": FARM_FIGHT_VIGOR,
                    # Open-field farming is permitted only when durable state
                    # chose it as a materially different tactic: either an
                    # operator-authored public goal or the planner's persisted
                    # internal campaign phase. A one-turn model-only false
                    # value is still normalized to the wall strategy.
                    "use_safe_spots": False if allow_open_field else True,
                    "hold_resume_above": max(0.9, float(arguments.get("hold_resume_above", 0.9))),
                    # max_carry counts occupied inventory entries, not only new
                    # loot.  A well-supplied character can already exceed the
                    # broker default before the keeper starts, causing an
                    # immediate false stop.  Preserve at least six free entries
                    # for loot and consumable churn while retaining any larger
                    # deliberate limit.
                    "max_carry": max(requested_max_carry, carried_slots + 6),
                    # The keeper cannot deposit below its non-configurable
                    # 400-shilling float.  Preserve 0 as the documented way to
                    # disable special bank trips; otherwise never authorize a
                    # trip whose deposit condition cannot be met.
                    "bank_above": effective_bank_above,
                    # The current keeper can reconnect immediately after a room
                    # transition and restore the prior dangerous saved room.
                    # Keep reconnect breakout disabled until the broker provides
                    # a stable-room/save acknowledgement.
                    "break_out_via_logoff": False,
                }
            )
        elif tool == "autopilot" and arguments.get("action") == "start" and arguments.get("mode") in {"survive", "idle"}:
            normalized.update(
                {
                    "hunt": "",
                    "assigned_room": None,
                    # A retained low farm threshold can trap even survival mode
                    # in bank travel.  Survival is for recovery, not special
                    # money runs; ordinary incidental banking still works.
                    "bank_above": 0,
                    "break_out_via_logoff": False,
                }
            )
        elif tool in PVP_TOOL_NAMES:
            normalized["swings_per_round"] = 1
            normalized["disengage_at"] = max(
                self.config.policy.rest_health_fraction,
                float(arguments.get("disengage_at", self.config.policy.rest_health_fraction)),
            )
        changes = {
            key: {"requested": before.get(key), "applied": value}
            for key, value in normalized.items()
            if before.get(key) != value
        }
        return normalized, changes

    @staticmethod
    def _underworld(observation: dict[str, Any]) -> bool:
        room = deep_get(observation, "look.room.name", deep_get(observation, "look.room", ""))
        return "underworld" in str(room or "").casefold()

    @staticmethod
    def _vital_fraction(observation: dict[str, Any], name: str) -> float | None:
        vital = deep_get(observation, f"status.vitals.{name}", deep_get(observation, f"look.vitals.{name}"))
        if not isinstance(vital, dict):
            return None
        current = vital.get("current", vital.get("value"))
        maximum = vital.get("max")
        try:
            return float(current) / float(maximum) if float(maximum) > 0 else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    @staticmethod
    def _combat_vigor_supply(observation: dict[str, Any]) -> dict[str, Any]:
        """Describe food the keeper can consume or make before an engagement."""
        items = deep_get(observation, "inventory.items", [])
        items = items if isinstance(items, list) else []
        # viNutrition values from the game source. Counting items alone was not
        # enough: the deterministic preflight must account for actual nutrition
        # against the configured farm gate rather than merely count food items.
        # the controller provisions enough food before handing control to the
        # background keeper.
        food_values = (
            ("inky cap", 50),
            ("chocolate mint", 5),
            (TOS_CHEESE_NAME, TOS_CHEESE_VIGOR),
            ("turkey leg", 15),
            ("mug of", 6),
            ("meat pie", 30),
            ("stew", 15),
            ("loaf of bread", 20),
            ("waterskin", 3),
            ("slice of pork", 9),
            ("bowl of soup", 9),
            ("spideye", 9),
            ("bunch of grapes", 7),
            ("apple", 10),
            ("edible mushroom", 5),
            ("drumstick", 9),
            ("goblet", 3),
        )
        food_count = 0
        vigor_points = 0
        herbs = 0
        elderberries = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).casefold().strip()
            raw_amount = item.get("amount", item.get("quantity", 1))
            try:
                amount = max(1, int(raw_amount or 1))
            except (TypeError, ValueError):
                amount = 1
            food_value = next(
                (value for marker, value in food_values if marker in name), None
            )
            if food_value is not None:
                food_count += amount
                vigor_points += food_value * amount
            if name in {"herb", "herbs"}:
                herbs += amount
            if name in {"elderberry", "elderberries", "elder berry", "elder berries"}:
                elderberries += amount

        spells = deep_get(observation, "spells.spells", [])
        spells = spells if isinstance(spells, list) else []
        knows_create_food = any(
            isinstance(spell, dict)
            and str(spell.get("name", "")).casefold().strip() == "create food"
            for spell in spells
        )
        cookable_casts = min(herbs // 2, elderberries // 2) if knows_create_food else 0
        return {
            "available": food_count > 0 or cookable_casts > 0,
            "food_count": food_count,
            "vigor_points": vigor_points,
            "knows_create_food": knows_create_food,
            "cookable_casts": cookable_casts,
            "herbs": herbs,
            "elderberries": elderberries,
        }

    def _live_overlevel_hostiles(
        self,
        observation: dict[str, Any],
        *,
        danger_margin: int = 6,
    ) -> list[dict[str, Any]]:
        """Join live attackable room objects to source-derived creature levels.

        Generator tables cannot describe temporary faction troops, summons, or
        other creatures created by live world state. The ordinary client does
        expose their names, and the pinned corpus knows their levels, so this
        join closes that gap without requiring an administrative game API.
        """
        maximum = deep_get(
            observation,
            "status.vitals.health.max",
            deep_get(observation, "look.vitals.health.max"),
        )
        try:
            level = int(maximum)
        except (TypeError, ValueError):
            return []
        limit = level + max(0, int(danger_margin))
        objects = deep_get(observation, "look.objects", [])
        objects = objects if isinstance(objects, list) else []
        resolved_by_name: dict[str, dict[str, Any] | None] = {}
        threats: list[dict[str, Any]] = []
        for item in objects:
            if not isinstance(item, dict) or item.get("is_player") is True:
                continue
            affordances = item.get("can") if isinstance(item.get("can"), list) else []
            if "attack" not in affordances:
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            key = name.casefold()
            if key not in resolved_by_name:
                match = self.knowledge.resolve(
                    name, kinds=["creature"], allow_fuzzy=False
                )
                resolved_by_name[key] = (
                    match.get("entity")
                    if match.get("status") in {"found", "found_fuzzy"}
                    and isinstance(match.get("entity"), dict)
                    else None
                )
            entity = resolved_by_name[key]
            entity_id = entity.get("id") if isinstance(entity, dict) else None
            if (
                entity_id in POLITICAL_FACTION_TROOP_ENTITY_IDS
                and not self._has_live_hostility_evidence(item)
            ):
                # ATTACK in the ordinary-client affordance list says the character may
                # initiate a swing.  It does not mean this troop has targeted
                # her.  Neutral and same-faction players are allies in the game
                # source, so presence alone must not become a farm quarantine.
                continue
            facts = (
                entity.get("facts")
                if isinstance(entity, dict) and isinstance(entity.get("facts"), dict)
                else {}
            )
            try:
                creature_level = int(facts.get("level"))
            except (TypeError, ValueError):
                continue
            if creature_level <= limit:
                continue
            threats.append(
                {
                    "object_id": item.get("id"),
                    "name": name,
                    "level": creature_level,
                    "character_level": level,
                    "danger_limit": limit,
                    "distance": item.get("distance"),
                    "reachable": item.get("reachable"),
                    "entity_id": entity_id,
                    "source_ref": deep_get(entity or {}, "evidence.source_ref"),
                }
            )
        return sorted(
            threats,
            key=lambda item: (-int(item["level"]), float(item.get("distance") or 0)),
        )

    def _combat_preflight(
        self,
        tool: str,
        arguments: dict[str, Any],
        observation: dict[str, Any],
        goal: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not self._is_combat_start(tool, arguments):
            return []
        blockers: list[dict[str, Any]] = []
        if self._underworld(observation):
            blockers.append({"kind": "recover_from_underworld", "guidance": "escape through a functioning portal before combat"})
        health = self._vital_fraction(observation, "health")
        if health is not None and health < 1.0:
            blockers.append({"kind": "recover_health", "health_fraction": health, "guidance": "return to full health before combat"})
        mana = self._vital_fraction(observation, "mana")
        if mana is not None and mana < 1.0:
            blockers.append({"kind": "recover_mana", "mana_fraction": mana, "guidance": "return to full mana before a new hazardous encounter"})
        rested = deep_get(observation, "status.vitals.vigor.rested", deep_get(observation, "look.vitals.vigor.rested"))
        if rested is False:
            blockers.append({"kind": "recover_vigor", "guidance": "eat or rest until the game reports rested"})
        if tool == "autopilot" and arguments.get("mode") == "farm":
            vigor = deep_get(
                observation,
                "status.vitals.vigor.value",
                deep_get(
                    observation,
                    "status.vitals.vigor.current",
                    deep_get(observation, "look.vitals.vigor.value"),
                ),
            )
            fight_vigor = int(
                arguments.get("fight_above_vigor", FARM_FIGHT_VIGOR)
                or FARM_FIGHT_VIGOR
            )
            if isinstance(vigor, (int, float)) and vigor < fight_vigor:
                supply = self._combat_vigor_supply(observation)
                verified_food_shortfall = (
                    int(supply.get("vigor_points", 0) or 0)
                    < max(0, fight_vigor - int(vigor))
                    and int(supply.get("cookable_casts", 0) or 0) <= 0
                )
                if not supply["available"] or verified_food_shortfall:
                    blockers.append(
                        {
                            "kind": "recover_combat_vigor",
                            "vigor": vigor,
                            "minimum": fight_vigor,
                            "supply": supply,
                            "guidance": (
                                f"acquire enough edible food before launching the keeper so verified nutrition can raise vigor to at least {fight_vigor}; "
                                "sitting cannot do this because resting stops at 80 vigor. Carried herbs and elderberries "
                                "count only when the verified spell list contains Create Food"
                            ),
                        }
                    )
            assigned_room = arguments.get("assigned_room")
            current_room = deep_get(observation, "look.room.num")
            if (
                assigned_room is not None
                and current_room is not None
                and str(assigned_room) == str(current_room)
            ):
                live_threats = self._live_overlevel_hostiles(observation)
                if live_threats:
                    blockers.append(
                        {
                            "kind": "live_room_overlevel_hostile",
                            "assigned_room": assigned_room,
                            "hostiles": live_threats,
                            "guidance": (
                                "Do not start the farm while these live, non-generator threats are present. "
                                "Recover under survival control and scout a different grounded room before launching."
                            ),
                        }
                    )
            quarantines = self.storage.get_runtime("farm_tactic_quarantine_v1", {})
            quarantines = quarantines if isinstance(quarantines, dict) else {}
            quarantine = quarantines.get(str(assigned_room)) if assigned_room is not None else None
            if isinstance(quarantine, dict) and self._farm_quarantine_matches(
                quarantine, arguments
            ):
                blockers.append(
                    {
                        "kind": "quarantined_farm_tactic",
                        "assigned_room": assigned_room,
                        "guidance": quarantine.get("guidance")
                        or "choose a different grounded farm room; this room/prey tactic failed survivability",
                        "evidence": quarantine,
                    }
                )
            stagnations = self.storage.get_runtime("farm_tactic_stagnation_v1", {})
            stagnations = stagnations if isinstance(stagnations, dict) else {}
            stagnation_key = (
                f"{goal['id']}|{assigned_room}|{str(arguments.get('hunt') or '').strip().casefold()}"
            )
            stagnation = stagnations.get(stagnation_key)
            if isinstance(stagnation, dict):
                blockers.append(
                    {
                        "kind": "stagnated_farm_tactic",
                        "assigned_room": assigned_room,
                        "hunt": arguments.get("hunt"),
                        "guidance": stagnation.get("guidance")
                        or "query hunting_grounds once and choose a different grounded room; do not restart this stalled room/prey tactic unchanged",
                        "evidence": stagnation,
                    }
                )
            readiness = self.learning.readiness_summary(observation)
            if readiness.get("recent_combat_deaths") and int(readiness.get("healing_supply_count", 0) or 0) < 4:
                blockers.append(
                    {
                        "kind": "replenish_healing_supplies_after_death",
                        "healing_supply_count": readiness.get("healing_supply_count", 0),
                        "minimum": 4,
                        "guidance": "carry at least four verified healing flasks before another background farm",
                    }
                )
        if tool in PVP_TOOL_NAMES:
            if tool == PVP_SEEK_TOOL_NAME:
                route_failure = self.storage.get_runtime(
                    PVP_ROUTE_FAILURE_RUNTIME_KEY, {}
                )
                current_room = deep_get(
                    observation,
                    "look.room.num",
                    deep_get(observation, "look.room_id"),
                )
                if (
                    isinstance(route_failure, dict)
                    and str(route_failure.get("actual_room_id")) == str(current_room)
                    and route_failure.get("corpus_version") == self.knowledge.corpus_version
                ):
                    blockers.append(
                        {
                            "kind": "pvp_patrol_route_unavailable",
                            "room_id": current_room,
                            "failed_hop": route_failure.get("failed_hop"),
                            "reason": route_failure.get("reason"),
                            "guidance": (
                                "do not vary pvp_seek room pairs from this location: the prior patrol did not "
                                "complete because its shared route hop was unavailable. Relocate through a "
                                "verified working exit or wait for the route/map implementation to change"
                            ),
                        }
                    )
            maximum = deep_get(observation, "status.vitals.health.max", deep_get(observation, "look.vitals.health.max"))
            prior_pvp = self.storage.events(kinds=["pvp.engagement.completed"], limit=1)["events"]
            if isinstance(maximum, (int, float)) and maximum < 30 and not prior_pvp:
                blockers.append(
                    {
                        "kind": "new_player_pvp_protection",
                        "max_health": maximum,
                        "guidance": "progress to 30 max HP first unless fresh live evidence proves this server permits earlier PvP",
                    }
                )
        return blockers

    def _safety_preflight(
        self,
        tool: str,
        arguments: dict[str, Any],
        observation: dict[str, Any],
        goal: dict[str, Any],
    ) -> list[dict[str, Any]]:
        blockers = self._combat_preflight(tool, arguments, observation, goal)
        transition = self._foreground_room_transition(tool, arguments)
        if transition:
            farm_intent = self._effective_farm_intent(goal)
            assigned_room = farm_intent.get("assigned_room")
            destination_room_ids = transition.get("room_ids", [])
            enters_assigned_room = assigned_room is not None and any(
                str(room_id) == str(assigned_room) for room_id in destination_room_ids
            )
            if enters_assigned_room:
                blockers.append(
                    {
                        "kind": "keeper_owned_hazardous_travel",
                        "assigned_room": assigned_room,
                        "destination": transition,
                        "guidance": (
                            f"do not use foreground {tool} to enter assigned farm room {assigned_room}; "
                            "the executable tool arguments contradict the safe farm workflow. From a bank or inn, "
                            "start the bounded farm keeper and let it own the hazardous route and combat"
                        ),
                    }
                )

        if tool == "bank":
            room_name = str(deep_get(observation, "look.room.name", ""))
            if "bank" not in room_name.casefold():
                blockers.append(
                    {
                        "kind": "bank_location_required",
                        "room": room_name,
                        "guidance": "travel to a verified bank room before calling bank; never invoke a mutation merely to make it fail",
                    }
                )
        if tool == "bank" and arguments.get("action") == "deposit":
            goal_text = " ".join(
                (
                    str(goal.get("title", "")),
                    str(goal.get("objective", "")),
                    str(deep_get(goal, "constraints.operator_notes", "")),
                )
            ).casefold()
            shopping_goal = any(
                marker in goal_text
                for marker in ("buy", "purchase", "shopping", "merchant", "shop", "acquire")
            )
            if shopping_goal:
                completion = self.criteria.evaluate(goal, observation)
                result_by_id = {
                    str(item.get("id")): item
                    for item in completion.get("criteria", [])
                    if isinstance(item, dict)
                }
                unmet_inventory = []
                for index, criterion in enumerate(goal.get("success_criteria", [])):
                    if not isinstance(criterion, dict) or criterion.get("kind") != "inventory_contains":
                        continue
                    criterion_id = str(criterion.get("id") or f"criterion_{index + 1}")
                    if result_by_id.get(criterion_id, {}).get("met") is not True:
                        unmet_inventory.append(criterion.get("item"))
                if unmet_inventory:
                    blockers.append(
                        {
                            "kind": "retain_purchase_funds",
                            "unmet_inventory": unmet_inventory,
                            "guidance": "keep the money needed for the current safe shopping goal; deposit only after the purchase or before a genuinely hazardous phase",
                        }
                    )
        return blockers

    def _foreground_room_transition(
        self, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Ground a planner-owned destination against room ids and spawn facts.

        This check deliberately runs above the broker.  It prevents a model
        response whose prose says "bank first" while its executable arguments
        say ``travel(to=<farm room>)`` from moving the character even once.
        """
        if tool not in FOREGROUND_ROOM_TRANSITION_TOOLS or arguments.get("to") is None:
            return None
        raw_destination = arguments.get("to")
        room_ids: list[Any] = []
        if isinstance(raw_destination, (int, float)) and not isinstance(
            raw_destination, bool
        ):
            room_ids.append(int(raw_destination))
        elif isinstance(raw_destination, str) and raw_destination.strip().isdigit():
            room_ids.append(int(raw_destination.strip()))

        grounded_rooms: list[dict[str, Any]] = []
        hazardous = False
        try:
            resolved = self.knowledge.resolve(
                str(raw_destination), kinds=["location"], allow_fuzzy=False
            )
            candidates = []
            if isinstance(resolved.get("entity"), dict):
                candidates.append(resolved["entity"])
            for candidate in resolved.get("matches", []):
                if isinstance(candidate, dict) and candidate not in candidates:
                    candidates.append(candidate)
            for candidate in candidates:
                facts = candidate.get("facts") if isinstance(candidate.get("facts"), dict) else {}
                room_id = facts.get("room_id")
                if room_id is not None and room_id not in room_ids:
                    room_ids.append(room_id)
                detail = self.knowledge.get(str(candidate.get("id") or ""))
                entity = detail.get("entity") if isinstance(detail.get("entity"), dict) else {}
                spawn_table = (
                    entity.get("spawn_table")
                    if isinstance(entity.get("spawn_table"), dict)
                    else {}
                )
                hostile_spawns = [
                    spawn
                    for spawn in spawn_table.get("spawns", [])
                    if isinstance(spawn, dict) and self._spawn_is_hostile(spawn)
                ]
                if hostile_spawns:
                    hazardous = True
                grounded_rooms.append(
                    {
                        "room_id": room_id,
                        "name": candidate.get("canonical_name"),
                        "hostile_spawn_count": len(hostile_spawns),
                    }
                )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            # Knowledge is decision support, not a new availability dependency.
            # Exact numeric keeper ownership remains enforceable when it is down.
            pass
        return {
            "requested": raw_destination,
            "room_ids": room_ids,
            "hazardous": hazardous,
            "grounded_rooms": grounded_rooms,
        }

    @staticmethod
    def _spawn_is_hostile(spawn: dict[str, Any]) -> bool:
        """Classify static spawns by source role, never by character level.

        Merchants, teachers, and other NPCs have levels in the compendium. A
        room is statically hazardous only when its source record identifies an
        actual monster; live aggression remains authoritative for surprises.
        """
        return str(spawn.get("role") or "").strip().casefold() == "monster"

    @staticmethod
    def _is_combat_start(tool: str, arguments: dict[str, Any]) -> bool:
        return tool in {"fight", "attack", *PVP_TOOL_NAMES} or (
            tool == "autopilot"
            and arguments.get("action") == "start"
            and arguments.get("mode") == "farm"
        )

    def _begin_foreground_action(
        self, tool: str, *, goal_id: str | None = None
    ) -> None:
        self._foreground_action = {
            "tool": tool,
            "goal_id": goal_id,
            "started_at": timestamp(),
        }
        self._game_action_active.set()

    def _end_foreground_action(self) -> None:
        self._game_action_active.clear()
        self._foreground_action = None

    def _reconcile_after_action_error(
        self,
        *,
        error: str,
    ) -> dict[str, Any] | None:
        try:
            self._begin_foreground_action("reconcile_after_action_error")
            if "not in game" in error.casefold() and self.config.game.autojoin:
                self.broker.ensure_joined()
            observation = self.broker.observe()
            self.last_observation = observation
            self.storage.record_snapshot(redact(observation))
            self.dependencies["broker"] = "healthy"
            return observation
        except BrokerError as exc:
            self.warnings = [*self.warnings[-9:], f"action reconciliation: {str(exc)[:300]}"]
            return None
        finally:
            self._end_foreground_action()

    def _record_death(
        self,
        goal: dict[str, Any],
        *,
        tool: str,
        arguments: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
        result: Any = None,
        error: str | None = None,
        correlation_id: str | None = None,
        policy_decision_id: str | None = None,
    ) -> dict[str, Any]:
        combat = self.learning.record_combat_outcome(
            tool=tool,
            arguments=arguments,
            before=before,
            result=result,
            after=after,
            error=error,
            died=True,
        )
        event = self.storage.emit_event(
            "character.died",
            f"The character died during {tool}",
            severity="critical",
            interesting=True,
            goal_id=goal["id"],
            data={
                "tool": tool,
                "target": arguments.get("target"),
                "before": {
                    "room": redact(deep_get(before, "look.room")),
                    "vitals": redact(deep_get(before, "status.vitals", deep_get(before, "look.vitals", {}))),
                    "inventory": redact(deep_get(before, "inventory.items", [])),
                },
                "after": {
                    "room": redact(deep_get(after, "look.room")),
                    "vitals": redact(deep_get(after, "status.vitals", deep_get(after, "look.vitals", {}))),
                    "inventory": redact(deep_get(after, "inventory.items", [])),
                },
                "broker_result": redact(result),
                "broker_error": str(error or "")[:500] or None,
                "combat_memory": combat,
            },
            correlation_id=correlation_id,
            policy_decision_id=policy_decision_id,
        )
        deferred = self.learning.defer_goal(
            goal,
            after,
            tool=tool,
            arguments=arguments,
            reason="Observed death shows that the active goal exceeds current verified combat readiness",
            event_kind="character.died",
            evidence_event_ids=[event["id"]],
            classification="insufficient_combat_power",
            scope="goal",
        )
        return {"event": event, **deferred}

    def submit_goal(self, payload: dict[str, Any]) -> dict[str, Any]:
        onboarding = self._onboarding_status(self.last_observation or {})
        if not onboarding.get("ready_for_goals"):
            raise OnboardingRequired(
                f"character onboarding is {onboarding.get('status')}; "
                f"{onboarding.get('next_action')}"
            )
        grounding = self.knowledge.require_valid_goal(payload)
        canonical = grounding["canonical_goal"]
        replay = self.storage.idempotent_result(str(canonical.get("request_id", "")), "submit_goal", canonical)
        if replay is not None:
            result = replay
        else:
            family = self.learning.goal_family(canonical)
            existing = next(
                (
                    goal
                    for status in ("active", "queued", "paused")
                    for goal in self.storage.goals([status])
                    if self.learning.goal_family(goal) == family
                ),
                None,
            )
            if existing is not None:
                result = {
                    "goal": existing,
                    "queue_position": None,
                    "deduplicated": True,
                    "warnings": [
                        {
                            "code": "GOAL_ALREADY_IN_PROGRESS",
                            "message": (
                                f"An equivalent goal is already {existing['status']}; supervise or resume it "
                                "instead of creating another retry."
                            ),
                            "goal_id": existing["id"],
                            "goal_family": family,
                        }
                    ],
                }
            else:
                review = self.learning.require_goal_eligible(canonical, self.last_observation or {})
                result = self.storage.submit_goal(
                    canonical,
                    retry_of_goal_id=review.get("retry_of_goal_id"),
                    preserve_replaced_active=True,
                )
                if review.get("lesson_id"):
                    self.storage.mark_retry_started(review["lesson_id"], result["goal"]["id"])
        result["grounding"] = {
            "corpus": grounding["corpus"],
            "resolved_entities": grounding["resolved_entities"],
            "warnings": grounding["warnings"],
        }
        return result

    def persona(self) -> dict[str, Any]:
        """Return the active persona together with its character-setup state."""

        return {
            **self.storage.persona(),
            "onboarding": self._onboarding_status(self.last_observation or {}),
        }

    def set_persona(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a persona and request goal-independent character onboarding."""

        allowed = {
            "request_id",
            "expected_version",
            "persona",
            "replace_existing_character",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unknown persona fields: {', '.join(unknown)}")
        replace_existing = payload.get("replace_existing_character", False)
        if not isinstance(replace_existing, bool):
            raise ValueError("replace_existing_character must be a boolean")
        stored = self.storage.set_persona(
            {
                key: value
                for key, value in payload.items()
                if key
                in {
                    "request_id",
                    "expected_version",
                    "persona",
                }
            }
        )
        if self.config.onboarding.enabled and self.config.onboarding.create_from_persona:
            previous = self.storage.get_runtime(ONBOARDING_RUNTIME_KEY, {})
            request_id = str(payload.get("request_id", ""))
            already_recorded = bool(
                isinstance(previous, dict)
                and previous.get("request_id") == request_id
                and int(previous.get("persona_version", 0)) == int(stored["version"])
            )
            if not already_recorded:
                state = {
                    "status": "pending",
                    "request_id": request_id,
                    "persona_version": stored["version"],
                    "desired_name": stored["name"],
                    "replace_existing_character": replace_existing,
                    "requested_at": timestamp(),
                }
                self.storage.set_runtime(ONBOARDING_RUNTIME_KEY, state)
                self.storage.emit_event(
                    "onboarding.requested",
                    f"Character onboarding requested for {stored['name']}",
                    severity="notice",
                    interesting=True,
                    data={
                        "persona_version": stored["version"],
                        "desired_name": stored["name"],
                        "replace_existing_character": replace_existing,
                    },
                )
        return {**stored, "onboarding": self._onboarding_status(self.last_observation or {})}

    def _onboarding_status(
        self, observation: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Project the durable setup state without performing game mutations."""

        if not self.config.onboarding.enabled:
            return {
                "status": "disabled",
                "ready_for_goals": True,
                "next_action": "Submit a goal when ready.",
            }
        persona = self.storage.persona()
        if int(persona.get("version", 0)) == 0:
            return {
                "status": "awaiting_persona",
                "ready_for_goals": False,
                "next_action": (
                    "Set the character name and complete persona through the persona tool."
                ),
            }
        state = self.storage.get_runtime(ONBOARDING_RUNTIME_KEY, {})
        state = dict(state) if isinstance(state, dict) else {}
        current_name = str(
            self._character_name(observation or self.last_observation or {}) or ""
        ).strip()
        desired_name = str(persona.get("name") or state.get("desired_name") or "").strip()
        if current_name and desired_name and current_name.casefold() == desired_name.casefold():
            return {
                **state,
                "status": "ready",
                "ready_for_goals": True,
                "desired_name": desired_name,
                "current_name": current_name,
                "next_action": "Submit the first strategic goal.",
            }
        status = str(state.get("status") or "pending")
        next_actions = {
            "pending": "The configured LLM will choose and create the character build.",
            "planning": "The configured LLM is choosing the character build.",
            "creating": "The controller is creating and verifying the character.",
            "awaiting_existing_character_confirmation": (
                "Set the persona again with replace_existing_character=true to replace the established character."
            ),
            "failed": (
                "Review the onboarding error, then set the persona again with a new request_id to retry."
            ),
        }
        return {
            **state,
            "status": status,
            "ready_for_goals": False,
            "desired_name": desired_name,
            "current_name": current_name or None,
            "next_action": next_actions.get(
                status, "Wait for the controller to complete character onboarding."
            ),
        }

    def _set_onboarding_state(self, **updates: Any) -> dict[str, Any]:
        current = self.storage.get_runtime(ONBOARDING_RUNTIME_KEY, {})
        value = dict(current) if isinstance(current, dict) else {}
        value.update(updates)
        value["updated_at"] = timestamp()
        self.storage.set_runtime(ONBOARDING_RUNTIME_KEY, value)
        return value

    def _onboarding_turn(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Create the persona-named character before accepting ordinary goals."""

        status = self._onboarding_status(observation)
        if status.get("ready_for_goals") or status.get("status") in {
            "awaiting_persona",
            "awaiting_existing_character_confirmation",
            "failed",
        }:
            return status

        persona_record = self.storage.persona()
        desired_name = str(persona_record.get("name") or "").strip()
        current_name = str(self._character_name(observation) or "").strip()
        state = self.storage.get_runtime(ONBOARDING_RUNTIME_KEY, {})
        state = dict(state) if isinstance(state, dict) else {}
        generated_placeholder = bool(
            current_name and GENERATED_CHARACTER_NAME_RE.fullmatch(current_name)
        )
        may_replace = bool(state.get("replace_existing_character")) or generated_placeholder
        if (
            current_name
            and current_name.casefold() != desired_name.casefold()
            and self.config.onboarding.preserve_existing_character
            and not may_replace
        ):
            first_notice = state.get("status") != "awaiting_existing_character_confirmation"
            state = self._set_onboarding_state(
                status="awaiting_existing_character_confirmation",
                current_name=current_name,
            )
            if first_notice:
                self.storage.emit_event(
                    "onboarding.existing_character_preserved",
                    f"Preserved established character {current_name} during onboarding",
                    severity="warning",
                    interesting=True,
                    data={"current_name": current_name, "desired_name": desired_name},
                )
            return self._onboarding_status(observation)

        capabilities = self.broker.capabilities()
        if "reroll" not in capabilities:
            self._set_onboarding_state(
                status="failed",
                error="the pinned harness does not expose the reroll capability",
            )
            return self._onboarding_status(observation)

        self._set_onboarding_state(status="planning", current_name=current_name or None)
        choice = self.model.plan_character(
            persona=persona_record,
            current_character={
                "name": current_name or None,
                "generated_placeholder": generated_placeholder,
                "vitals": redact(deep_get(observation, "status.vitals", {})),
            },
        )
        self.dependencies["model"] = "healthy"
        arguments = {
            "action": "reroll",
            "agent": self.config.game.agent,
            "name": desired_name,
            "stats": choice["stats"],
            "loadout": choice["loadout"],
        }
        preview = self.broker.call_tool(
            "reroll",
            {**arguments, "action": "plan"},
            timeout=30,
            mutation=False,
        )
        if not isinstance(preview, dict) or preview.get("ok") is not True:
            self._set_onboarding_state(
                status="failed",
                build=choice,
                error="the harness rejected the character creation plan",
                preview=redact(preview),
            )
            self.storage.emit_event(
                "onboarding.character_plan_rejected",
                "Harness rejected the LLM-selected character build",
                severity="warning",
                interesting=True,
                data={"build": choice, "preview": redact(preview)},
            )
            return self._onboarding_status(observation)

        synthetic_goal = {
            "id": None,
            "title": f"Onboard character {desired_name}",
            "objective": "Create the persona-defined character before goal-driven play.",
        }
        decision = self.policy.evaluate(
            "reroll",
            arguments,
            observation,
            synthetic_goal,
            known_tools=set(capabilities),
        )
        if decision.decision == "deny":
            self._set_onboarding_state(status="failed", error=decision.summary)
            return self._onboarding_status(observation)

        correlation_id = uuid7()
        attempt_id = self.storage.create_action_attempt(
            None,
            observation.get("id"),
            "reroll",
            arguments,
            choice["rationale"],
            decision.id,
            correlation_id,
        )
        assessment_id: str | None = None
        if decision.decision == "allow_with_caution":
            assessment = self.policy.consequence_assessment(
                decision, synthetic_goal, choice["rationale"]
            )
            assessment["action_attempt_id"] = attempt_id
            assessment_id = self.storage.record_consequence(assessment)["id"]

        self._set_onboarding_state(status="creating", build=choice, attempt_id=attempt_id)
        self.storage.update_action_attempt(attempt_id, "sent")
        self.storage.emit_event(
            "onboarding.character_creation.started",
            f"Creating persona-defined character {desired_name}",
            severity="notice",
            interesting=True,
            data={"build": choice, "replacing": current_name or None},
            correlation_id=correlation_id,
            policy_decision_id=decision.id,
        )
        try:
            result = self.broker.call_tool(
                "reroll",
                {**arguments, "confirm": True},
                timeout=max(90, self.config.model.planner_timeout_seconds),
                mutation=True,
            )
            after = self.broker.observe()
            self.last_observation = after
            created_name = str(self._character_name(after) or "").strip()
            verified = bool(
                isinstance(result, dict)
                and result.get("done") is True
                and created_name.casefold() == desired_name.casefold()
            )
            self.storage.update_action_attempt(
                attempt_id,
                "succeeded" if verified else "failed",
                result={"broker": redact(result), "created_name": created_name},
                error_code=None if verified else "ONBOARDING_VERIFICATION_FAILED",
            )
            if assessment_id:
                self.storage.complete_consequence(
                    assessment_id,
                    outcome={"created_name": created_name, "verified": verified},
                    succeeded=verified,
                )
            if not verified:
                self._set_onboarding_state(
                    status="failed",
                    error="character creation did not return the requested verified name",
                    result=redact(result),
                    created_name=created_name or None,
                )
                return self._onboarding_status(after)
            self._set_fallback()
            self._set_onboarding_state(
                status="ready",
                completed_at=timestamp(),
                current_name=created_name,
                result={"done": True, "stats_as_asked": result.get("stats_as_asked")},
            )
            self.storage.emit_event(
                "onboarding.completed",
                f"Character {created_name} is ready for strategic goals",
                severity="notice",
                interesting=True,
                data={"persona_version": persona_record["version"], "build": choice},
                correlation_id=correlation_id,
                policy_decision_id=decision.id,
            )
            return self._onboarding_status(after)
        except (BrokerError, ValueError) as exc:
            self.storage.update_action_attempt(
                attempt_id,
                "failed",
                result={"error": str(exc)[:500]},
                error_code=getattr(exc, "code", "ONBOARDING_FAILED"),
            )
            if assessment_id:
                self.storage.complete_consequence(
                    assessment_id,
                    outcome={"error": str(exc)[:500]},
                    succeeded=False,
                )
            self._set_onboarding_state(status="failed", error=str(exc)[:500])
            raise

    def manage_goal(self, payload: dict[str, Any]) -> dict[str, Any]:
        goal = self.storage.goal(str(payload.get("goal_id", "")))
        if goal is None:
            # Preserve the storage layer's stable NOT_FOUND response.
            return self.storage.manage_goal(payload)
        assessment: dict[str, Any] | None = None
        if payload.get("action") == "cancel" and goal.get("status") == "active":
            assessment = self._active_goal_cancellation_assessment(goal, payload)
            if not assessment["allowed"]:
                self.storage.emit_event(
                    "goal.cancellation.guarded",
                    f"Protected active goal from premature cancellation: {goal['title']}",
                    severity="warning",
                    interesting=False,
                    goal_id=goal["id"],
                    data=assessment,
                )
                raise InvalidTransition(
                    "GOAL_COMMITMENT_GUARD: active goal cancellation was refused because the goal is fresh or "
                    "still making verified progress. Pause it for reversible replanning, or provide a controller-"
                    "verifiable cause (safety, invalid, durably_stalled, opportunity_ended); use operator_requested only when the "
                    "human explicitly asked to cancel. "
                    + str(assessment.get("detail") or "")
                )
        result = self.storage.manage_goal(payload)
        resulting_goal = result.get("goal") if isinstance(result, dict) else None
        if payload.get("action") == "resume":
            # Resume is an explicit request to try the strategic outcome again,
            # not to replay the tactical plan and feedback that led to the
            # pause/block. A fresh or preserved campaign phase will require a
            # newly verified plan against current state on its next turn.
            self._invalidate_execution_plan(
                goal, "goal resumed; require a fresh tactical plan from current state"
            )
            if self._planner_feedback(goal) is not None:
                self._clear_planner_feedback()
        if isinstance(resulting_goal, dict) and resulting_goal.get("status") == "cancelled":
            self.storage.complete_campaign_run(resulting_goal["id"], status="cancelled")
        if assessment is not None:
            result["cancellation_assessment"] = assessment
        return result

    def _active_goal_cancellation_assessment(
        self, goal: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        cause = str(payload.get("cause") or "").strip()
        valid_causes = {
            "operator_requested",
            "safety",
            "invalid",
            "durably_stalled",
            "superseded",
            "opportunity_ended",
        }
        if cause and cause not in valid_causes:
            raise ValueError(f"unknown cancellation cause: {cause}")
        age = self._age_seconds(goal.get("activated_at") or goal.get("created_at")) or 0.0
        liveness = self._compact_liveness(goal)
        since_progress = liveness.get("seconds_since_verified_progress")
        since_action = liveness.get("seconds_since_successful_action")
        since_progress = age if since_progress is None else float(since_progress)
        since_action = age if since_action is None else float(since_action)
        committed = age >= self.config.controller.minimum_goal_commitment_seconds
        quiet_long_enough = min(since_progress, since_action) >= self.config.controller.minimum_stall_seconds
        verified_stall = liveness.get("state") == "stalled" or (committed and quiet_long_enough)
        observation = self.last_observation or {}
        safety_verified = self._underworld(observation) or self._risk(observation) == "critical"
        validation = self.knowledge.validate_goal(
            {
                "title": goal.get("title"),
                "objective": goal.get("objective"),
                "success_criteria": goal.get("success_criteria"),
                "constraints": goal.get("constraints", {}),
                "priority": goal.get("priority", 50),
                "activation": "queue",
            }
        )
        invalid_verified = validation.get("valid") is False
        superseded_verified = committed and bool(self.storage.goals(["queued"]))
        direct_pvp = self._direct_pvp_contract(goal)
        direct_completion = self.criteria.evaluate(goal, observation)
        observation_age = self._age_seconds(observation.get("observed_at"))
        opportunity_ended_verified = bool(
            direct_pvp
            and direct_pvp.get("cancel_if_absent") is True
            and not self._pvp_phase_criterion_met(goal, direct_completion)
            and not self._visible_player_matches(observation, str(direct_pvp["target"]))
            and observation_age is not None
            and observation_age <= 30.0
        )
        allowed = (
            cause == "operator_requested"
            or (cause == "safety" and safety_verified)
            or (cause == "invalid" and invalid_verified)
            or (cause in {"", "durably_stalled"} and verified_stall)
            or (cause == "superseded" and superseded_verified)
            or (cause == "opportunity_ended" and opportunity_ended_verified)
        )
        return {
            "allowed": allowed,
            "cause": cause or None,
            "goal_age_seconds": round(age, 1),
            "minimum_commitment_seconds": self.config.controller.minimum_goal_commitment_seconds,
            "minimum_stall_seconds": self.config.controller.minimum_stall_seconds,
            "seconds_since_verified_progress": round(since_progress, 1),
            "seconds_since_successful_action": round(since_action, 1),
            "liveness_state": liveness.get("state"),
            "verified": {
                "durably_stalled": verified_stall,
                "safety": safety_verified,
                "invalid": invalid_verified,
                "superseded": superseded_verified,
                "opportunity_ended": opportunity_ended_verified,
            },
            "detail": (
                "Current liveness is not a verified durable stall; elapsed criterion time alone is not enough."
                if not allowed
                else "Cancellation cause passed the controller commitment guard."
            ),
        }

    def decide_proposal(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("request_id", ""))
        if request_id and payload.get("action") in {"accept", "reject"}:
            replay = self.storage.idempotent_result(request_id, "decide_proposal", payload)
            if replay is not None:
                return replay
        review: dict[str, Any] = {"allowed": True}
        if payload.get("action") == "accept":
            onboarding = self._onboarding_status(self.last_observation or {})
            if not onboarding.get("ready_for_goals"):
                raise OnboardingRequired(
                    f"character onboarding is {onboarding.get('status')}; "
                    f"{onboarding.get('next_action')}"
                )
            proposal = self.storage.proposal(str(payload.get("proposal_id", "")))
            if proposal:
                grounding = self.knowledge.require_valid_goal(proposal.get("goal_draft", {}))
                review = self.learning.require_goal_eligible(grounding["canonical_goal"], self.last_observation or {})
        result = self.storage.decide_proposal(payload, retry_of_goal_id=review.get("retry_of_goal_id"))
        if review.get("lesson_id") and result.get("goal"):
            self.storage.mark_retry_started(review["lesson_id"], result["goal"]["id"])
        return result

    def validate_goal(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.knowledge.validate_goal(payload)

    def progression_context(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        detail = str(payload.get("detail", "compact"))
        if detail not in {"compact", "full"}:
            raise ValueError("detail must be compact or full")
        state = payload.get("character_state") if isinstance(payload.get("character_state"), dict) else (self.last_observation or {})
        result = self.knowledge.progression_context(state, limit=int(payload.get("limit", 8)))
        result["combat_readiness"] = self.learning.readiness_summary(state)
        result["empirical_combat_history"] = self.learning.combat_summary(state, limit=8)
        if not self.offline_diagnostics and self.dependencies.get("broker") == "healthy":
            level = result.get("character", {}).get("max_health")
            capabilities = self.broker.capabilities()
            live_warnings: list[str] = []

            def add_live_result(key: str, tool: str, arguments: dict[str, Any]) -> None:
                try:
                    result[key] = self.broker.call_tool(tool, arguments, timeout=15)
                except (BrokerError, ValueError) as exc:
                    live_warnings.append(f"{tool}: {str(exc)[:300]}")

            if "abilities" in capabilities:
                add_live_result(
                    "live_abilities",
                    "abilities",
                    {"agent": self.config.game.agent, "known_only": True},
                )
            if "spells" in capabilities:
                add_live_result(
                    "live_spell_readiness",
                    "spells",
                    {"agent": self.config.game.agent},
                )
            if level is not None:
                advancement_tool = "progress" if "progress" in capabilities else "advancement"
                if advancement_tool in capabilities:
                    add_live_result(
                        "live_advancement",
                        advancement_tool,
                        {"agent": self.config.game.agent},
                    )
                if "hunting_grounds" in capabilities:
                    grounds: dict[str, Any] = {
                        "for_level": level,
                        "limit": min(12, max(1, int(payload.get("limit", 8)))),
                    }
                    if payload.get("karma") in {"evil", "good", "neutral"}:
                        grounds["karma"] = payload["karma"]
                    add_live_result(
                        "live_hunting_grounds",
                        "hunting_grounds",
                        grounds,
                    )
            if "prey" in capabilities:
                prey: dict[str, Any] = {
                    "agent": self.config.game.agent,
                    "purpose": "advance",
                    "goals": [{"kind": "hp"}],
                    "limit": min(12, max(1, int(payload.get("limit", 8)))),
                }
                if payload.get("karma") in {"evil", "good", "neutral"}:
                    prey["karma"] = payload["karma"]
                add_live_result("live_prey", "prey", prey)
            if live_warnings:
                result["live_warning"] = "; ".join(live_warnings)[:500]
        return result if detail == "full" else self._compact_progression_context(result, state)

    def _compact_progression_context(
        self, result: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, Any]:
        candidates = []
        for item in result.get("candidates", []):
            if not isinstance(item, dict):
                continue
            evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
            candidates.append(
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "name",
                        "level",
                        "level_above",
                        "karma",
                        "difficulty",
                        "vulnerabilities",
                        "resistances",
                        "attack_type",
                        "locations",
                    )
                }
                | {
                    "evidence": {
                        "source_tier": evidence.get("source_tier"),
                        "corpus_version": evidence.get("corpus_version"),
                    }
                }
            )

        room_options = []
        for option in result.get("room_options_by_candidate", []):
            if not isinstance(option, dict):
                continue
            target = option.get("target") if isinstance(option.get("target"), dict) else {}
            target_facts = target.get("facts") if isinstance(target.get("facts"), dict) else {}
            rooms = []
            for room in option.get("rooms", []):
                if not isinstance(room, dict):
                    continue
                safe = room.get("safe_spot_evidence") if isinstance(room.get("safe_spot_evidence"), dict) else {}
                rooms.append(
                    {
                        key: room.get(key)
                        for key in (
                            "room",
                            "generator_chance_total",
                            "population_cap",
                            "target",
                            "target_level",
                            "target_chance",
                            "target_how",
                            "target_eligible_for_hp",
                            "preferred",
                        )
                    }
                    | {
                        # The complete mix is the safety-critical part. Omit only
                        # repeated citations; the corpus version remains above.
                        "spawns": [
                            {
                                key: spawn.get(key)
                                for key in (
                                    "creature_id",
                                    "creature",
                                    "level",
                                    "difficulty",
                                    "karma",
                                    "role",
                                    "faction",
                                    "chance",
                                    "cap",
                                    "how",
                                )
                            }
                            for spawn in room.get("spawns", [])
                            if isinstance(spawn, dict)
                        ],
                        "safe_spot_evidence": {
                            key: safe.get(key)
                            for key in (
                                "tested_squares",
                                "proven_clean_squares",
                                "discredited_squares",
                                "clean_hold_seconds",
                            )
                        }
                        | {
                            "best_clean_spots": [
                                {
                                    key: spot.get(key)
                                    for key in (
                                        "col",
                                        "row",
                                        "held",
                                        "failed",
                                        "held_seconds",
                                        "most_attackers",
                                    )
                                }
                                for spot in safe.get("best_clean_spots", [])[:2]
                                if isinstance(spot, dict)
                            ]
                        },
                    }
                )
            room_options.append(
                {
                    "status": option.get("status"),
                    "target": {
                        "id": target.get("id"),
                        "canonical_name": target.get("canonical_name"),
                        "facts": target_facts,
                    },
                    "rooms": rooms,
                    "selection_note": option.get("selection_note"),
                }
            )

        history = (
            result.get("empirical_combat_history")
            if isinstance(result.get("empirical_combat_history"), dict)
            else {}
        )
        compact_history = {
            "by_target": history.get("by_target", [])[:8],
            "recent": [
                {
                    key: item.get(key)
                    for key in (
                        "occurred_at",
                        "target",
                        "outcome",
                        "died",
                        "room",
                        "health_after",
                    )
                }
                for item in history.get("recent", [])[-4:]
                if isinstance(item, dict)
            ],
            "total_deaths": history.get("total_deaths"),
        }
        compact = {
            key: result.get(key)
            for key in (
                "character",
                "new_player_doctrine",
                "verified_rule",
                "guidance",
                "corpus",
                "live_advancement",
                "live_hunting_grounds",
                "live_prey",
                "live_warning",
            )
            if key in result
        }
        compact.update(
            {
                "detail": "compact",
                "candidates": candidates,
                "room_options_by_candidate": room_options,
                "combat_readiness": self._compact_readiness(state),
                "empirical_combat_history": compact_history,
            }
        )
        if "live_abilities" in result or "live_spell_readiness" in result:
            compact["live_development"] = self._compact_character_development(
                {
                    "abilities": result.get("live_abilities"),
                    "spells": result.get("live_spell_readiness"),
                }
            )
        return compact

    def run_forever(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            self.last_heartbeat_at = timestamp()
            try:
                self.turn()
                backoff = 1.0
                onboarding_ready = self._onboarding_status().get("ready_for_goals")
                cadence = (
                    self.config.controller.active_cadence_seconds
                    if self.storage.active_goal() or onboarding_ready is False
                    else self.config.controller.idle_cadence_seconds
                )
            except BrokerError as exc:
                self.dependencies["broker"] = "unhealthy"
                self._degrade("broker", exc)
                try:
                    self.broker.ensure_started()
                    self.broker.capabilities(refresh=True)
                    if self.config.game.autojoin:
                        self.broker.ensure_joined()
                    self._set_fallback()
                    if self.config.controller.conversation_enabled:
                        self._start_conversation_listener()
                        self._start_social_worker()
                    self.dependencies["broker"] = "healthy"
                except BrokerError:
                    pass
                cadence = backoff
                backoff = min(backoff * 2, self.config.controller.error_backoff_max_seconds)
            except ModelError as exc:
                self.dependencies["model"] = "unhealthy"
                self._degrade("model", exc)
                cadence = backoff
                backoff = min(backoff * 2, self.config.controller.error_backoff_max_seconds)
            except Exception as exc:
                LOG.exception("controller turn failed")
                self._degrade("controller", exc)
                cadence = backoff
                backoff = min(backoff * 2, self.config.controller.error_backoff_max_seconds)
            else:
                if self.dependencies.get("broker") == "healthy" and self.dependencies.get("model") != "unhealthy":
                    self.state = "running"
                    self.warnings = []
                    self._active_degradations.clear()
            self.stop_event.wait(cadence)
        self.state = "stopped"

    @staticmethod
    def _health_progress_criteria(
        goal: dict[str, Any],
        completion: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Return max-health criteria paired with their latest verifier result."""
        results = {
            str(item.get("id")): item
            for item in completion.get("criteria", [])
            if isinstance(item, dict) and item.get("id") is not None
        }
        matched: list[dict[str, Any]] = []
        for index, criterion in enumerate(goal.get("success_criteria", [])):
            if not isinstance(criterion, dict) or criterion.get("kind") != "numeric_threshold":
                continue
            if str(criterion.get("metric", "")) != "status.vitals.health.max":
                continue
            criterion_id = str(criterion.get("id") or f"criterion_{index + 1}")
            matched.append({"criterion": criterion, "result": results.get(criterion_id, {})})
        return matched

    @staticmethod
    def _farm_counter(status: dict[str, Any], name: str) -> int:
        value = deep_get(status, f"did.{name}", 0)
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _farm_room(status: dict[str, Any], observation: dict[str, Any]) -> Any:
        return (
            deep_get(observation, "look.room.num")
            or deep_get(observation, "look.room.name")
            or deep_get(status, "placement.assigned_room")
            or deep_get(status, "policy.assignedRoom")
            or deep_get(status, "policy.assigned_room")
        )

    @staticmethod
    def _farm_assigned_room(status: dict[str, Any]) -> Any:
        return (
            deep_get(status, "placement.assigned_room")
            or deep_get(status, "policy.assignedRoom")
            or deep_get(status, "policy.assigned_room")
        )

    @staticmethod
    def _farm_target(status: dict[str, Any]) -> str:
        return str(deep_get(status, "policy.hunt") or status.get("hunting") or "unknown")

    @staticmethod
    def _goal_farm_intent(goal: dict[str, Any]) -> dict[str, Any]:
        """Recover the deliberately structured farm signature from operator notes.

        Runtime ownership is authoritative for launches made by this controller.
        The note fallback lets a newly deployed controller reject a legacy keeper
        job instead of silently adopting it for a different active goal.
        """
        constraints = goal.get("constraints", {})
        notes = str(constraints.get("operator_notes") or "") if isinstance(constraints, dict) else ""
        room_match = re.search(r"\bassigned_room\s*=\s*(\d+)\b", notes, re.IGNORECASE)
        hunt_match = re.search(
            r"\bhunt\s*=\s*[\"']?([a-z][a-z ]*?)(?=[\"',;]|\s+(?:assigned_room|max_carry|use_safe_spots|flee_below|hold_resume_above|rest_below|fight_above_vigor|bank_above|pull_within|break_out_via_logoff)\s*=|$)",
            notes,
            re.IGNORECASE,
        )
        intent: dict[str, Any] = {
            "assigned_room": int(room_match.group(1)) if room_match else None,
            "hunt": " ".join(hunt_match.group(1).casefold().split()) if hunt_match else None,
        }
        for field in ("use_safe_spots", "break_out_via_logoff"):
            match = re.search(
                rf"\b{field}\s*=\s*(true|false)\b", notes, re.IGNORECASE
            )
            intent[field] = (
                match.group(1).casefold() == "true" if match else None
            )
        for field, converter in (
            ("max_carry", int),
            ("flee_below", float),
            ("hold_resume_above", float),
            ("rest_below", float),
            ("fight_above_vigor", int),
            ("bank_above", int),
            ("pull_within", int),
        ):
            match = re.search(
                rf"\b{field}\s*(?:>=|<=|=)\s*(\d+(?:\.\d+)?)\b",
                notes,
                re.IGNORECASE,
            )
            intent[field] = converter(float(match.group(1))) if match else None
        return intent

    @staticmethod
    def _campaign_phase_farm_intent(
        phase: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Read the executable farm signature from a durable internal phase.

        The campaign manager owns long-running tactical decomposition for a
        strategic supervisor goal. New phases carry typed fields in ``context``;
        the short prose fallback keeps phases persisted by older prompts
        executable across an upgrade.
        """
        if not isinstance(phase, dict) or phase.get("kind") != "farm":
            return {}
        context = phase.get("context")
        context = context if isinstance(context, dict) else {}
        intent: dict[str, Any] = {
            "assigned_room": context.get(
                "assigned_room", context.get("room", context.get("room_id"))
            ),
            "hunt": context.get("hunt", context.get("target", context.get("prey"))),
        }
        for field in (
            "use_safe_spots",
            "break_out_via_logoff",
            "max_carry",
            "flee_below",
            "hold_resume_above",
            "rest_below",
            "fight_above_vigor",
            "bank_above",
            "pull_within",
        ):
            intent[field] = context.get(field)

        if not isinstance(intent.get("use_safe_spots"), bool):
            strategy = " ".join(
                str(value or "")
                for value in (
                    phase.get("objective"),
                    phase.get("rationale"),
                    context.get("strategy"),
                    context.get("notes"),
                )
            ).casefold()
            if any(
                marker in strategy
                for marker in ("open-field", "open field", "without safe spot")
            ):
                intent["use_safe_spots"] = False
            elif "safe spot" in strategy:
                intent["use_safe_spots"] = True

        room = intent.get("assigned_room")
        try:
            intent["assigned_room"] = int(room) if room is not None else None
        except (TypeError, ValueError):
            intent["assigned_room"] = None
        hunt = " ".join(str(intent.get("hunt") or "").casefold().split())
        intent["hunt"] = hunt or None
        return intent

    def _effective_farm_intent(self, goal: dict[str, Any]) -> dict[str, Any]:
        """Merge operator-authored goal policy with the active campaign phase.

        Explicit public-goal fields remain authoritative.  The phase supplies
        the prey, room, and tactic for strategic goals such as "reach 45 HP"
        that intentionally leave those hour-to-hour choices to the configured planner.
        """
        goal_intent = self._goal_farm_intent(goal)
        run = self.storage.campaign_run(str(goal.get("id") or ""))
        phase = self.storage.active_campaign_phase(run["id"]) if run else None
        phase_intent = self._campaign_phase_farm_intent(phase)
        keys = set(goal_intent) | set(phase_intent)
        return {
            key: (
                goal_intent.get(key)
                if goal_intent.get(key) is not None
                else phase_intent.get(key)
            )
            for key in keys
        }

    def _structured_farm_launch_plan(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        completion: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Turn an executable, goal-owned farm recipe into a keeper launch.

        The supervisor has already chosen and grounded the bounded phase. Requiring the
        exact machine-readable recipe keeps tactical identity in durable goal
        state, while this fast path prevents the tactical model from spending
        turns rediscovering ``prey`` and ``hunting_grounds`` after banking and
        equipment preparation are complete. Any unmet deterministic preflight
        falls back to the planner so it can perform the missing preparation.
        """
        health_criteria = self._health_progress_criteria(goal, completion)
        if not any(item["result"].get("met") is not True for item in health_criteria):
            return None
        intent = self._effective_farm_intent(goal)
        if intent.get("assigned_room") is None or not intent.get("hunt"):
            return None
        current_room = deep_get(observation, "look.room.num")
        if str(current_room) != str(TOS_INN_ROOM_ID):
            return None

        readiness = self.learning.readiness_summary(observation)
        if (
            readiness.get("equipment_state") != "known"
            or not readiness.get("wielded_weapons")
        ):
            return None

        arguments: dict[str, Any] = {
            "action": "start",
            "mode": "farm",
            # Banking is a tactic selected in the durable recipe, not a
            # prerequisite for roaming or combat.  The keeper's special bank
            # trips stay disabled unless the planner/supervisor deliberately supplies a
            # positive bank_above value after assessing the live finances.
            "bank_above": 0,
        }
        arguments.update({key: value for key, value in intent.items() if value is not None})
        arguments, _ = self._normalize_combat_arguments(
            "autopilot",
            arguments,
            observation,
            allow_open_field=intent.get("use_safe_spots") is False,
        )
        if self._safety_preflight("autopilot", arguments, observation, goal):
            return None
        return {
            "decision": "act",
            "tool": "autopilot",
            "arguments": arguments,
            "rationale": (
                "Launch the goal-owned bounded keeper from Tos Inn using the grounded "
                "farm recipe; preparation and deterministic safety preflight are complete."
            ),
            "expected_observation": {
                "autopilot.running": True,
                "autopilot.mode": "farm",
                "autopilot.assigned_room": intent["assigned_room"],
            },
            "proposal": None,
        }

    def _recent_farm_food_quote(
        self, goal: dict[str, Any]
    ) -> dict[str, int] | None:
        """Return the newest live Paddock cheese quote for this goal.

        Merchant template ids are runtime evidence, not durable game facts.
        Reusing the most recent quote avoids another model turn while keeping
        deterministic provisioning bound to what the ordinary client saw.
        """

        for event in reversed(
            self.storage.goal_events(
                goal["id"], kinds=["action.succeeded"], limit=100
            )
        ):
            data = event.get("data") if isinstance(event, dict) else None
            if not isinstance(data, dict) or data.get("tool") != "shop":
                continue
            result = data.get("result")
            items = result.get("items") if isinstance(result, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if (
                    isinstance(item, dict)
                    and str(item.get("name") or "").strip().casefold()
                    == TOS_CHEESE_NAME
                    and isinstance(item.get("id"), int)
                    and isinstance(item.get("cost"), int)
                    and int(item["cost"]) > 0
                    and isinstance(result.get("seller"), int)
                ):
                    return {
                        "seller": int(result["seller"]),
                        "item_id": int(item["id"]),
                        "cost": int(item["cost"]),
                    }
        return None

    @staticmethod
    def _visible_tos_innkeeper(observation: dict[str, Any]) -> int | None:
        objects = deep_get(observation, "look.objects", [])
        for item in objects if isinstance(objects, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip().casefold()
            capabilities = item.get("can")
            if (
                name == TOS_INNKEEPER_NAME
                and isinstance(item.get("id"), int)
                and isinstance(capabilities, list)
                and "buy" in capabilities
            ):
                return int(item["id"])
        return None

    def _structured_farm_controller_plan(
        self, goal: dict[str, Any]
    ) -> dict[str, Any]:
        intent = self._effective_farm_intent(goal)
        return {
            "summary": (
                "Provision only the food needed for the numeric vigor gate, "
                "launch the goal-owned keeper from Tos Inn, then verify the "
                "bounded HP outcome and home finish."
            ),
            "steps": [
                {
                    "id": "farm-bank-transit",
                    "outcome": "Travel between Tos Inn and First Royal Bank of Tos (room 54) as farm funding and final banking require.",
                    "tool": "travel",
                    "verification": "Current room is the intended Tos Inn or bank endpoint for the next preparation action.",
                },
                {
                    "id": "withdraw-provision-funds",
                    "outcome": "Withdraw only the live quoted cost of the remaining farm food.",
                    "tool": "bank",
                    "verification": "Carried shillings increased by the requested amount.",
                },
                {
                    "id": "deposit-before-farm",
                    "outcome": "After provisioning is complete, deposit any carried shillings before the hazardous phase.",
                    "tool": "bank",
                    "verification": "A verified bank receipt permits the current carried amount for this phase.",
                },
                {
                    "id": "buy-farm-food",
                    "outcome": f"Quote or buy enough wheel(s) of cheese from Paddock to satisfy the {FARM_FIGHT_VIGOR}-vigor launch gate.",
                    "tool": "shop",
                    "verification": f"Verified carried food nutrition plus rested vigor reaches at least {FARM_FIGHT_VIGOR}.",
                },
                {
                    "id": "rest-for-farm",
                    "outcome": "Rest safely to the ordinary 80-vigor ceiling and stand again.",
                    "tool": "rest_up",
                    "verification": "Live vigor is at least 80 and the character is standing.",
                },
                {
                    "id": "equip-before-farm",
                    "outcome": "Ensure the best carried weapon is equipped before leaving sanctuary.",
                    "tool": "equip_best",
                    "verification": "The equipment list reports a wielded weapon.",
                },
                {
                    "id": "launch-goal-keeper",
                    "outcome": (
                        "Launch the grounded goal-owned keeper from Tos Inn for "
                        f"{intent.get('hunt')} in assigned room "
                        f"{intent.get('assigned_room')}."
                    ),
                    "tool": "autopilot",
                    "verification": (
                        "Keeper status reports running with this goal id, prey "
                        f"{intent.get('hunt')}, and assigned room "
                        f"{intent.get('assigned_room')}."
                    ),
                },
                {
                    "id": "finish-at-tos-bar",
                    "outcome": "After the HP criterion is met, finish inside Tos Inn by the bar.",
                    "tool": "walk_to",
                    "verification": "All deterministic HP and return-home criteria are met.",
                },
            ],
            "assumptions": [],
            "revision_reason": (
                "Replace model-selected preparation with a live-state-driven "
                "bounded provisioning sequence."
            ),
        }

    def _structured_farm_preparation_action(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        completion: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Advance a grounded farm through safe preparation without an LLM turn."""

        health_criteria = self._health_progress_criteria(goal, completion)
        if not any(item["result"].get("met") is not True for item in health_criteria):
            return None
        intent = self._effective_farm_intent(goal)
        if intent.get("assigned_room") is None or not intent.get("hunt"):
            return None

        current_room = deep_get(observation, "look.room.num")
        readiness = self.learning.readiness_summary(observation)
        supply = self._combat_vigor_supply(observation)
        vigor = deep_get(
            observation,
            "status.vitals.vigor.value",
            deep_get(observation, "look.vitals.vigor.value", 0),
        )
        try:
            live_vigor = int(vigor or 0)
        except (TypeError, ValueError):
            return None
        fight_vigor = FARM_FIGHT_VIGOR
        prepared_vigor = max(live_vigor, RESTED_VIGOR_FLOOR)
        nutrition_shortfall = max(
            0,
            fight_vigor
            - prepared_vigor
            - int(supply.get("vigor_points", 0) or 0),
        )
        can_make_food = int(supply.get("cookable_casts", 0) or 0) > 0

        def action(
            tool: str,
            arguments: dict[str, Any],
            step_id: str,
            rationale: str,
        ) -> dict[str, Any]:
            # A durable lesson is scoped to the exact tactic, not the desired
            # destination. The broker's route call supports an explicit hop
            # budget, which is a materially different retry after a stale
            # default-route lesson (and is especially useful after a broker
            # pathing update). Without this escalation, a controller-owned
            # preparation action would be re-suppressed every five seconds and
            # never give the repaired router a chance to move the character.
            if tool == "travel" and "max_hops" not in arguments:
                feedback = self._planner_feedback(goal)
                blocked_action = (
                    feedback.get("blocked_action")
                    if isinstance(feedback, dict)
                    else None
                )
                blocked_arguments = (
                    blocked_action.get("arguments")
                    if isinstance(blocked_action, dict)
                    else None
                )
                blocked_room = (
                    blocked_action.get("room")
                    if isinstance(blocked_action, dict)
                    else None
                )
                if (
                    isinstance(blocked_action, dict)
                    and blocked_action.get("tool") == "travel"
                    and isinstance(blocked_arguments, dict)
                    and str(blocked_arguments.get("to"))
                    == str(arguments.get("to"))
                    and str(blocked_room) == str(current_room)
                ):
                    arguments = {**arguments, "max_hops": 25}
                    rationale += (
                        " Retry with an explicit route budget because the "
                        "default-route tactic is durably deferred in this room."
                    )
            return {
                "decision": "act",
                "tool": tool,
                "arguments": arguments,
                "rationale": rationale,
                "expected_observation": {},
                "proposal": None,
                "plan_step_id": step_id,
            }

        if nutrition_shortfall > 0 and not can_make_food:
            quote = self._recent_farm_food_quote(goal)
            if quote is None:
                if str(current_room) != str(TOS_INN_ROOM_ID):
                    return action(
                        "travel",
                        {"to": TOS_INN_ROOM_ID},
                        "farm-bank-transit",
                        "Return to the safe inn to obtain a fresh food quote.",
                    )
                seller = self._visible_tos_innkeeper(observation)
                if seller is None:
                    return None
                return action(
                    "shop",
                    {"seller": seller},
                    "buy-farm-food",
                    "Read Paddock's live catalog before moving any money.",
                )

            cheese_needed = max(
                1,
                (nutrition_shortfall + TOS_CHEESE_VIGOR - 1)
                // TOS_CHEESE_VIGOR,
            )
            required_funds = cheese_needed * int(quote["cost"])
            carried = self._carried_currency(observation)
            if carried < required_funds:
                if str(current_room) != str(TOS_BANK_ROOM_ID):
                    return action(
                        "travel",
                        {"to": TOS_BANK_ROOM_ID},
                        "farm-bank-transit",
                        "Reach the verified Tos bank to withdraw only the food shortfall.",
                    )
                return action(
                    "bank",
                    {"action": "withdraw", "amount": required_funds - carried},
                    "withdraw-provision-funds",
                    "Withdraw exactly the remaining live quoted food cost.",
                )
            if str(current_room) != str(TOS_INN_ROOM_ID):
                return action(
                    "travel",
                    {"to": TOS_INN_ROOM_ID},
                    "farm-bank-transit",
                    "Return to Paddock with the bounded provision funds.",
                )
            return action(
                "shop",
                {"seller": quote["seller"], "buy_ids": [quote["item_id"]]},
                "buy-farm-food",
                "Buy one verified wheel at a time until carried nutrition reaches the gate.",
            )

        if str(current_room) != str(TOS_INN_ROOM_ID):
            return action(
                "travel",
                {"to": TOS_INN_ROOM_ID},
                "farm-bank-transit",
                "Return to Tos Inn before the keeper launch.",
            )
        rested = deep_get(
            observation,
            "status.vitals.vigor.rested",
            deep_get(observation, "look.vitals.vigor.rested"),
        )
        if rested is False:
            return action(
                "rest_up",
                {"to": 0.4, "max_seconds": 30},
                "rest-for-farm",
                "Recover to the ordinary rest ceiling and stand before launch.",
            )
        if (
            readiness.get("equipment_state") != "known"
            or not readiness.get("wielded_weapons")
        ):
            if not readiness.get("carried_weapons"):
                return None
            if self.learning.check_action("equip_best", {}, observation) is not None:
                # The tactical campaign owns recovery once the ordinary
                # no-argument equip attempt is durably disproved.  Returning
                # None lets the planner use explicit inventory/property actions rather
                # than tripping the same lesson and ending another phase.
                return None
            return action(
                "equip_best",
                {},
                "equip-before-farm",
                "Equip the best carried weapon while still in sanctuary.",
            )
        return None

    @staticmethod
    def _farm_quarantine_matches(
        quarantine: dict[str, Any], arguments: dict[str, Any]
    ) -> bool:
        """Whether a quarantine applies to this exact room/prey/strategy.

        Old serious incident records intentionally remain room-wide.  A legacy
        record whose sole evidence was a disproved wall can be safely inferred
        to describe the wall strategy, allowing an explicit open-field retry.
        """
        recorded_target = str(quarantine.get("target") or "").strip().casefold()
        requested_target = str(arguments.get("hunt") or "").strip().casefold()
        if recorded_target and requested_target and recorded_target != requested_target:
            return False

        recorded_safe_spots = quarantine.get("use_safe_spots")
        if not isinstance(recorded_safe_spots, bool):
            reasons = [
                str(item).casefold()
                for item in quarantine.get("reasons", [])
                if item is not None
            ]
            if reasons and all("safe spot" in reason for reason in reasons):
                recorded_safe_spots = True
        requested_safe_spots = arguments.get("use_safe_spots")
        if isinstance(recorded_safe_spots, bool) and isinstance(
            requested_safe_spots, bool
        ):
            return recorded_safe_spots == requested_safe_spots
        return True

    def _background_farm_mismatch(
        self, goal: dict[str, Any], status: dict[str, Any]
    ) -> dict[str, Any] | None:
        owner = self.storage.get_runtime("background_farm_owner_v1", {})
        owner = owner if isinstance(owner, dict) else {}
        intent = self._effective_farm_intent(goal)
        expected = {**intent, "fight_above_vigor": FARM_FIGHT_VIGOR}
        actual = {
            "assigned_room": self._farm_assigned_room(status),
            "hunt": self._farm_target(status).strip().casefold(),
            "use_safe_spots": deep_get(status, "policy.useSafeSpots", deep_get(status, "policy.use_safe_spots")),
            "fight_above_vigor": deep_get(
                status,
                "policy.fightAboveVigor",
                deep_get(status, "policy.fight_above_vigor"),
            ),
        }
        reasons: list[str] = []
        if owner.get("goal_id") and owner.get("goal_id") != goal.get("id"):
            reasons.append("running keeper belongs to a different durable goal")
        for field in (
            "assigned_room",
            "hunt",
            "use_safe_spots",
            "fight_above_vigor",
        ):
            wanted = expected.get(field)
            if wanted is not None and str(actual.get(field)) != str(wanted):
                reasons.append(f"{field} is {actual.get(field)!r}, expected {wanted!r}")
        if not reasons:
            return None
        return {
            "reasons": reasons,
            "owner": redact(owner),
            "expected": expected,
            "actual": actual,
        }

    def _stopped_farm_route_failure(
        self, goal: dict[str, Any], observation: dict[str, Any], status: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Turn a terminal keeper route-placement error into durable evidence.

        The keeper can stop after failing an exit before it ever reaches the
        assigned hunting room. Historically that looked like an ordinary idle
        keeper, so the foreground planner could start the same trip again. Only
        route-specific failures for this goal's exact assignment qualify here.
        """
        if status.get("running") is True and str(status.get("mode") or "") == "farm":
            return None
        handled_key = f"background_farm_route_failure_handled_v1:{goal['id']}"
        if self.storage.get_runtime(handled_key, False) is True:
            return None

        owner = self.storage.get_runtime("background_farm_owner_v1", {})
        owner = owner if isinstance(owner, dict) else {}
        intent = self._effective_farm_intent(goal)
        owner_matches = owner.get("goal_id") == goal.get("id")
        assigned_room = (
            owner.get("assigned_room") if owner_matches else intent.get("assigned_room")
        )
        target = str(
            (owner.get("hunt") if owner_matches else intent.get("hunt")) or ""
        ).strip().casefold()
        if assigned_room is None or not target:
            return None

        placement = status.get("placement")
        placement = placement if isinstance(placement, dict) else {}
        # The broker keeps cumulative placement failures in status. A failed
        # square/exit from an earlier pass is not terminal route evidence after
        # the character is visibly in the assigned room.
        if str(self._observation_room(observation)) == str(assigned_room) or (
            placement.get("standing_where_assigned") is True
            and str(placement.get("assigned_room")) == str(assigned_room)
        ):
            return None
        raw_failures = placement.get("why_not", [])
        if isinstance(raw_failures, dict):
            raw_failures = [
                {"room": room, **(value if isinstance(value, dict) else {"why": value})}
                for room, value in raw_failures.items()
            ]
        failures = [item for item in raw_failures if isinstance(item, dict)]
        matching_failures = [
            item
            for item in failures
            if item.get("room") is not None
            and str(item.get("room")) == str(assigned_room)
        ]
        reason_parts = [
            str(item.get("why") or item.get("reason") or item.get("detail") or "")
            for item in matching_failures
        ]
        journal_text = self._farm_journal_text(status)
        combined = " ".join([*reason_parts, journal_text]).casefold()
        route_markers = (
            "every square for that exit refused",
            "exit refused",
            "route failed",
            "failed to reach",
            "could not reach",
            "no route",
            "unreachable",
        )
        if not any(marker in combined for marker in route_markers):
            return None
        # Without a live ownership record, require the failed placement record
        # itself to name the room in the active goal. This prevents an old
        # journal entry from pausing an unrelated later goal.
        if not owner_matches and not matching_failures:
            return None

        return {
            "assigned_room": assigned_room,
            "target": target,
            "origin_room": owner.get("origin_room") if owner_matches else None,
            "current_room": self._observation_room(observation),
            "placement": redact(placement),
            "reason": next((part for part in reason_parts if part), "keeper route placement failed"),
        }

    def _handle_stopped_farm_route_failure(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        status: dict[str, Any],
        completion: dict[str, Any],
    ) -> dict[str, Any] | None:
        failure = self._stopped_farm_route_failure(goal, observation, status)
        if failure is None:
            return None

        assigned_room = failure["assigned_room"]
        target = str(failure["target"])
        stagnations = self.storage.get_runtime("farm_tactic_stagnation_v1", {})
        stagnations = dict(stagnations) if isinstance(stagnations, dict) else {}
        stagnation_key = f"{goal['id']}|{assigned_room}|{target}"
        prior = stagnations.get(stagnation_key)
        count = int(prior.get("count", 0) or 0) + 1 if isinstance(prior, dict) else 1
        stagnation = {
            "goal_id": goal["id"],
            "room": failure.get("current_room"),
            "assigned_room": assigned_room,
            "requested_assigned_room": assigned_room,
            "origin_room": failure.get("origin_room"),
            "stalled_in_transit": str(failure.get("current_room")) != str(assigned_room),
            "target": target,
            "count": count,
            "recorded_at": timestamp(),
            "placement": failure.get("placement"),
            "last_error": failure.get("reason"),
            "guidance": (
                "Do not restart this assigned-room/prey route unchanged. Choose a different "
                "grounded hunting room or wait for verified route/placement evidence to change."
            ),
        }
        stagnations[stagnation_key] = stagnation
        self.storage.set_runtime("farm_tactic_stagnation_v1", stagnations)

        farm_arguments = {
            "action": "start",
            "mode": "farm",
            "hunt": target,
            "assigned_room": assigned_room,
        }
        reason = (
            f"The keeper could not route from room {failure.get('origin_room') or failure.get('current_room')} "
            f"to assigned_room={assigned_room}: {failure.get('reason')}"
        )
        deferred = self.learning.defer_goal(
            goal,
            observation,
            tool="autopilot",
            arguments=farm_arguments,
            reason=reason,
            event_kind="background_farm.route_failed",
            classification="route_unavailable",
            scope="tactic",
            block=False,
        )
        current_goal = self.storage.goal(goal["id"])
        paused = None
        failed_phase = self._fail_active_campaign_phase(goal, reason)
        if (
            failed_phase is None
            and current_goal
            and current_goal.get("status") == "active"
        ):
            paused = self.storage.manage_goal(
                {
                    "request_id": f"controller-farm-route-failed-{uuid7()}",
                    "goal_id": goal["id"],
                    "expected_version": current_goal.get("version"),
                    "action": "pause",
                    "reason": reason,
                }
            ).get("goal")
        self.storage.set_runtime(
            f"background_farm_route_failure_handled_v1:{goal['id']}", True
        )
        self.storage.set_runtime("background_farm_owner_v1", {})
        recovery = None
        health_fraction = self._vital_fraction(observation, "health")
        if health_fraction is not None and health_fraction < 1.0:
            recovery = self._ensure_survival_keeper()
        self.storage.emit_event(
            "background_farm.route_failed",
            "Paused a farm phase after its exact keeper route failed",
            severity="warning",
            interesting=True,
            goal_id=goal["id"],
            data={
                **stagnation,
                "lesson_id": deep_get(deferred, "lesson.id"),
                "goal_paused": paused is not None,
                "campaign_phase_failed": failed_phase.get("id") if failed_phase else None,
                "strategic_goal_preserved": failed_phase is not None,
                "next_active_goal_id": deep_get(self.storage.active_goal() or {}, "id"),
                "recovery": redact(recovery),
            },
        )
        return {
            "background_farm_route_failed": True,
            "goal_paused": paused is not None,
            "campaign_phase_failed": failed_phase,
            "strategic_goal_preserved": failed_phase is not None,
            "failure": stagnation,
            "next_active_goal": self.storage.active_goal(),
            "recovery": recovery,
            "completion": completion,
            **deferred,
        }

    def _farm_flee_threshold(self, status: dict[str, Any]) -> float:
        raw = deep_get(status, "policy.fleeBelow", deep_get(status, "policy.flee_below"))
        try:
            requested = float(raw)
        except (TypeError, ValueError):
            requested = self.config.policy.rest_health_fraction
        # Report the keeper's actual boundary for evidence and quarantine
        # analysis. New launches are normalized to FARM_FLEE_THRESHOLD above,
        # but a keeper started by an older controller may still report a
        # historical value until it is stopped.
        return max(0.0, min(1.0, requested))

    @staticmethod
    def _farm_journal_text(
        status: dict[str, Any], *, minimum_pass: int | None = None
    ) -> str:
        records: list[Any] = []
        # Current harness status calls the compact tail `recent`; older builds
        # and full_journal responses use `journal`.  Trials also carry explicit
        # safe-spot verdicts.  Keep the operational journal available for
        # diagnosis, but do not equate an unavailable spot for one quarry with
        # a disproved spot. Only an observed idle hit disproves a wall.
        for key in ("journal", "recent", "trials"):
            values = status.get(key)
            if isinstance(values, list):
                for value in values[-20:]:
                    if minimum_pass is not None:
                        record_pass = value.get("pass") if isinstance(value, dict) else None
                        # A farm-session floor deliberately excludes unscoped
                        # historical prose. Current keeper safety verdicts carry
                        # a pass number; old no-pass records must not condemn a
                        # newly assigned room after a process restart.
                        if not isinstance(record_pass, (int, float)) or int(record_pass) < minimum_pass:
                            continue
                    records.append(value)
        return canonical_json(records).casefold() if records else ""

    @staticmethod
    def _farm_safe_spot_failure_ids(
        status: dict[str, Any], *, minimum_pass: int | None = None
    ) -> list[str]:
        """Return stable ids for distinct, observed held-wall failures.

        The keeper repeats the same journal entry in several status responses
        (and may mirror it between ``recent`` and ``trials``).  Treating the
        presence of that entry as a new incident on every controller heartbeat
        turns one harmless wall correction into an apparent degradation loop.
        """
        failures: set[str] = set()
        for key in ("journal", "recent", "trials"):
            values = status.get(key)
            if not isinstance(values, list):
                continue
            for value in values[-20:]:
                if not isinstance(value, dict):
                    continue
                record_pass = value.get("pass")
                if minimum_pass is not None and (
                    not isinstance(record_pass, (int, float))
                    or int(record_pass) < minimum_pass
                ):
                    continue
                what = str(value.get("what") or "").casefold()
                verdict = str(value.get("verdict") or "").casefold()
                try:
                    lost = float(value.get("lost") or value.get("lost_health") or 0)
                except (TypeError, ValueError):
                    lost = 0
                if not (
                    "this is not a safe spot" in what
                    or "safe spot failed" in what
                    or (lost > 0 and "does not work" in verdict)
                ):
                    continue

                where = value.get("where") if isinstance(value.get("where"), dict) else {}
                col = where.get("col", value.get("at_col"))
                row = where.get("row", value.get("at_row"))
                if isinstance(record_pass, (int, float)):
                    # Pass and square identify one keeper verdict even when its
                    # prose is mirrored into more than one status collection.
                    identity = f"pass:{int(record_pass)}|col:{col}|row:{row}"
                else:
                    identity = canonical_json(
                        {
                            "at": value.get("at"),
                            "col": col,
                            "row": row,
                            "lost": lost,
                            "what": what,
                            "verdict": verdict,
                        }
                    )
                failures.add(identity)
        return sorted(failures)

    @staticmethod
    def _farm_safe_spot_disproved(
        status: dict[str, Any], *, minimum_pass: int | None = None
    ) -> bool:
        """Return true only when the keeper observed a wall fail under attack."""
        return bool(
            BotController._farm_safe_spot_failure_ids(
                status, minimum_pass=minimum_pass
            )
        )

    @staticmethod
    def _farm_kill_records(
        status: dict[str, Any],
        *,
        minimum_pass: int | None = None,
        after_at: int | float | None = None,
    ) -> list[dict[str, Any]]:
        """Read deduplicated, explicitly named kills from the keeper journal."""
        found: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
        for key in ("journal", "recent"):
            values = status.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, dict) or str(value.get("what") or "").casefold() != "killed":
                    continue
                record_pass = value.get("pass")
                if minimum_pass is not None and (
                    not isinstance(record_pass, (int, float)) or int(record_pass) < minimum_pass
                ):
                    continue
                record_at = value.get("at")
                if not isinstance(record_at, (int, float)):
                    continue
                if after_at is not None and record_at <= after_at:
                    continue
                found[(record_at, record_pass, value.get("target"))] = value
        return sorted(found.values(), key=lambda item: float(item.get("at") or 0))

    def _farm_status_evidence(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        status: dict[str, Any],
    ) -> dict[str, Any]:
        """Turn cumulative keeper status into deltas and durable campaign evidence."""
        key = f"background_farm_snapshot_v2:{goal['id']}"
        previous = self.storage.get_runtime(key, {})
        previous = previous if isinstance(previous, dict) else {}
        did = status.get("did") if isinstance(status.get("did"), dict) else {}
        counters = {
            name: self._farm_counter(status, name)
            for name in (
                "kills",
                "deaths",
                "withdrawals",
                "mulligans",
                "logoffs",
                "deaths_in_safe_spot",
                "deaths_in_proven_safe_spot",
            )
        }
        prior_counters = previous.get("counters") if isinstance(previous.get("counters"), dict) else {}
        deltas = {
            name: max(0, value - int(prior_counters.get(name, 0) or 0))
            for name, value in counters.items()
        }
        profile = self.learning.profile(observation)
        current_supplies = int(profile.get("healing_supply_count", 0) or 0)
        prior_supplies = previous.get("healing_supply_count")
        supplies_used = (
            max(0, int(prior_supplies) - current_supplies)
            if isinstance(prior_supplies, (int, float))
            else 0
        )
        room = self._farm_room(status, observation)
        assigned_room = self._farm_assigned_room(status)
        at_assigned_room = (
            str(room) == str(assigned_room)
            if room is not None and assigned_room is not None
            else None
        )
        target = self._farm_target(status)
        use_safe_spots = deep_get(
            status,
            "policy.useSafeSpots",
            deep_get(status, "policy.use_safe_spots"),
        )
        use_safe_spots = use_safe_spots if isinstance(use_safe_spots, bool) else None

        pass_floor = previous.get("pass_floor")
        pass_floor = int(pass_floor) if isinstance(pass_floor, (int, float)) else None
        prior_kill_at = previous.get("last_kill_at")
        prior_kill_at = prior_kill_at if isinstance(prior_kill_at, (int, float)) else None
        kill_records = self._farm_kill_records(
            status, minimum_pass=pass_floor, after_at=prior_kill_at
        )
        if len(kill_records) > deltas["kills"]:
            kill_records = kill_records[-deltas["kills"] :] if deltas["kills"] else []
        kills_by_target: dict[str, int] = {}
        for record in kill_records[:20]:
            actual_target = str(record.get("target") or "unknown")
            kills_by_target[actual_target] = kills_by_target.get(actual_target, 0) + 1
            self.learning.record_combat_outcome(
                tool="autopilot",
                arguments={"target": actual_target, "hunt": target, "assigned_room": room},
                before=observation,
                result={
                    "killed": True,
                    "target": actual_target,
                    "source": "keeper_journal",
                    "pass": record.get("pass"),
                    "at": record.get("at"),
                },
                after=observation,
            )
        for _ in range(min(deltas["withdrawals"], 20)):
            self.learning.record_combat_outcome(
                tool="autopilot",
                arguments={"target": target, "hunt": target, "assigned_room": room},
                before=observation,
                result={"disengaged": True, "target": target, "source": "keeper_status_delta"},
                after=observation,
            )

        observed_safe_spot_failures = self._farm_safe_spot_failure_ids(
            status, minimum_pass=pass_floor
        )
        prior_safe_spot_failures = previous.get("safe_spot_failure_ids")
        prior_safe_spot_failures = (
            [str(item) for item in prior_safe_spot_failures]
            if isinstance(prior_safe_spot_failures, list)
            else []
        )
        all_safe_spot_failures = sorted(
            set(prior_safe_spot_failures) | set(observed_safe_spot_failures)
        )
        safe_spot_disproved = bool(
            set(observed_safe_spot_failures) - set(prior_safe_spot_failures)
        )
        safe_spot_failure_count = len(all_safe_spot_failures)
        health_fraction = self._vital_fraction(observation, "health")
        threshold = self._farm_flee_threshold(status)
        risk_reasons: list[str] = []
        tactic_warnings: list[str] = []
        if health_fraction is not None and health_fraction <= threshold:
            in_transit = (
                at_assigned_room is False
                and str(status.get("activity") or "").casefold() == "travelling"
            )
            if (
                in_transit
                and health_fraction > self.config.policy.critical_health_fraction
            ):
                # Travel can briefly cross a populated room. The keeper still
                # owns the route and may regenerate before the next observation;
                # a non-critical dip without a withdrawal is route evidence,
                # not proof that the destination farm failed.
                tactic_warnings.append(
                    "transient route damage reached the farm flee threshold while the keeper retained control"
                )
            else:
                risk_reasons.append("health reached the keeper flee threshold")
        if deltas["withdrawals"]:
            risk_reasons.append("the keeper had to withdraw")
        last_death = status.get("last_death") if isinstance(status.get("last_death"), dict) else None
        death_at = str(last_death.get("at")) if last_death and last_death.get("at") is not None else None
        death_is_new = bool(
            death_at and self.storage.get_runtime("background_farm_last_death_at") != death_at
        )
        if deltas["deaths"] or death_is_new:
            risk_reasons.append("the keeper observed a death")
        if deltas["deaths_in_safe_spot"] or deltas["deaths_in_proven_safe_spot"]:
            risk_reasons.append("a claimed safe spot failed lethally")
        if safe_spot_disproved:
            tactic_warnings.append("live journal evidence disproved a safe spot")
        # A wall being disproved is useful tactical learning, but the keeper is
        # explicitly built to abandon that square and try another one (or fight
        # in the open).  Quarantine only when repeated wall failures have also
        # consumed nearly all healing margin.  Health, withdrawal, and death
        # boundaries above remain immediate fail-closed signals.
        if safe_spot_failure_count >= 3 and current_supplies <= 1:
            risk_reasons.append(
                "repeated safe-spot failures left too little healing margin"
            )
        if supplies_used and current_supplies == 0:
            risk_reasons.append("healing supplies were depleted")

        snapshot = {
            "observed_at": timestamp(),
            "room": room,
            "assigned_room": assigned_room,
            "at_assigned_room": at_assigned_room,
            "target": target,
            "use_safe_spots": use_safe_spots,
            "counters": counters,
            "healing_supply_count": current_supplies,
            "safe_spot": redact(status.get("safe_spot")),
            "activity": status.get("activity"),
            "health_fraction": health_fraction,
            "flee_threshold": threshold,
            "pass_floor": pass_floor,
            "last_kill_at": max(
                [float(record.get("at") or 0) for record in kill_records]
                + ([float(prior_kill_at)] if prior_kill_at is not None else [0])
            ),
            "safe_spot_failure_ids": all_safe_spot_failures[-50:],
            "safe_spot_failure_count": safe_spot_failure_count,
        }
        self.storage.set_runtime(key, snapshot)
        if any(deltas.values()) or supplies_used or safe_spot_disproved or tactic_warnings:
            history = self.storage.get_runtime("background_farm_history_v1", [])
            history = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
            sample = {
                **snapshot,
                "goal_id": goal["id"],
                "deltas": deltas,
                "kills_by_target": kills_by_target,
                "unattributed_kills": max(0, deltas["kills"] - len(kill_records)),
                "healing_supplies_used": supplies_used,
                "safe_spot_disproved": safe_spot_disproved,
                "safe_spot_failure_count": safe_spot_failure_count,
                "tactic_warnings": tactic_warnings,
                "risk_reasons": risk_reasons,
            }
            history.append(sample)
            self.storage.set_runtime("background_farm_history_v1", history[-100:])
            self.storage.emit_event(
                "background_farm.evidence",
                "Recorded background-farm progress and safety evidence",
                severity="warning" if risk_reasons else "info",
                interesting=bool(risk_reasons),
                goal_id=goal["id"],
                data=sample,
            )
        return {
            "room": room,
            "assigned_room": assigned_room,
            "at_assigned_room": at_assigned_room,
            "target": target,
            "use_safe_spots": use_safe_spots,
            "counters": counters,
            "deltas": deltas,
            "kills_by_target": kills_by_target,
            "unattributed_kills": max(0, deltas["kills"] - len(kill_records)),
            "healing_supply_count": current_supplies,
            "healing_supplies_used": supplies_used,
            "health_fraction": health_fraction,
            "flee_threshold": threshold,
            "safe_spot_disproved": safe_spot_disproved,
            "safe_spot_failure_count": safe_spot_failure_count,
            "tactic_warnings": tactic_warnings,
            "death_is_new": death_is_new,
            "risk_reasons": list(dict.fromkeys(risk_reasons)),
        }

    def _quarantine_farm_tactic(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        room = evidence.get("room")
        quarantines = self.storage.get_runtime("farm_tactic_quarantine_v1", {})
        quarantines = dict(quarantines) if isinstance(quarantines, dict) else {}
        record = {
            "room": room,
            "assigned_room": evidence.get("assigned_room"),
            "at_assigned_room": evidence.get("at_assigned_room"),
            "target": evidence.get("target"),
            "use_safe_spots": evidence.get("use_safe_spots"),
            "quarantined_at": timestamp(),
            "goal_id": goal["id"],
            "reasons": evidence.get("risk_reasons", []),
            "deltas": evidence.get("deltas", {}),
            "health_fraction": evidence.get("health_fraction"),
            "flee_threshold": evidence.get("flee_threshold"),
            "live_overlevel_hostiles": evidence.get("live_overlevel_hostiles", []),
            "quarantined": evidence.get("at_assigned_room") is True,
            "guidance": (
                "Do not reuse this room/prey farm unchanged; choose a different grounded room after a verified capability improvement."
                if evidence.get("at_assigned_room") is True
                else "The retreat occurred during transit before the assigned room; do not label the target farm room unsafe. Recover and meet the keeper's vigor gate before another launch."
            ),
        }
        if evidence.get("at_assigned_room") is True and evidence.get("assigned_room") is not None:
            quarantines[str(evidence.get("assigned_room"))] = record
            self.storage.set_runtime("farm_tactic_quarantine_v1", quarantines)
        return record

    def _manage_background_farm(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        completion: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Give a running farm exclusive control until its bounded HP phase ends."""
        full_scan = goal["id"] not in self._farm_full_scan_goals
        status = self.broker.call_tool(
            "autopilot",
            {
                "agent": self.config.game.agent,
                "action": "status",
                "full_journal": full_scan,
            },
            timeout=20,
        )
        if full_scan:
            self._farm_full_scan_goals.add(goal["id"])
        if not isinstance(status, dict):
            return None
        # Upstream now implements ordinary stop as an inert telemetry loop.
        # It is safe for foreground/campaign work to proceed and must not be
        # fed back through stopped-route diagnosis or another stop forever.
        if self._keeper_is_inert(status):
            return None
        route_failure = self._handle_stopped_farm_route_failure(
            goal, observation, status, completion
        )
        if route_failure is not None:
            return route_failure
        if status.get("running") is not True:
            return None

        keeper_mode = str(status.get("mode") or "unknown")
        if keeper_mode != "farm":
            # Every keeper mode can move the character.  A startup/survival
            # keeper must therefore own the turn just like a farm keeper;
            # otherwise the planner can issue foreground travel while the
            # keeper is still completing its own route.  Let survival finish
            # healing first, then request a stop and withhold foreground work
            # until a later status call proves the loop has exited.
            health_fraction = self._vital_fraction(observation, "health")
            if keeper_mode == "survive" and health_fraction is not None and health_fraction < 1.0:
                return {
                    "background_survival_monitoring": True,
                    "activity": status.get("activity"),
                    "health_fraction": health_fraction,
                    "completion": completion,
                }
            stopped = self.broker.call_tool(
                "autopilot",
                {"agent": self.config.game.agent, "action": "stop"},
                timeout=20,
                mutation=True,
            )
            self.storage.set_runtime("background_farm_owner_v1", {})
            return {
                "background_keeper_stopping": True,
                "mode": keeper_mode,
                "activity": status.get("activity"),
                "result": redact(stopped),
                "completion": completion,
            }

        mismatch = self._background_farm_mismatch(goal, status)
        if mismatch:
            stopped = self.broker.call_tool(
                "autopilot",
                {"agent": self.config.game.agent, "action": "stop"},
                timeout=20,
                mutation=True,
            )
            self.storage.set_runtime("background_farm_owner_v1", {})
            self._set_planner_feedback(
                goal,
                "A stale background farm from another goal was stopped. Wait for the keeper to finish its current pass, then launch only the hunt and assigned_room named by this goal.",
            )
            self.storage.emit_event(
                "background_farm.owner_mismatch",
                "Stopped a background farm whose goal or tactic did not match the active goal",
                severity="warning",
                interesting=True,
                goal_id=goal["id"],
                data={**mismatch, "result": redact(stopped)},
            )
            return {
                "background_farm_stale_stopped": True,
                "mismatch": mismatch,
                "completion": completion,
            }

        evidence = self._farm_status_evidence(goal, observation, status)
        if evidence.get("at_assigned_room") is True:
            live_threats = self._live_overlevel_hostiles(observation)
            if live_threats:
                evidence["live_overlevel_hostiles"] = live_threats
                evidence["risk_reasons"] = list(
                    dict.fromkeys(
                        [
                            *evidence.get("risk_reasons", []),
                            "live room contains a source-resolved hostile above the verified danger band",
                        ]
                    )
                )
                self.storage.emit_event(
                    "background_farm.live_threat_detected",
                    "Live room state contained an over-level hostile omitted from the generator table",
                    severity="warning",
                    interesting=True,
                    goal_id=goal["id"],
                    data={
                        "room": evidence.get("room"),
                        "assigned_room": evidence.get("assigned_room"),
                        "target": evidence.get("target"),
                        "hostiles": live_threats,
                    },
                )
        if evidence["risk_reasons"]:
            return self._handoff_background_farm_to_survival(
                goal,
                observation,
                status=status,
                evidence=evidence,
            )

        health_criteria = self._health_progress_criteria(goal, completion)
        unmet_health = [item for item in health_criteria if item["result"].get("met") is not True]
        unhealthy = bool(status.get("stalled") or status.get("last_error"))
        if unmet_health and not unhealthy:
            runtime_key = f"background_farm_notice_at:{goal['id']}"
            last_notice = float(self.storage.get_runtime(runtime_key, 0) or 0)
            if time.time() - last_notice >= 300:
                self.storage.emit_event(
                    "background_farm.monitored",
                    "Background farming keeper owns movement and combat for this HP phase",
                    severity="info",
                    interesting=False,
                    goal_id=goal["id"],
                    data={
                        "activity": status.get("activity"),
                        "did": redact(status.get("did")),
                        "placement": redact(status.get("placement")),
                        "safe_spot": redact(status.get("safe_spot")),
                        "health": deep_get(observation, "status.vitals.health"),
                        "targets": [item["criterion"].get("value") for item in unmet_health],
                    },
                )
                self.storage.set_runtime(runtime_key, time.time())
            return {
                "background_farm_monitoring": True,
                "activity": status.get("activity"),
                "completion": completion,
            }

        # A healthy keeper is stopped only when its bounded HP criterion is met.
        # A stalled or errored keeper is also released so the planner can repair
        # the tactic on the following turn. Returning here prevents a foreground
        # mutation from racing the keeper during the same controller turn.
        stopped = self.broker.call_tool(
            "autopilot",
            {"agent": self.config.game.agent, "action": "stop"},
            timeout=20,
            mutation=True,
        )
        self.storage.set_runtime("background_farm_owner_v1", {})
        reason = (
            "background keeper stalled or errored"
            if unhealthy
            else "bounded max-HP target reached"
        )
        if unhealthy:
            room = self._farm_room(status, observation)
            assigned_room = self._farm_assigned_room(status)
            target = self._farm_target(status)
            # A keeper can stall before it ever reaches the requested hunting
            # ground (for example, on unreachable geometry in its departure
            # room).  Keying that evidence to the destination would wrongly
            # suppress a later launch after the character is moved there.  A
            # transit stall describes the room where it actually happened;
            # only a stall at the assignment describes that assignment.
            tactic_room = (
                room
                if room is not None
                and assigned_room is not None
                and str(room) != str(assigned_room)
                else assigned_room or room
            )
            stagnations = self.storage.get_runtime("farm_tactic_stagnation_v1", {})
            stagnations = dict(stagnations) if isinstance(stagnations, dict) else {}
            stagnation_key = f"{goal['id']}|{tactic_room}|{target.strip().casefold()}"
            prior = stagnations.get(stagnation_key)
            count = int(prior.get("count", 0) or 0) + 1 if isinstance(prior, dict) else 1
            stagnation = {
                "goal_id": goal["id"],
                "room": room,
                "assigned_room": tactic_room,
                "requested_assigned_room": assigned_room,
                "stalled_in_transit": bool(
                    room is not None
                    and assigned_room is not None
                    and str(room) != str(assigned_room)
                ),
                "target": target,
                "count": count,
                "recorded_at": timestamp(),
                "stalled": redact(status.get("stalled")),
                "last_error": status.get("last_error"),
                "did": redact(status.get("did")),
                "guidance": (
                    "Do not restart this room/prey farm unchanged. Query hunting_grounds once, "
                    "choose a different non-quarantined room whose full spawn table is within the "
                    "verified danger band, and launch the same bounded HP phase there."
                ),
            }
            stagnations[stagnation_key] = stagnation
            self.storage.set_runtime("farm_tactic_stagnation_v1", stagnations)
            self.storage.emit_event(
                "background_farm.tactic_deferred",
                "Deferred an unchanged farm tactic after a keeper stall",
                severity="notice",
                interesting=True,
                goal_id=goal["id"],
                data=stagnation,
            )
        self._set_planner_feedback(
            goal,
            (
                f"The farming keeper was stopped because {reason}. "
                + (
                    "On the next turn query hunting_grounds once and launch the same bounded phase "
                    "in a different grounded, non-quarantined room; do not restart the stalled "
                    "room/prey tactic unchanged."
                    if unhealthy
                    else "On the next turn, recover or travel home for the remaining deterministic criteria; "
                    "do not restart the completed farm."
                )
            ),
        )
        self.storage.emit_event(
            "background_farm.stopped",
            f"Stopped background farming keeper: {reason}",
            severity="warning" if unhealthy else "info",
            interesting=unhealthy,
            goal_id=goal["id"],
            data={
                "reason": reason,
                "activity": status.get("activity"),
                "last_error": status.get("last_error"),
                "stalled": status.get("stalled"),
                "result": redact(stopped),
            },
        )
        return {
            "background_farm_stopped": True,
            "reason": reason,
            "completion": completion,
        }

    def _handoff_background_farm_to_survival(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        *,
        status: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Stop automatic retry at the keeper's own retreat boundary."""
        if status is None:
            status = self.broker.call_tool(
                "autopilot",
                {"agent": self.config.game.agent, "action": "status", "full_journal": True},
                timeout=20,
            )
        if not self._keeper_is_driving(status) or status.get("mode") != "farm":
            return None

        evidence = evidence or self._farm_status_evidence(goal, observation, status)
        if not evidence.get("risk_reasons"):
            evidence["risk_reasons"] = ["controller critical-health interrupt"]

        death_event = None
        last_death = status.get("last_death") if isinstance(status.get("last_death"), dict) else None
        if last_death and last_death.get("at") is not None:
            death_at = str(last_death.get("at"))
            if self.storage.get_runtime("background_farm_last_death_at") != death_at:
                target = last_death.get("hunting") or deep_get(status, "policy.hunt") or "unknown"
                before = {
                    "look": {
                        "room": {
                            "num": last_death.get("room_num"),
                            "name": last_death.get("died_in"),
                        }
                    },
                    "status": {
                        "vitals": {
                            "health": {
                                "current": last_death.get("last_health"),
                                "max": last_death.get("level"),
                            }
                        }
                    },
                    "inventory": observation.get("inventory", {}),
                }
                combat = self.learning.record_combat_outcome(
                    tool="autopilot",
                    arguments={"target": target, "hunt": target},
                    before=before,
                    result={"died": True, "target": target, "post_mortem": last_death.get("post_mortem")},
                    after=observation,
                    died=True,
                )
                death_event = self.storage.emit_event(
                    "character.died",
                    "The character died during background farming",
                    severity="critical",
                    interesting=True,
                    goal_id=goal["id"],
                    data={"tool": "autopilot", "broker_death": redact(last_death), "combat_memory": combat},
                )
                self.storage.set_runtime("background_farm_last_death_at", death_at)

        quarantine = self._quarantine_farm_tactic(goal, observation, evidence)
        handled_key = f"background_farm_failure_handled_v1:{goal['id']}"
        already_handled = bool(self.storage.get_runtime(handled_key, False))
        lesson = None
        paused = None
        if not already_handled:
            live_room_hazard = bool(evidence.get("live_overlevel_hostiles"))
            assigned_tactic_failure = evidence.get("at_assigned_room") is True
            lesson_result = self.learning.defer_goal(
                goal,
                observation,
                tool="autopilot",
                arguments={
                    "action": "start",
                    "mode": "farm",
                    "hunt": evidence.get("target"),
                    "assigned_room": evidence.get("assigned_room"),
                },
                reason=(
                    (
                        "Hazardous transit to the assigned farm room exceeded verified survivability before arrival: "
                        if evidence.get("at_assigned_room") is False
                        else "Background farming exceeded verified survivability in the assigned room: "
                    )
                    + "; ".join(str(item) for item in evidence.get("risk_reasons", []))
                ),
                event_kind="background_farm.survival_handoff",
                # A failure after arrival invalidates that room/target/safe-spot
                # tactic. A pre-arrival retreat invalidates the exact launch
                # route/origin/assignment tactic. Neither observation proves the
                # max-HP outcome impossible or justifies a capability-only gate.
                classification=(
                    "ineffective_tactic"
                    if assigned_tactic_failure
                    else "route_unavailable"
                ),
                scope="tactic",
                block=False,
                # Let the classifier choose a room/corpus change for transit or
                # a capability/cooldown change for an in-room tactic. Do not
                # store a vigor predicate already proven true at launch time.
                retry_when=None,
            )
            lesson = lesson_result.get("lesson")

        switched = self.broker.call_tool(
            "autopilot",
            {
                "agent": self.config.game.agent,
                "action": "start",
                "mode": "survive",
                "hunt": "",
                "assigned_room": None,
                "bank_above": 0,
                "rest_below": self.config.policy.rest_health_fraction,
                "flee_below": self.config.policy.rest_health_fraction,
                # Reconnecting while a just-entered sanctuary has not yet been
                # durably saved can restore the prior dangerous room.
                "break_out_via_logoff": False,
            },
            timeout=20,
            mutation=True,
        )
        self.storage.set_runtime("background_farm_owner_v1", {})
        current_goal = self.storage.goal(goal["id"])
        phase_failure_reason = (
            "Farm survivability failed in the current internal phase: "
            + "; ".join(str(item) for item in evidence.get("risk_reasons", []))
        )
        failed_phase = (
            self._fail_active_campaign_phase(goal, phase_failure_reason)
            if not already_handled
            else None
        )
        if (
            not already_handled
            and failed_phase is None
            and current_goal
            and current_goal.get("status") == "active"
        ):
            paused = self.storage.manage_goal(
                {
                    "request_id": uuid7(),
                    "goal_id": goal["id"],
                    "expected_version": current_goal.get("version"),
                    "action": "pause",
                    "reason": (
                        "Controller paused progression after farm survivability failed: "
                        + "; ".join(str(item) for item in evidence.get("risk_reasons", []))
                    ),
                }
            ).get("goal")
        self.storage.set_runtime(handled_key, True)
        self.storage.emit_event(
            "background_farm.survival_handoff",
            "Farm retreat boundary handed the keeper back to survival-only recovery",
            severity="warning",
            interesting=True,
            goal_id=goal["id"],
            data={
                "activity": status.get("activity"),
                "last_death": redact(last_death),
                "death_event_id": death_event.get("id") if death_event else None,
                "evidence": redact(evidence),
                "quarantine": quarantine,
                "lesson_id": lesson.get("id") if isinstance(lesson, dict) else None,
                "goal_paused": paused is not None,
                "campaign_phase_failed": failed_phase.get("id") if failed_phase else None,
                "strategic_goal_preserved": failed_phase is not None,
                "result": redact(switched),
            },
        )
        return {
            "switched_to_survival": True,
            "death_observed": death_event is not None,
            "goal_paused": paused is not None,
            "campaign_phase_failed": failed_phase,
            "strategic_goal_preserved": failed_phase is not None,
            "quarantine": quarantine,
        }

    def _ensure_survival_keeper(self) -> dict[str, Any]:
        """Start foreground emergency recovery unless it already owns the turn."""
        status = self.broker.call_tool(
            "autopilot",
            {"agent": self.config.game.agent, "action": "status"},
            timeout=20,
        )
        if (
            self._keeper_is_driving(status)
            and status.get("mode") == "survive"
        ):
            return {
                "survival_keeper_running": True,
                "already_running": True,
                "activity": status.get("activity"),
            }

        result = self.broker.call_tool(
            "autopilot",
            {
                "agent": self.config.game.agent,
                "action": "start",
                "mode": "survive",
                # The upstream keeper retains farm policy fields across mode
                # changes. Clear them so emergency recovery cannot route back
                # toward a hunting room or make a special banking trip.
                "hunt": "",
                "assigned_room": None,
                "bank_above": 0,
                "rest_below": self.config.policy.rest_health_fraction,
                "flee_below": max(
                    0.75, self.config.policy.rest_health_fraction
                ),
                "break_out_via_logoff": False,
            },
            timeout=20,
            mutation=True,
        )
        return {
            "survival_keeper_started": True,
            "already_running": False,
            "result": redact(result),
        }

    @staticmethod
    def _observed_ability_values(
        observation: dict[str, Any],
    ) -> dict[str, dict[str, Any]] | None:
        abilities = observation.get("abilities")
        if not isinstance(abilities, dict):
            return None
        freshness = abilities.get("freshness")
        if isinstance(freshness, dict) and freshness.get("known") is False:
            return None
        values: dict[str, dict[str, Any]] = {}
        for plural, singular in (("skills", "skill"), ("spells", "spell")):
            rows = abilities.get(plural)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or not row.get("name"):
                    continue
                try:
                    ability = int(row.get("ability"))
                except (TypeError, ValueError):
                    continue
                if ability <= 0:
                    continue
                name = " ".join(str(row["name"]).split())
                key = f"{singular}:{name.casefold()}"
                values[key] = {
                    "kind": singular,
                    "name": name,
                    "ability": ability,
                }
        return values

    def _record_character_progress(self, observation: dict[str, Any]) -> None:
        """Emit only durable character milestones suitable for an executive journal."""
        runtime_key = "character_progress_milestones_v1"
        previous = self.storage.get_runtime(runtime_key)
        health_max = deep_get(
            observation,
            "status.vitals.health.max",
            deep_get(observation, "look.vitals.health.max"),
        )
        try:
            max_health = int(health_max) if health_max is not None else None
        except (TypeError, ValueError):
            max_health = None
        ability_values = self._observed_ability_values(observation)

        if not isinstance(previous, dict):
            baseline = ability_values or {}
            self.storage.set_runtime(
                runtime_key,
                {
                    "max_health": max_health,
                    "abilities": baseline,
                    "reported_abilities": {
                        key: int(value["ability"]) for key, value in baseline.items()
                    },
                    "observed_at": timestamp(),
                },
            )
            return

        active = self.storage.active_goal()
        goal_id = active.get("id") if isinstance(active, dict) else None
        old_max = previous.get("max_health")
        if (
            isinstance(max_health, int)
            and isinstance(old_max, (int, float))
            and max_health > int(old_max)
        ):
            self.storage.emit_event(
                "progress.hp_gained",
                f"Maximum HP increased from {int(old_max)} to {max_health}",
                severity="notice",
                interesting=True,
                goal_id=goal_id,
                data={
                    "before": int(old_max),
                    "after": max_health,
                    "gained": max_health - int(old_max),
                    "room": redact(deep_get(observation, "look.room")),
                },
            )

        prior_abilities = previous.get("abilities")
        prior_abilities = prior_abilities if isinstance(prior_abilities, dict) else {}
        reported = previous.get("reported_abilities")
        reported = dict(reported) if isinstance(reported, dict) else {}
        if ability_values is not None:
            for key, current in ability_values.items():
                before = prior_abilities.get(key)
                if not isinstance(before, dict):
                    self.storage.emit_event(
                        f"progress.{current['kind']}_learned",
                        f"The character learned {current['kind']} {current['name']}",
                        severity="notice",
                        interesting=True,
                        goal_id=goal_id,
                        data={
                            "kind": current["kind"],
                            "name": current["name"],
                            "ability": current["ability"],
                        },
                    )
                    reported[key] = int(current["ability"])
                    continue
                old_value = before.get("ability")
                try:
                    old_value = int(old_value)
                except (TypeError, ValueError):
                    old_value = int(current["ability"])
                new_value = int(current["ability"])
                try:
                    last_reported = int(reported.get(key, old_value))
                except (TypeError, ValueError):
                    last_reported = old_value
                milestone = (new_value // 5) * 5
                if new_value > old_value and milestone >= 5 and milestone > last_reported:
                    self.storage.emit_event(
                        f"progress.{current['kind']}_milestone",
                        f"{current['name']} reached {milestone}",
                        severity="notice",
                        interesting=True,
                        goal_id=goal_id,
                        data={
                            "kind": current["kind"],
                            "name": current["name"],
                            "before": old_value,
                            "after": new_value,
                            "milestone": milestone,
                        },
                    )
                    reported[key] = milestone

        self.storage.set_runtime(
            runtime_key,
            {
                "max_health": max_health if max_health is not None else old_max,
                "abilities": ability_values if ability_values is not None else prior_abilities,
                "reported_abilities": reported,
                "observed_at": timestamp(),
            },
        )

    def _campaign_turn_state(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        grounding: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        """Reconcile the durable internal phase and select one when needed."""
        run, phase = self.campaign.ensure(goal)
        outcome = self.campaign.evaluate_phase(goal, run, phase, observation)
        if outcome.completed or outcome.failed:
            self._invalidate_execution_plan(
                goal,
                (
                    "the internal campaign phase reached its deterministic criteria"
                    if outcome.completed
                    else str(outcome.detail.get("reason") or "a phase abandonment predicate was verified")
                ),
            )
            run = self.storage.campaign_run(goal["id"]) or run
            phase = self.storage.active_campaign_phase(run["id"])
        phase_blocker = self._campaign_phase_grounding_blocker(
            phase,
            observation,
        )
        if phase is not None and phase_blocker is not None:
            reason = (
                "farm phase conflicts with retained controller evidence: "
                + str(phase_blocker.get("guidance") or phase_blocker.get("kind"))
            )
            self.storage.transition_campaign_phase(
                phase["id"],
                "failed",
                reason=reason,
            )
            self.storage.emit_event(
                "campaign.phase.grounding_rejected",
                "Retired an internal farm phase that conflicts with retained evidence",
                severity="warning",
                interesting=False,
                goal_id=goal["id"],
                data={
                    "phase_id": phase["id"],
                    "blocker": phase_blocker,
                    "strategic_goal_preserved": True,
                },
            )
            self._invalidate_execution_plan(goal, reason)
            run = self.storage.campaign_run(goal["id"]) or run
            phase = None
        if phase is not None:
            return run, phase, None

        grounded_context = (
            self.knowledge.context_for(goal, redact(observation))
            if hasattr(self.knowledge, "context_for")
            else {}
        )
        learned = self.learning.context_for(goal, redact(observation))
        financial = self._financial_context(observation)
        progression: dict[str, Any] | None = None
        if any(
            str(criterion.get("metric") or "").casefold()
            in {
                "max_health",
                "status.vitals.health.max",
                "look.vitals.health.max",
                "status.vitals.health.maximum",
                "look.vitals.health.maximum",
            }
            for criterion in goal.get("success_criteria", [])
            if isinstance(criterion, dict)
        ):
            try:
                progression = self.progression_context(
                    {"character_state": observation, "detail": "compact", "limit": 8}
                )
            except (BrokerError, ValueError) as exc:
                progression = {"warning": str(exc)[:500]}

        if hasattr(self.model, "manage_campaign"):
            decision = self.model.manage_campaign(
                goal=goal,
                observation=redact(observation),
                campaign_context=self._campaign_context(run, None),
                grounded_knowledge=grounded_context,
                learned_failures=learned,
                financial_context=financial,
                progression_context=progression,
            )
            self.dependencies["model"] = "healthy"
        else:
            decision = {
                "decision": "start_phase",
                "phase": self.campaign.fallback_phase(goal, observation),
                "rationale": "Compatibility mode for a planner without a campaign-manager method.",
                "evidence": [],
            }

        external_blocker_candidate: dict[str, Any] | None = None
        manager_decision = str(decision.get("decision") or "")
        if manager_decision == "complete_campaign_candidate":
            decision = {
                "decision": "start_phase",
                "phase": self.campaign.fallback_phase(goal, observation),
                "rationale": "Deterministic strategic criteria rejected premature completion.",
                "evidence": decision.get("evidence", []),
            }
        elif manager_decision == "report_external_blocker_candidate":
            external_blocker_candidate = {
                "status": "candidate",
                "rationale": str(decision.get("rationale") or "")[:1000],
                "evidence": redact(decision.get("evidence", [])),
                "recorded_at": timestamp(),
            }
            self.storage.update_campaign_memory(
                run["id"], external_blocker=external_blocker_candidate
            )
            self.storage.emit_event(
                "campaign.blocker.candidate",
                "Campaign manager reported a possible external blocker; strategic goal remains active",
                severity="warning",
                interesting=False,
                goal_id=goal["id"],
                data={
                    "run_id": run["id"],
                    "rationale": str(decision.get("rationale") or "")[:1000],
                    "strategic_goal_preserved": True,
                },
            )
            decision = {
                "decision": "start_phase",
                "phase": self.campaign.fallback_phase(goal, observation),
                "rationale": "Continue ordinary-game alternatives until an external blocker is verified.",
                "evidence": decision.get("evidence", []),
            }

        try:
            proposed_phase = decision.get("phase")
            proposed_blocker = self._campaign_phase_grounding_blocker(
                proposed_phase if isinstance(proposed_phase, dict) else None,
                observation,
            )
            if proposed_blocker is not None:
                raise ValueError(
                    "proposed farm phase conflicts with retained controller evidence: "
                    + str(
                        proposed_blocker.get("guidance")
                        or proposed_blocker.get("kind")
                    )
                )
            phase = self.campaign.apply_manager_decision(
                run, goal, decision, observation=observation
            )
        except (TypeError, ValueError) as exc:
            self.storage.emit_event(
                "campaign.manager.rejected",
                f"Rejected invalid internal campaign phase: {str(exc)[:240]}",
                severity="warning",
                interesting=False,
                goal_id=goal["id"],
                data={"decision": redact(decision), "strategic_goal_preserved": True},
            )
            fallback = {
                "decision": "start_phase",
                "phase": self.campaign.fallback_phase(goal, observation),
                "rationale": f"Compatibility fallback after invalid phase: {str(exc)[:300]}",
            }
            phase = self.campaign.apply_manager_decision(
                run, goal, fallback, observation=observation
            )
        if phase is None:
            raise ModelError("campaign manager did not leave an executable internal phase")
        if external_blocker_candidate is not None:
            # Creating a new phase normally clears stale blockers. This one is fresh and
            # must stay visible while the fallback phase tests ordinary-game alternatives.
            self.storage.update_campaign_memory(
                run["id"], external_blocker=external_blocker_candidate
            )
        return run, phase, {"decision": decision, "grounding": grounding}

    def _campaign_phase_grounding_blocker(
        self,
        phase: dict[str, Any] | None,
        observation: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Reject a farm phase that contradicts retained tactic evidence.

        Tactical planning cannot replace a durable campaign phase.  Accepting a
        phase for a quarantined room/prey/strategy therefore traps the planner
        in endless plan revisions even when it correctly recognizes the bad
        destination.  Apply the same quarantine matching used by autopilot
        before the phase is allowed to own execution.
        """

        if not isinstance(phase, dict) or phase.get("kind") != "farm":
            return None
        intent = self._campaign_phase_farm_intent(phase)
        assigned_room = intent.get("assigned_room")
        target = intent.get("hunt")
        use_safe_spots = intent.get("use_safe_spots")
        missing = []
        if assigned_room is None:
            missing.append("context.room")
        if not target:
            missing.append("context.target")
        if not isinstance(use_safe_spots, bool):
            missing.append("context.use_safe_spots")
        if missing:
            return {
                "kind": "invalid_farm_phase_context",
                "assigned_room": assigned_room,
                "hunt": target,
                "use_safe_spots": use_safe_spots,
                "guidance": (
                    "farm phase is not executable; provide structured "
                    + ", ".join(missing)
                    + " instead of relying on rationale prose"
                ),
            }
        arguments = {
            "assigned_room": assigned_room,
            "hunt": target,
            "use_safe_spots": use_safe_spots,
        }
        quarantines = self.storage.get_runtime("farm_tactic_quarantine_v1", {})
        quarantines = quarantines if isinstance(quarantines, dict) else {}
        quarantine = quarantines.get(str(assigned_room))
        if isinstance(quarantine, dict) and self._farm_quarantine_matches(
            quarantine, arguments
        ):
            return {
                "kind": "quarantined_farm_phase",
                "assigned_room": assigned_room,
                "hunt": target,
                "use_safe_spots": use_safe_spots,
                "guidance": quarantine.get("guidance")
                or "choose a different grounded farm room and tactic",
                "evidence": redact(quarantine),
            }

        if not isinstance(observation, dict):
            return None
        current_room = self._observation_room(observation)
        for lesson in self.storage.goal_lessons(
            statuses=["deferred", "unlocked"],
            limit=200,
        ):
            if lesson.get("classification") != "route_unavailable":
                continue
            failed_state = lesson.get("failed_state")
            failed_state = failed_state if isinstance(failed_state, dict) else {}
            failed_tactic = failed_state.get("failed_tactic")
            failed_tactic = failed_tactic if isinstance(failed_tactic, dict) else {}
            failed_arguments = failed_tactic.get("arguments")
            failed_arguments = failed_arguments if isinstance(failed_arguments, dict) else {}
            failed_destination = next(
                (
                    failed_arguments.get(key)
                    for key in ("to", "destination", "room", "assigned_room")
                    if failed_arguments.get(key) is not None
                ),
                None,
            )
            failed_room = failed_tactic.get("room", failed_state.get("room"))
            failed_origin = (
                failed_room.get("id", failed_room.get("num"))
                if isinstance(failed_room, dict)
                else failed_room
            )
            failed_corpus = failed_state.get("corpus_version")
            if (
                failed_destination is None
                or str(failed_destination) != str(assigned_room)
                or str(failed_origin) != str(current_room)
                or (
                    failed_corpus
                    and str(failed_corpus) != str(self.knowledge.corpus_version)
                )
            ):
                continue
            return {
                "kind": "retained_route_failure",
                "assigned_room": assigned_room,
                "origin_room": current_room,
                "lesson_id": lesson.get("id"),
                "guidance": (
                    str(lesson.get("summary") or "route is unavailable from the current origin")
                    + "; choose a different grounded destination or staging origin"
                ),
                "evidence": {
                    "tool": failed_tactic.get("tool"),
                    "arguments": redact(failed_arguments),
                    "corpus_version": failed_corpus,
                },
            }
        return None

    def _campaign_context(
        self,
        run: dict[str, Any],
        phase: dict[str, Any] | None,
    ) -> dict[str, Any]:
        value = self.campaign.context(run, phase)
        value["operator_contract"] = {
            "primary": "Complete the active operator-supplied strategic goal through ordinary gameplay.",
            "pvp": (
                "Use player combat only when the active goal or immediate direct-defense context calls for it."
            ),
            "finish": "Use the active goal's deterministic terminal criteria; do not add a default destination.",
        }
        return value

    def _reconcile_existing_campaign_phase(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Finish a verified phase before legacy keeper ownership can mask it."""
        run = self.storage.campaign_run(goal["id"])
        if run is None:
            return None
        phase = self.storage.active_campaign_phase(run["id"])
        if phase is None:
            return None
        outcome = self.campaign.evaluate_phase(goal, run, phase, observation)
        exhausted = (
            None
            if outcome.completed or outcome.failed
            else self.campaign.budget_exhausted(phase)
        )
        if not outcome.completed and not outcome.failed and exhausted is None:
            return None
        if outcome.completed:
            result_phase = outcome.phase
            reason = "the internal campaign phase reached its deterministic criteria"
        elif outcome.failed:
            result_phase = outcome.phase
            reason = str(
                outcome.detail.get("reason")
                or "a phase abandonment predicate was verified"
            )
        else:
            reason = (
                "internal campaign phase exhausted its bounded review budget: "
                + canonical_json(exhausted)
            )
            result_phase = self.storage.transition_campaign_phase(
                phase["id"], "failed", reason=reason, resume_parent=False
            )
        self._invalidate_execution_plan(goal, reason)
        keeper_result: Any = None
        if phase.get("kind") == "farm":
            try:
                keeper = self.broker.call_tool(
                    "autopilot",
                    {"agent": self.config.game.agent, "action": "status"},
                    timeout=10,
                    mutation=False,
                )
                if (
                    self._keeper_is_driving(keeper)
                    and str(keeper.get("mode") or "") == "farm"
                    and keeper.get("goal_id") in {None, goal["id"]}
                ):
                    keeper_result = self.broker.call_tool(
                        "autopilot",
                        {"agent": self.config.game.agent, "action": "stop"},
                        timeout=20,
                        mutation=True,
                    )
                    self.storage.set_runtime("background_farm_owner_v1", {})
            except (BrokerError, ValueError):
                # The phase result is already deterministic and durable. The
                # next turn will reconcile any keeper still owning movement.
                keeper_result = {"status": "stop_pending_reconciliation"}
        return {
            "campaign_phase_completed": outcome.completed,
            "campaign_phase_abandoned": outcome.failed,
            "campaign_phase_budget_exhausted": exhausted is not None,
            "phase": result_phase,
            "completion": outcome.detail,
            "budget": exhausted,
            "keeper": redact(keeper_result),
            "strategic_goal_preserved": True,
        }

    def _fail_active_campaign_phase(
        self, goal: dict[str, Any], reason: str
    ) -> dict[str, Any] | None:
        run = self.storage.campaign_run(goal["id"])
        phase = self.storage.active_campaign_phase(run["id"]) if run else None
        if phase is None:
            return None
        failed = self.storage.transition_campaign_phase(
            phase["id"], "failed", reason=reason, resume_parent=False
        )
        self._invalidate_execution_plan(goal, reason)
        return failed

    def turn(self) -> dict[str, Any]:
        if self.offline_diagnostics:
            return {"offline_diagnostics": True}
        if not self._turn_lock.acquire(blocking=False):
            return {"skipped": "turn already running"}
        try:
            observation = self.broker.observe()
            self.last_observation = observation
            self.storage.record_snapshot(redact(observation))
            self._record_character_progress(observation)
            self.dependencies["broker"] = "healthy"
            self.learning.refresh_unlocks(observation)
            reconciled = self._reconcile_inactive_goal_completions(observation)
            goal = self.storage.active_goal()
            if goal is None:
                onboarding = self._onboarding_turn(observation)
                return {
                    "idle": True,
                    "reconciled_goal_ids": [item["id"] for item in reconciled],
                    "onboarding": onboarding,
                }
            grounding = self.knowledge.validate_goal(goal)
            if not grounding["valid"]:
                error_codes = {
                    str(error.get("code") or "")
                    for error in grounding.get("errors", [])
                    if isinstance(error, dict)
                }
                if error_codes == {"INVALID_FARM_OPERATOR_NOTES"}:
                    reason = (
                        "active goal contains malformed farm operator notes; "
                        "replace it with the validated key=value recipe"
                    )
                    blocked = self.storage.block_goal(
                        goal["id"],
                        reason=reason,
                        blocked_reason="invalid_goal_contract",
                    )
                    self.storage.emit_event(
                        "knowledge.goal_blocked",
                        f"Blocked malformed farm goal: {goal['title']}",
                        severity="warning",
                        interesting=True,
                        goal_id=goal["id"],
                        data={
                            "errors": grounding["errors"],
                            "corpus": grounding["corpus"],
                            "replacement_allowed": True,
                        },
                    )
                    return {
                        "blocked": True,
                        "goal": blocked,
                        "grounding": grounding,
                        "replacement_allowed": True,
                    }
                deferred = self.learning.defer_goal(
                    goal,
                    observation,
                    reason=(
                        "active goal failed grounded feasibility validation: "
                        + "; ".join(
                            str(error.get("message") or error.get("code"))
                            for error in grounding.get("errors", [])[:5]
                            if isinstance(error, dict)
                        )
                    ),
                    event_kind="knowledge.goal_blocked",
                    classification="invalid_reference",
                    scope="goal",
                )
                blocked = deferred["goal"]
                self.storage.emit_event(
                    "knowledge.goal_blocked",
                    f"Blocked ungrounded goal: {goal['title']}",
                    severity="warning",
                    interesting=True,
                    goal_id=goal["id"],
                    data={"errors": grounding["errors"], "corpus": grounding["corpus"]},
                )
                return {"blocked": True, "goal": blocked, "grounding": grounding}
            self._reconcile_purchase_transaction(goal)
            completion = self.criteria.evaluate(goal, observation)
            if completion["all_met"]:
                done = self.storage.set_goal_completion(goal["id"], completion, terminal="succeeded", reason="all deterministic criteria verified")
                self.storage.complete_campaign_run(goal["id"], status="succeeded")
                self.storage.emit_event("goal.succeeded", f"Goal succeeded: {goal['title']}", interesting=True, goal_id=goal["id"], data={"completion": completion})
                self.learning.record_success(goal)
                return {"goal": done, "completed": True}
            goal = self.storage.set_goal_completion(goal["id"], completion)
            phase_completion = self._reconcile_existing_campaign_phase(
                goal, observation
            )
            if phase_completion is not None:
                return phase_completion
            expired_opportunity = self._expire_direct_pvp_opportunity(
                goal, observation, completion
            )
            if expired_opportunity is not None:
                return expired_opportunity
            if not any(
                item.get("result", {}).get("met") is not True
                for item in self._health_progress_criteria(goal, completion)
            ):
                feedback = self._planner_feedback(goal)
                feedback_message = str((feedback or {}).get("message") or "").casefold()
                if (
                    "execution plan failed deterministic verification" in feedback_message
                    and any(
                        marker in feedback_message
                        for marker in (
                            "farm launch",
                            "autopilot launch",
                            "assigned room",
                            "tos inn room 52",
                        )
                    )
                ):
                    self._clear_planner_feedback()
            # A successfully learned skill/spell normally disappears from the
            # teacher's quote. Verify the durable transaction result first;
            # re-running stock preflight after acquisition would misclassify
            # that expected disappearance as world_unavailable and block the
            # required return-home phase.
            purchase_result_met = self._purchase_result_met(goal, completion)
            purchase_preflight = (
                None
                if purchase_result_met
                else self._purchase_preflight(goal, observation)
            )
            if isinstance(purchase_preflight, dict):
                failed_statuses = {
                    "merchant_not_visible",
                    "item_not_quoted",
                    "price_exceeds_limit",
                }
                failure_is_durable = (
                    purchase_preflight.get("status") == "price_exceeds_limit"
                    or int(purchase_preflight.get("failed_checks", 0) or 0) >= 3
                )
                if purchase_preflight.get("status") in failed_statuses and failure_is_durable:
                    reason = (
                        "purchase plan failed live verification: "
                        + str(purchase_preflight.get("reason") or purchase_preflight.get("status"))
                    )
                    deferred = self.learning.defer_goal(
                        goal,
                        observation,
                        tool="purchase_preflight",
                        reason=reason,
                        event_kind="planner.preflight.failed",
                        classification="world_unavailable",
                        scope="goal",
                    )
                    self.storage.emit_event(
                        "planner.preflight.failed",
                        f"Purchase preflight blocked goal: {goal['title']}",
                        severity="warning",
                        interesting=True,
                        goal_id=goal["id"],
                        data={"preflight": redact(purchase_preflight)},
                    )
                    return {
                        "goal_blocked": True,
                        "purchase_preflight": purchase_preflight,
                        **deferred,
                    }
            advisories = self._goal_advisories(goal, observation)
            if any(item["kind"] == "survival_interrupt" for item in advisories):
                handoff = self._handoff_background_farm_to_survival(goal, observation)
                # A foreground fight/travel goal has no farm keeper for the
                # handoff helper to convert. Start recovery directly instead
                # of repeatedly suppressing planning while nothing heals or
                # withdraws the character.
                if handoff is None:
                    handoff = self._ensure_survival_keeper()
                incident = self.storage.get_runtime("survival_incident_v1", {})
                same_incident = isinstance(incident, dict) and incident.get("goal_id") == goal["id"]
                event = None
                if not same_incident:
                    event = self.storage.emit_event(
                        "survival.interrupt",
                        "Critical health: tactical planning yielded to survival autopilot",
                        severity="warning",
                        interesting=True,
                        goal_id=goal["id"],
                        data={"advisories": advisories, "keeper": redact(handoff)},
                    )
                    self.storage.set_runtime(
                        "survival_incident_v1",
                        {"goal_id": goal["id"], "started_at": time.time(), "event_id": event["id"]},
                    )
                interrupts = self.storage.goal_events(goal["id"], kinds=["survival.interrupt"], limit=100)
                if event and len(interrupts) >= self.config.learning.survival_interrupt_budget:
                    deferred = self.learning.defer_goal(
                        goal,
                        observation,
                        tool="survival_autopilot",
                        reason="Repeated critical-health interrupts show that the current goal exceeds verified combat readiness",
                        event_kind="survival.interrupt",
                        evidence_event_ids=[item["id"] for item in interrupts[-20:]],
                        classification="insufficient_combat_power",
                        scope="goal",
                    )
                    return {"goal_blocked": True, **deferred}
                return {"survival_interrupt": True, "advisories": advisories, "background_farm": handoff}
            incident = self.storage.get_runtime("survival_incident_v1", {})
            if isinstance(incident, dict) and incident.get("goal_id") == goal["id"]:
                self.storage.set_runtime("survival_incident_v1", None)
            farm_control = self._manage_background_farm(goal, observation, completion)
            if farm_control is not None:
                return farm_control
            structured_purchase = self._structured_purchase_preparation_action(
                goal, observation, completion, purchase_preflight
            )
            if structured_purchase is not None:
                execution_plan = self._execution_plan(goal)
                step_ids = {
                    str(step.get("id") or "")
                    for step in execution_plan.get("steps", [])
                    if isinstance(step, dict)
                } if isinstance(execution_plan, dict) else set()
                step_id = str(structured_purchase.get("plan_step_id") or "")
                if step_id not in step_ids:
                    stored_plan = self._store_execution_plan(
                        goal,
                        self._structured_purchase_controller_plan(goal, completion),
                        grounding=grounding,
                        revision=execution_plan is not None,
                    )
                    return {"planned": True, "execution_plan": stored_plan}
                return self._execute(goal, observation, structured_purchase)
            structured_preparation = self._structured_farm_preparation_action(
                goal, observation, completion
            )
            if structured_preparation is not None:
                execution_plan = self._execution_plan(goal)
                step_ids = {
                    str(step.get("id") or "")
                    for step in execution_plan.get("steps", [])
                    if isinstance(step, dict)
                } if isinstance(execution_plan, dict) else set()
                step_id = str(structured_preparation.get("plan_step_id") or "")
                if step_id not in step_ids:
                    stored_plan = self._store_execution_plan(
                        goal,
                        self._structured_farm_controller_plan(goal),
                        grounding=grounding,
                        revision=execution_plan is not None,
                    )
                    return {"planned": True, "execution_plan": stored_plan}
                return self._execute(goal, observation, structured_preparation)
            structured_launch = self._structured_farm_launch_plan(
                goal, observation, completion
            )
            if structured_launch is not None:
                execution_plan = self._execution_plan(goal)
                if execution_plan is None:
                    structured_intent = self._effective_farm_intent(goal)
                    execution_plan = self._store_execution_plan(
                        goal,
                        {
                            "summary": "Complete deterministic preparation, launch the goal-owned keeper, then verify the bounded outcome and home finish.",
                            "steps": [
                                {
                                    "id": "launch-goal-keeper",
                                    "outcome": (
                                        "Launch the grounded goal-owned keeper from Tos Inn for "
                                        f"{structured_intent.get('hunt')} in assigned room "
                                        f"{structured_intent.get('assigned_room')}."
                                    ),
                                    "tool": structured_launch.get("tool"),
                                    "verification": (
                                        "Keeper status reports running with this goal id, prey "
                                        f"{structured_intent.get('hunt')}, and assigned room "
                                        f"{structured_intent.get('assigned_room')}."
                                    ),
                                },
                                {
                                    "id": "verify-goal-outcome",
                                    "outcome": "Observe the deterministic success criteria and return-home criteria.",
                                    "tool": None,
                                    "verification": "The controller criteria evaluator reports all criteria met.",
                                },
                            ],
                            "assumptions": [],
                            "revision_reason": None,
                        },
                        grounding=grounding,
                        revision=False,
                    )
                    return {"planned": True, "execution_plan": execution_plan}
                structured_launch = {
                    **structured_launch,
                    "plan_step_id": "launch-goal-keeper",
                }
                return self._execute(goal, observation, structured_launch)
            campaign_run, campaign_phase, _campaign_selection = self._campaign_turn_state(
                goal, observation, grounding
            )
            page = self.storage.events(after_cursor=max(0, self.storage.get_runtime("planner_event_cursor", 0) - 12), limit=20)
            pending_proposals = self.storage.proposals()[:10]
            planner_feedback = self._planner_feedback(goal)
            grounded_context = self.knowledge.context_for(goal, redact(observation))
            grounded_context["live_overlevel_hostiles"] = self._live_overlevel_hostiles(
                observation
            )
            if purchase_preflight is not None:
                grounded_context["purchase_preflight"] = redact(purchase_preflight)
            execution_plan = self._execution_plan(goal)
            decision = self.model.plan(
                goal=goal,
                observation=redact(observation),
                tools=self._planner_tools(campaign_phase),
                persona=self.storage.persona(),
                recent_events=page["events"],
                pending_proposals=[
                    {
                        "id": proposal["id"],
                        "title": proposal["goal_draft"].get("title"),
                        "objective": proposal["goal_draft"].get("objective"),
                    }
                    for proposal in pending_proposals
                ],
                planner_feedback=planner_feedback,
                policy_summary=self.policy.summary(observation),
                financial_context=self._financial_context(observation),
                grounded_knowledge=grounded_context,
                learned_failures=self.learning.context_for(goal, redact(observation)),
                execution_plan=redact(execution_plan) if execution_plan else None,
                campaign_context=self._campaign_context(campaign_run, campaign_phase),
            )
            self.dependencies["model"] = "healthy"
            self.storage.set_runtime("planner_event_cursor", page["next_cursor"])
            if decision["decision"] == "plan":
                try:
                    stored_plan = self._store_execution_plan(
                        goal,
                        decision.get("execution_plan"),
                        grounding=grounding,
                        revision=execution_plan is not None,
                    )
                except ModelError as exc:
                    plan_rejections = int(
                        (planner_feedback or {}).get(
                            "consecutive_plan_rejections", 0
                        )
                        or 0
                    ) + 1
                    retained_steps = [
                        str(step.get("id") or "")
                        for step in execution_plan.get("steps", [])
                        if isinstance(step, dict) and step.get("tool")
                    ] if isinstance(execution_plan, dict) else []
                    if execution_plan is not None:
                        feedback_message = (
                            f"The optional plan revision failed deterministic verification: {exc}. "
                            "The existing execution plan remains verified; do not revise it again. "
                            "Return decision=act bound to one existing actionable step id: "
                            + ", ".join(retained_steps)
                            + "."
                        )
                    else:
                        feedback_message = (
                            f"The proposed execution plan failed deterministic verification: {exc}. "
                            "Return one corrected plan before selecting a tool."
                        )
                    self._set_planner_feedback(
                        goal,
                        feedback_message,
                        consecutive_plan_rejections=plan_rejections,
                    )
                    self.storage.emit_event(
                        "planner.plan.rejected",
                        f"Rejected unverifiable execution plan: {str(exc)[:240]}",
                        severity="warning",
                        interesting=plan_rejections >= 3,
                        goal_id=goal["id"],
                        data={
                            "reason": str(exc)[:1000],
                            "plan": redact(decision.get("execution_plan")),
                            "consecutive_plan_rejections": plan_rejections,
                            "retained_verified_plan": execution_plan is not None,
                        },
                    )
                    return {"plan_rejected": True, "reason": str(exc)}
                self._clear_planner_feedback()
                return {"planned": True, "execution_plan": stored_plan}
            if decision["decision"] == "act" and execution_plan is None:
                self._set_planner_feedback(
                    goal,
                    "No verified execution plan exists for this goal. Return decision=plan with 1-8 grounded, verifiable steps before selecting a tool.",
                )
                return {"plan_required": True, "action_suppressed": True}
            if decision["decision"] == "act":
                step_id = str(decision.get("plan_step_id") or "").strip()
                valid_step_ids = {
                    str(step.get("id") or "")
                    for step in execution_plan.get("steps", [])
                    if isinstance(step, dict)
                } if isinstance(execution_plan, dict) else set()
                if step_id not in valid_step_ids:
                    self._set_planner_feedback(
                        goal,
                        "The selected action was not bound to a valid execution_plan step. Return plan_step_id exactly matching one stored step, or revise the plan first.",
                    )
                    return {
                        "plan_step_required": True,
                        "action_suppressed": True,
                        "valid_step_ids": sorted(valid_step_ids),
                    }
                selected_step = next(
                    step
                    for step in execution_plan.get("steps", [])
                    if isinstance(step, dict) and str(step.get("id") or "") == step_id
                )
                selected_tool = selected_step.get("tool")
                if not selected_tool or selected_tool != decision.get("tool"):
                    self._set_planner_feedback(
                        goal,
                        "The selected action tool did not match the declared execution_plan step tool. Revise the plan or bind the action to the correct actionable step.",
                    )
                    return {
                        "plan_tool_mismatch": True,
                        "action_suppressed": True,
                        "plan_step_id": step_id,
                        "expected_tool": selected_tool,
                        "selected_tool": decision.get("tool"),
                    }
            if decision["decision"] == "wait":
                learned_wait = self.learning.check_action("planner_wait", {}, observation)
                if learned_wait:
                    self._set_planner_feedback(
                        goal,
                        "Waiting is durably known not to advance this goal in the current state. Choose a concrete tool or a materially different supporting proposal now.",
                    )
                    self.storage.emit_event(
                        "action.lesson_suppressed",
                        "Suppressed a planner wait known not to advance the goal",
                        severity="warning",
                        interesting=True,
                        goal_id=goal["id"],
                        data={"tool": "planner_wait", "lesson": learned_wait["lesson"]},
                    )
                    failed_phase = self._fail_active_campaign_phase(
                        goal,
                        "planner wait is durably known not to advance this internal phase",
                    )
                    if failed_phase is not None:
                        return {
                            "retry_suppressed": True,
                            "campaign_phase_failed": failed_phase,
                            "strategic_goal_preserved": True,
                            "learning": learned_wait,
                        }
                    return {"retry_suppressed": True, "learning": learned_wait}
                rationale = str(decision.get("rationale", ""))[:500]
                waits = int((planner_feedback or {}).get("consecutive_waits", 0)) + 1
                self._set_planner_feedback(
                    goal,
                    (
                        f"The previous {waits} planning turn(s) waited without advancing the active goal. "
                        f"Last rationale: {rationale or 'none'}. Pending proposals are inert and must not block "
                        "the active goal. Choose one concrete available tool on the next turn unless a transient "
                        "in-game condition makes every legal action unsafe or impossible."
                    ),
                    consecutive_waits=waits,
                )
                if waits == 3:
                    self.storage.emit_event(
                        "planner.stalled",
                        "Planner waited for three consecutive turns without advancing the active goal",
                        severity="warning",
                        interesting=True,
                        goal_id=goal["id"],
                        data={"last_rationale": rationale},
                    )
                if waits >= self.config.learning.wait_budget:
                    failed_phase = self._fail_active_campaign_phase(
                        goal,
                        f"planner waited {waits} consecutive turns without verified progress",
                    )
                    if failed_phase is not None:
                        return {
                            "campaign_phase_failed": failed_phase,
                            "strategic_goal_preserved": True,
                            "consecutive_waits": waits,
                        }
                    deferred = self.learning.defer_goal(
                        goal,
                        observation,
                        tool="planner_wait",
                        reason=f"Planner waited {waits} consecutive turns without verified progress: {rationale or 'no rationale'}",
                        event_kind="planner.stalled",
                        classification="ineffective_tactic",
                        scope="tactic",
                    )
                    return {
                        "goal_blocked": bool(deferred.get("goal_blocked")),
                        "tactic_deferred": not bool(deferred.get("goal_blocked")),
                        "consecutive_waits": waits,
                        **deferred,
                    }
                return {"wait": True, "rationale": rationale, "consecutive_waits": waits}
            if decision["decision"] == "propose_goal":
                proposal = decision.get("proposal")
                if not isinstance(proposal, dict):
                    if pending_proposals:
                        self._set_planner_feedback(
                            goal,
                            "The prior propose_goal decision had no proposal object and changed nothing. A pending "
                            "proposal is inert and unrelated to execution. Advance the active goal with a concrete tool.",
                        )
                        return {
                            "wait": True,
                            "rationale": "Planner referred to a proposal while one is already pending.",
                            "suppressed": "proposal_already_pending",
                        }
                    raise ModelError("planner proposed a goal without a proposal object")
                try:
                    proposal_grounding = self.knowledge.require_valid_goal(proposal)
                    self.learning.require_goal_eligible(proposal_grounding["canonical_goal"], observation)
                    created = self.storage.create_proposal(
                        proposal_grounding["canonical_goal"],
                        str(decision.get("rationale", "Bot-proposed follow-up")),
                    )
                except GoalDeferredError as exc:
                    self._set_planner_feedback(
                        goal,
                        f"The proposed follow-up is deferred: {exc}. Do not paraphrase it; choose a supporting goal from the lesson or continue the active goal.",
                    )
                    return {"proposal_suppressed": True, "reason": str(exc), "learning": exc.result}
                except (TypeError, ValueError, KnowledgeValidationError) as exc:
                    self._set_planner_feedback(
                        goal,
                        f"The prior proposal was invalid and changed nothing: {exc}. Advance the active goal with a concrete tool.",
                    )
                    raise ModelError(f"planner proposed an invalid goal: {exc}") from exc
                self._set_planner_feedback(
                    goal,
                    "The optional future proposal was stored and is now inert pending supervisor review. Do not revisit "
                    "or wait on it; resume the active goal with a concrete tool.",
                )
                return {"proposal": created}
            self._clear_planner_feedback()
            return self._execute(goal, observation, decision)
        finally:
            self._turn_lock.release()

    def _broker_action_timeout(self, tool: str) -> float:
        """Keep LLM responsiveness independent from legitimate game action time."""
        minimums = {
            # Cross-world travel advances through multiple rooms and can take
            # several minutes while remaining healthy and productive.
            "travel": 600.0,
            "rest_up": 300.0,
            "escape_underworld": 240.0,
            "leave_raza": 240.0,
            "go_through": 120.0,
            "walk_to": 120.0,
        }
        return max(
            float(self.config.model.planner_timeout_seconds),
            minimums.get(tool, 0.0),
        )

    def _execute(self, goal: dict[str, Any], observation: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        tool = str(plan.get("tool") or "")
        arguments = plan.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("planner arguments must be an object")
        if tool in CONTROLLER_ONLY_TOOLS:
            raise ModelError(f"planner cannot call controller-owned tool {tool}")
        arguments = dict(arguments)
        # Agent ids are controller-owned routing metadata. The planner never
        # chooses them, even if an older model context emits one.
        arguments.pop("agent", None)
        direct_pvp = self._direct_pvp_contract(goal)
        if direct_pvp is not None:
            if tool == PVP_SEEK_TOOL_NAME:
                raise ModelError(
                    "active goal requires pvp_engage only against the fresh local target; "
                    "pvp_seek cannot broaden it into a patrol"
                )
            if tool == PVP_TOOL_NAME and str(arguments.get("target") or "").strip().casefold() != str(
                direct_pvp["target"]
            ).strip().casefold():
                raise ModelError(
                    f"active goal permits pvp_engage only against exact target {direct_pvp['target']}"
                )
        bank_changes: dict[str, Any] = {}
        if tool == "bank" and str(arguments.get("action") or "").casefold() == "deposit":
            carried = self._carried_currency(observation)
            if carried <= 0:
                return self._complete_already_satisfied_bank_deposit(
                    goal, observation, plan, arguments
                )
            requested = arguments.get("amount")
            normalized_amount = (
                min(int(requested), carried)
                if isinstance(requested, (int, float))
                and not isinstance(requested, bool)
                and requested > 0
                else carried
            )
            if requested != normalized_amount:
                bank_changes["amount"] = {
                    "requested": requested,
                    "applied": normalized_amount,
                    "verified_carried_currency": carried,
                }
                arguments["amount"] = normalized_amount
        if tool == PVP_SEEK_TOOL_NAME:
            # Daily phases should involve different opponents.  Durable phase
            # evidence, rather than the model's memory, owns this exclusion.
            completed_targets = [
                str(item.get("target") or "").strip()
                for item in self._pvp_today_summary().get("recent", [])
                if str(item.get("target") or "").strip()
            ]
            requested_exclusions = arguments.get("exclude_targets", [])
            requested_exclusions = requested_exclusions if isinstance(requested_exclusions, list) else []
            exclusions = list(
                dict.fromkeys(
                    str(item).strip()
                    for item in [*requested_exclusions, *completed_targets]
                    if str(item).strip()
                )
            )
            if exclusions:
                arguments["exclude_targets"] = exclusions
                target = str(arguments.get("target") or "").strip()
                if target and target.casefold() in {item.casefold() for item in exclusions}:
                    # Continue the bounded goal by seeking somebody else rather
                    # than feeding a known-completed opponent back to the model.
                    arguments.pop("target", None)
        farm_intent: dict[str, Any] = {}
        if (
            tool == "autopilot"
            and arguments.get("action") == "start"
            and arguments.get("mode") == "farm"
        ):
            farm_intent = self._effective_farm_intent(goal)
            # Farm identity and deliberate strategy belong to the durable goal,
            # not to one fallible planner response.  Safety normalization below
            # still raises weak numeric thresholds.
            for field, value in farm_intent.items():
                if value is not None:
                    arguments[field] = value
        try:
            arguments, safety_changes = self._normalize_combat_arguments(
                tool,
                arguments,
                observation,
                allow_open_field=farm_intent.get("use_safe_spots") is False,
            )
        except (TypeError, ValueError) as exc:
            raise ModelError(f"planner supplied invalid {tool} safety arguments: {exc}") from exc
        safety_changes = {**bank_changes, **safety_changes}
        capabilities = self._available_tools()
        tool_spec = capabilities.get(tool)
        if tool_spec is None:
            raise ModelError(f"planner selected unknown broker tool {tool}")
        if tool_spec.accepts("agent"):
            arguments["agent"] = self.config.game.agent
        try:
            tool_spec.validate(arguments)
            if tool == PVP_TOOL_NAME:
                self.pvp.validate(arguments)
            elif tool == PVP_SEEK_TOOL_NAME:
                self.pvp.validate_seek(arguments)
        except ValueError as exc:
            raise ModelError(f"planner supplied invalid {tool} arguments: {exc}") from exc
        if self._is_combat_start(tool, arguments):
            # Model inference can take long enough for an adjacent creature to
            # change the character's health after the turn's initial snapshot.
            # Never authorize a hazardous start from that stale observation.
            observation = self.broker.observe()
            self.last_observation = observation
            self.storage.record_snapshot(redact(observation))
            if direct_pvp is not None and tool == PVP_TOOL_NAME:
                expired = self._expire_direct_pvp_opportunity(
                    goal,
                    observation,
                    self.criteria.evaluate(goal, observation),
                )
                if expired is not None:
                    return {
                        "action": tool,
                        "action_suppressed": True,
                        **expired,
                    }
        campaign_run = self.storage.campaign_run(goal["id"])
        campaign_phase = (
            self.storage.active_campaign_phase(campaign_run["id"])
            if campaign_run
            else None
        )
        phase_attempt_id, repeated_signature = self.campaign.prepare_attempt(
            campaign_phase,
            tool=tool,
            arguments=arguments,
            observation=observation,
            expected_effect=plan.get("expected_observation"),
        )
        if repeated_signature:
            breaker = self.campaign.trip_breaker(
                goal,
                campaign_phase,
                signature=repeated_signature,
                semantic_action=tool,
                failure_count=self.campaign.ACTION_FAILURE_LIMIT,
                reason="equivalent action already failed twice in the same verified state",
            )
            self._invalidate_execution_plan(
                goal, "campaign circuit breaker rejected an equivalent failed action"
            )
            return {
                "action": tool,
                "action_suppressed": True,
                "campaign_breaker": breaker,
                "strategic_goal_preserved": True,
            }

        def finish_phase_attempt(
            status: str,
            *,
            action_attempt_id: str | None = None,
            result: Any = None,
            verification: Any = None,
            reason: str = "",
        ) -> dict[str, Any]:
            return self.campaign.finish_attempt(
                goal,
                campaign_run,
                campaign_phase,
                phase_attempt_id,
                status=status,
                action_attempt_id=action_attempt_id,
                result=result,
                verification=verification,
                reason=reason,
            )

        preflight = self._safety_preflight(tool, arguments, observation, goal)
        preflight.extend(
            self._purchase_action_blockers(goal, observation, tool, arguments)
        )
        if preflight:
            phase_result = finish_phase_attempt(
                "suppressed",
                result={"blockers": preflight},
                verification={"allowed": False},
                reason="; ".join(
                    str(item.get("guidance") or item.get("kind")) for item in preflight
                ),
            )
            suppression = self._record_safety_suppression(
                goal, observation, tool, arguments, preflight
            )
            self._set_planner_feedback(
                goal,
                "The action was held for a deterministic safety preflight. Resolve these conditions before retrying: "
                + "; ".join(str(item.get("guidance") or item.get("kind")) for item in preflight),
                blocked_action={"tool": tool, "arguments": redact(arguments), "room": self._observation_room(observation)},
                safety_suppression=suppression,
            )
            count = int(suppression["same_blocker_count"])
            event_data = {
                "tool": tool,
                "arguments": redact(arguments),
                "blockers": preflight,
                **suppression,
            }
            # Preserve the first occurrence and escalation thresholds without
            # writing hundreds of identical deterministic warnings to SQLite.
            if count in {1, 3, self.config.learning.wait_budget}:
                self.storage.emit_event(
                    "action.safety_suppressed",
                    f"Held {tool} until its safety preflight is satisfied",
                    severity="warning",
                    interesting=False,
                    goal_id=goal["id"],
                    data=event_data,
                )
            if count == 3:
                self.storage.emit_event(
                    "planner.stalled",
                    f"Planner repeated the same blocked {tool} action three times",
                    severity="warning",
                    interesting=True,
                    goal_id=goal["id"],
                    data=event_data,
                )
            if phase_result.get("breaker_tripped"):
                self._invalidate_execution_plan(
                    goal, "campaign breaker ended a repeatedly safety-blocked phase"
                )
                return {
                    "action": tool,
                    "safety_suppressed": True,
                    "blockers": preflight,
                    "campaign_breaker": phase_result,
                    "strategic_goal_preserved": True,
                }
            if count >= self.config.learning.wait_budget:
                deferred = self.learning.defer_goal(
                    goal,
                    observation,
                    tool=tool,
                    arguments=arguments,
                    reason=(
                        "Repeated deterministic safety suppression exhausted the controller wait budget: "
                        + "; ".join(
                            str(item.get("guidance") or item.get("kind"))
                            for item in preflight
                        )
                    ),
                    event_kind="action.safety_suppressed",
                    classification="ineffective_tactic",
                    scope="tactic",
                    block=False,
                )
                paused = self.storage.manage_goal(
                    {
                        "request_id": f"controller-safety-stall-{uuid7()}",
                        "goal_id": goal["id"],
                        "action": "pause",
                        "reason": (
                            "controller paused the goal after the same safety blocker "
                            f"repeated {count} times"
                        ),
                    }
                )["goal"]
                return {
                    "action": tool,
                    "safety_suppressed": True,
                    "goal_paused": True,
                    "goal": paused,
                    "blockers": preflight,
                    "suppression": suppression,
                    **deferred,
                }
            return {
                "action": tool,
                "safety_suppressed": True,
                "blockers": preflight,
                "suppression": suppression,
            }
        self._clear_safety_suppression(goal["id"])
        if safety_changes:
            self.storage.emit_event(
                "action.safety_normalized",
                f"Applied controller combat safety defaults to {tool}",
                severity="info",
                interesting=False,
                goal_id=goal["id"],
                data={"tool": tool, "changes": safety_changes},
            )
        learned_block = self.learning.check_action(tool, arguments, observation)
        if learned_block:
            lesson = learned_block["lesson"]
            failure_reason = str(lesson.get("summary") or "")
            recorded_block = self._blocked_action(goal, observation, tool, arguments)
            if recorded_block and is_inventory_capacity_refusal(recorded_block.get("reason")):
                failure_reason = str(recorded_block.get("reason") or failure_reason)
            capacity_refusal = tool == "shop" and is_inventory_capacity_refusal(failure_reason)
            invalidates_plan = self._failure_invalidates_plan(tool, failure_reason)
            runtime_key = f"lesson_suppression:{goal['id']}:{lesson['id']}"
            suppressed_count = int(self.storage.get_runtime(runtime_key, 0) or 0) + 1
            self.storage.set_runtime(runtime_key, suppressed_count)
            if invalidates_plan:
                self._invalidate_execution_plan(goal, failure_reason)
            phase_result = finish_phase_attempt(
                "suppressed",
                result={"lesson": lesson},
                verification={"durably_deferred": True},
                reason=failure_reason or "tactic is durably deferred",
            )
            if campaign_phase is not None and not phase_result.get("breaker_tripped"):
                # A durable tactic lesson already represents the required repeated,
                # unchanged failure evidence. Do not spend two more phase attempts
                # rediscovering it just because the campaign manager selected a new
                # phase wrapper around the same semantic action.
                phase_result = self.campaign.trip_breaker(
                    goal,
                    campaign_phase,
                    signature=self.campaign.action_signature(
                        campaign_phase,
                        tool,
                        arguments,
                        observation,
                        plan.get("expected_observation"),
                    ),
                    semantic_action=tool,
                    failure_count=self.campaign.ACTION_FAILURE_LIMIT,
                    reason=(
                        failure_reason
                        or "the selected tactic remains durably deferred in unchanged state"
                    ),
                )
            self._set_planner_feedback(
                goal,
                (
                    self._no_progress_guidance(
                        tool,
                        failure_reason,
                        repeated=True,
                        observation=observation,
                    )
                    if invalidates_plan
                    else f"This exact tactic is durably deferred: {lesson.get('summary')}. Choose different arguments or a different tool; do not repeat it until its retry condition is met."
                ),
                blocked_action={"tool": tool, "arguments": redact(arguments), "room": self._observation_room(observation)},
                failure_context=self._failure_context(tool, failure_reason, observation),
            )
            self.storage.emit_event(
                "action.lesson_suppressed",
                f"Suppressed a tactic known not to work in the current state: {tool}",
                severity="warning",
                interesting=suppressed_count >= self.config.learning.repeated_tactic_budget,
                goal_id=goal["id"],
                data={"tool": tool, "arguments": redact(arguments), "lesson": lesson, "suppressed_count": suppressed_count},
            )
            if phase_result.get("breaker_tripped"):
                self._invalidate_execution_plan(
                    goal, "campaign breaker ended a phase repeating a deferred tactic"
                )
                return {
                    "action": tool,
                    "retry_suppressed": True,
                    "campaign_breaker": phase_result,
                    "strategic_goal_preserved": True,
                }
            return {"action": tool, "retry_suppressed": True, "learning": learned_block, "suppressed_count": suppressed_count}
        blocked = self._blocked_action(goal, observation, tool, arguments)
        if blocked:
            blocked_reason = str(blocked.get("reason") or "")
            capacity_refusal = tool == "shop" and is_inventory_capacity_refusal(blocked_reason)
            blocked["suppressed_count"] = int(blocked.get("suppressed_count", 0)) + 1
            self._save_blocked_action(blocked)
            phase_result = finish_phase_attempt(
                "suppressed",
                result=blocked,
                verification={"known_no_progress": True},
                reason=blocked_reason,
            )
            if self._failure_invalidates_plan(tool, blocked_reason):
                self._invalidate_execution_plan(goal, blocked_reason)
            self._set_planner_feedback(
                goal,
                self._no_progress_guidance(
                    tool,
                    blocked_reason,
                    repeated=True,
                    observation=observation,
                ),
                blocked_action={"tool": tool, "arguments": redact(arguments), "room": blocked.get("room")},
                failure_context=self._failure_context(tool, blocked_reason, observation),
            )
            if blocked["suppressed_count"] in {1, 3}:
                self.storage.emit_event(
                    "action.retry_suppressed",
                    f"Suppressed repeated no-progress action: {tool}",
                    severity="warning",
                    interesting=blocked["suppressed_count"] == 3,
                    goal_id=goal["id"],
                    data={
                        "tool": tool,
                        "arguments": redact(arguments),
                        "reason": blocked.get("reason"),
                        "suppressed_count": blocked["suppressed_count"],
                    },
                )
            if phase_result.get("breaker_tripped"):
                self._invalidate_execution_plan(
                    goal, "campaign breaker ended a phase repeating a known no-progress action"
                )
                return {
                    "action": tool,
                    "retry_suppressed": True,
                    "reason": blocked_reason,
                    "campaign_breaker": phase_result,
                    "strategic_goal_preserved": True,
                }
            if blocked["suppressed_count"] >= self.config.learning.repeated_tactic_budget:
                invalid_reference = tool in {"map", KNOWLEDGE_TOOL_NAME} and "no match" in str(blocked.get("reason", "")).casefold()
                insufficient_funds = tool == "shop" and any(
                    marker in blocked_reason.casefold()
                    for marker in ("enough money", "insufficient fund", "cannot afford")
                )
                reason = (
                    "repeated authoritative lookup returned no matching game entity"
                    if invalid_reference
                    else (
                        f"the exact {tool} tactic repeatedly made no progress in the same state: {blocked_reason}"
                        if insufficient_funds
                        else f"the exact {tool} tactic repeatedly made no progress in the same state"
                    )
                )
                deferred = self.learning.defer_goal(
                    goal,
                    observation,
                    tool=tool,
                    arguments=arguments,
                    reason=reason,
                    event_kind="action.retry_suppressed",
                    classification="invalid_reference" if invalid_reference else None,
                    # Goal activation already validates references from the
                    # operator-authored goal against the knowledge corpus.  A
                    # later zero-match map/search string was invented by the
                    # tactical planner, so quarantine that exact lookup rather
                    # than falsely declaring the valid goal impossible.
                    scope="tactic",
                    retry_when=(
                        {
                            "mode": "any",
                            "conditions": [
                                {
                                    "kind": "numeric_increase",
                                    "field": "carried_currency",
                                    "from": int(blocked.get("carried_currency", 0) or 0),
                                }
                            ],
                        }
                        if insufficient_funds
                        else (
                            {
                                "mode": "any",
                                "conditions": [
                                    {
                                        "kind": "component_changed",
                                        "field": "inventory_load_hash",
                                        "from": blocked.get("inventory_load_hash"),
                                    }
                                ],
                            }
                            if capacity_refusal
                            else None
                        )
                    ),
                )
                goal_blocked = bool(deferred.get("goal_blocked"))
                return {
                    "action": tool,
                    "goal_blocked": goal_blocked,
                    "tactic_deferred": not goal_blocked,
                    **deferred,
                    "reason": blocked.get("reason"),
                    "suppressed_count": blocked["suppressed_count"],
                }
            return {
                "action": tool,
                "retry_suppressed": True,
                "reason": blocked.get("reason"),
                "suppressed_count": blocked["suppressed_count"],
            }
        known = set(capabilities)
        policy = self.policy.evaluate(tool, arguments, observation, goal, known_tools=known)
        correlation_id = uuid7()
        self.storage.emit_event(
            "policy.authorized" if policy.decision != "deny" else "policy.denied",
            policy.summary,
            interesting=policy.notify,
            severity="warning" if policy.decision == "deny" else "info",
            goal_id=goal["id"],
            data=policy.as_dict(),
            correlation_id=correlation_id,
            policy_decision_id=policy.id,
        )
        if policy.decision == "deny":
            phase_result = finish_phase_attempt(
                "suppressed",
                result=policy.as_dict(),
                verification={"policy_allowed": False},
                reason=policy.summary,
            )
            return {"denied": True, "policy": policy.as_dict()}
        rationale = str(plan.get("rationale", ""))[:1000]
        attempt_id = self.storage.create_action_attempt(goal["id"], observation.get("id"), tool, arguments, rationale, policy.id, correlation_id)
        assessment_id: str | None = None
        if policy.decision == "allow_with_caution":
            assessment = self.policy.consequence_assessment(policy, goal, rationale)
            assessment["action_attempt_id"] = attempt_id
            assessment_id = self.storage.record_consequence(assessment)["id"]
            if tool == "reroll" and arguments.get("action") == "reroll":
                arguments["confirm"] = True
        self.storage.update_action_attempt(attempt_id, "sent")
        try:
            if tool == KNOWLEDGE_TOOL_NAME:
                self.knowledge.validate_tool_arguments(arguments)
                result = self.knowledge.search(
                    str(arguments["query"]),
                    kinds=arguments.get("kinds"),
                    limit=int(arguments.get("limit", 5)),
                )
            else:
                self._begin_foreground_action(tool, goal_id=goal["id"])
                try:
                    if tool == PVP_TOOL_NAME:
                        result = self.pvp.engage(arguments, timeout=self.config.model.planner_timeout_seconds)
                        self._emit_pvp_result(goal, result, correlation_id=correlation_id, policy_decision_id=policy.id)
                    elif tool == PVP_SEEK_TOOL_NAME:
                        result = self.pvp.seek(
                            arguments,
                            timeout=max(360, self.config.model.planner_timeout_seconds),
                        )
                        self._emit_pvp_result(goal, result, correlation_id=correlation_id, policy_decision_id=policy.id)
                    else:
                        if tool in MOVEMENT_TOOLS and "rest" in capabilities:
                            stood = self.broker.call_tool(
                                "rest",
                                {"agent": self.config.game.agent, "stand": True},
                                timeout=10,
                                mutation=True,
                            )
                            self.storage.emit_event(
                                "action.movement_prepared",
                                "Stood the character up before movement",
                                severity="info",
                                interesting=False,
                                goal_id=goal["id"],
                                data={"tool": tool, "result": redact(stood)},
                                correlation_id=correlation_id,
                                policy_decision_id=policy.id,
                            )
                        if tool in MOVEMENT_TOOLS and "look" in capabilities:
                            # The ordinary client can temporarily lose its self
                            # object across posture/room-content transitions. A
                            # look in a prior planner turn is not sufficient;
                            # refresh position immediately before movement.
                            self.broker.call_tool(
                                "look",
                                {"agent": self.config.game.agent},
                                timeout=20,
                                mutation=False,
                            )
                        result = self.broker.call_tool(
                            tool,
                            arguments,
                            timeout=self._broker_action_timeout(tool),
                            mutation=True,
                        )
                        position_unknown = (
                            tool in MOVEMENT_TOOLS
                            and isinstance(result, dict)
                            and result.get("arrived") is False
                            and "own position unknown"
                            in str(result.get("reason") or "").casefold()
                        )
                        if position_unknown and "look" in capabilities:
                            # The failed response proves that no movement was
                            # accepted, so one immediate relocalize-and-retry is
                            # safe. Never turn this transient client-state loss
                            # into a route lesson or a whole-goal failure.
                            refreshed = self.broker.call_tool(
                                "look",
                                {"agent": self.config.game.agent},
                                timeout=20,
                                mutation=False,
                            )
                            result = self.broker.call_tool(
                                tool,
                                arguments,
                                timeout=self._broker_action_timeout(tool),
                                mutation=True,
                            )
                            self.storage.emit_event(
                                "action.movement_relocalized",
                                "Relocalized the character and retried movement once",
                                severity="info",
                                interesting=False,
                                goal_id=goal["id"],
                                data={
                                    "tool": tool,
                                    "look": redact(refreshed),
                                    "result": redact(result),
                                },
                                correlation_id=correlation_id,
                                policy_decision_id=policy.id,
                            )
                finally:
                    self._end_foreground_action()
            post_action = (
                self.broker.observe()
                if tool
                in {
                    "fight",
                    *PVP_TOOL_NAMES,
                    "bank",
                    "shop",
                    "sell",
                    "sell_all",
                    "trade",
                    "act",
                    "equip_best",
                    "wear_best",
                }
                else None
            )
            if post_action is not None:
                self.last_observation = post_action
                self.storage.record_snapshot(redact(post_action))
            self._record_plan_action(
                goal,
                step_id=str(plan.get("plan_step_id") or "controller-owned-step"),
                tool=tool,
                result=result,
            )
            if post_action is not None and (
                tool == "fight"
                or tool == PVP_TOOL_NAME
                or (tool == PVP_SEEK_TOOL_NAME and isinstance(result, dict) and result.get("engaged") is True)
            ):
                died = bool(
                    isinstance(result, dict) and result.get("died") is True
                    or (not self._underworld(observation) and self._underworld(post_action))
                )
                if died:
                    self.storage.update_action_attempt(attempt_id, "failed", result=redact(result), error_code="CHARACTER_DIED")
                    finish_phase_attempt(
                        "failed",
                        action_attempt_id=attempt_id,
                        result=result,
                        verification={"died": True},
                        reason="character died during the internal phase",
                    )
                    death = self._record_death(
                        goal,
                        tool=tool,
                        arguments=arguments,
                        before=observation,
                        after=post_action,
                        result=result,
                        correlation_id=correlation_id,
                        policy_decision_id=policy.id,
                    )
                    if assessment_id:
                        self.storage.complete_consequence(assessment_id, outcome={"died": True, "result": redact(result)}, succeeded=False)
                    return {"action": tool, "failed": True, "died": True, **death}
                combat = self.learning.record_combat_outcome(
                    tool=tool,
                    arguments=arguments,
                    before=observation,
                    result=result,
                    after=post_action,
                )
                self.storage.emit_event(
                    "combat.encounter.completed",
                    f"Combat encounter completed: {tool}",
                    severity="warning" if combat.get("outcome") == "disengaged" else "info",
                    interesting=combat.get("outcome") == "disengaged",
                    goal_id=goal["id"],
                    data=combat,
                    correlation_id=correlation_id,
                    policy_decision_id=policy.id,
                )
            no_progress = self._no_progress_reason(
                result,
                observation,
                tool=tool,
                arguments=arguments,
                after_observation=post_action,
            )
            if no_progress is None:
                no_progress = self._repeated_evidence_reason(tool, arguments, result, observation)
            if no_progress:
                failure_observation = post_action or observation
                self.storage.update_action_attempt(attempt_id, "failed", result=redact(result), error_code="NO_PROGRESS")
                phase_result = finish_phase_attempt(
                    "failed",
                    action_attempt_id=attempt_id,
                    result=result,
                    verification={"no_progress": True, "reason": no_progress},
                    reason=no_progress,
                )
                event = self.storage.emit_event(
                    "action.no_progress",
                    f"Action made no progress: {tool}",
                    severity="warning",
                    interesting=False,
                    goal_id=goal["id"],
                    data={"tool": tool, "arguments": redact(arguments), "room": redact(deep_get(observation, "look.room")), "reason": no_progress, "result": redact(result), "attempt_id": attempt_id},
                    correlation_id=correlation_id,
                    policy_decision_id=policy.id,
                )
                pvp_route_outcome = (
                    tool == PVP_SEEK_TOOL_NAME
                    and isinstance(result, dict)
                    and result.get("outcome") in {"route_unavailable", "travel_error"}
                )
                pvp_route_failure = (
                    result.get("route_failure")
                    if pvp_route_outcome
                    and isinstance(result.get("route_failure"), dict)
                    else {}
                )
                if pvp_route_outcome:
                    self.storage.set_runtime(
                        PVP_ROUTE_FAILURE_RUNTIME_KEY,
                        {
                            "goal_id": goal["id"],
                            "outcome": result.get("outcome"),
                            "actual_room_id": pvp_route_failure.get("actual_room_id"),
                            "actual_room_name": pvp_route_failure.get("actual_room_name"),
                            "requested_room_id": pvp_route_failure.get("requested_room_id"),
                            "failed_hop": redact(pvp_route_failure.get("failed_hop")),
                            "reason": pvp_route_failure.get("reason") or no_progress,
                            "corpus_version": self.knowledge.corpus_version,
                            "recorded_at": timestamp(),
                        },
                    )
                if self._failure_invalidates_plan(tool, no_progress):
                    self._invalidate_execution_plan(goal, no_progress)
                self._set_planner_feedback(
                    goal,
                    self._no_progress_guidance(
                        tool,
                        no_progress,
                        observation=failure_observation,
                    ),
                    blocked_action={
                        "tool": tool,
                        "arguments": redact(arguments),
                        "room": self._observation_room(observation),
                    },
                    failure_context=self._failure_context(
                        tool,
                        no_progress,
                        failure_observation,
                    ),
                )
                self._record_blocked_action(
                    goal,
                    failure_observation,
                    tool,
                    arguments,
                    no_progress,
                )
                if phase_result.get("breaker_tripped"):
                    self._invalidate_execution_plan(
                        goal, "campaign breaker ended a phase after repeated semantic failure"
                    )
                    if assessment_id:
                        self.storage.complete_consequence(
                            assessment_id,
                            outcome={
                                "no_progress": True,
                                "reason": no_progress,
                                "campaign_breaker": phase_result,
                            },
                            succeeded=False,
                        )
                    return {
                        "action": tool,
                        "no_progress": True,
                        "reason": no_progress,
                        "campaign_breaker": phase_result,
                        "strategic_goal_preserved": True,
                    }
                if pvp_route_outcome:
                    lesson_arguments = {
                        "actual_room_id": pvp_route_failure.get("actual_room_id"),
                        "requested_room_id": pvp_route_failure.get("requested_room_id"),
                        "failed_hop": pvp_route_failure.get("failed_hop"),
                    }
                    deferred = self.learning.defer_goal(
                        goal,
                        observation,
                        tool=tool,
                        arguments=lesson_arguments,
                        reason=no_progress,
                        event_kind="pvp.search.failed",
                        evidence_event_ids=[event["id"]],
                        classification=(
                            "route_unavailable"
                            if result.get("outcome") == "route_unavailable"
                            else "dependency_failure"
                        ),
                        scope="tactic",
                        block=False,
                    )
                else:
                    deferred = self.learning.maybe_defer(
                        goal,
                        observation,
                        tool=tool,
                        arguments=arguments,
                        reason=no_progress,
                        event=event,
                    )
                if assessment_id:
                    self.storage.complete_consequence(
                        assessment_id,
                        outcome={"no_progress": True, "reason": no_progress, "result": redact(result)},
                        succeeded=False,
                    )
                if deferred:
                    return {
                        "action": tool,
                        "goal_blocked": bool(deferred.get("goal_blocked")),
                        "tactic_deferred": not bool(deferred.get("goal_blocked")),
                        "reason": no_progress,
                        **deferred,
                    }
                return {"action": tool, "no_progress": True, "reason": no_progress, "result": redact(result)}
            if (
                tool == "bank"
                and arguments.get("action") == "deposit"
                and post_action is not None
            ):
                self._record_bank_receipt(goal, observation, post_action)
            # Feedback describes the immediately previous failed/suppressed
            # tactic. Once any concrete action succeeds it is stale and can
            # mislead both the next planner turn and supervisor status into
            # treating a recovered route as still blocked.
            self._clear_planner_feedback()
            self.storage.update_action_attempt(attempt_id, "succeeded", result=redact(result))
            finish_phase_attempt(
                "succeeded",
                action_attempt_id=attempt_id,
                result=result,
                verification={"no_progress": False},
            )
            event = self.storage.emit_event(
                "action.succeeded",
                f"Action succeeded: {tool}",
                interesting=False,
                goal_id=goal["id"],
                data={"tool": tool, "result": redact(result), "attempt_id": attempt_id},
                correlation_id=correlation_id,
                policy_decision_id=policy.id,
            )
            if tool == "shop" and isinstance(result, dict):
                self._emit_shop_property_transaction(
                    goal,
                    result,
                    correlation_id=correlation_id,
                    policy_decision_id=policy.id,
                )
            if assessment_id:
                self.storage.complete_consequence(assessment_id, outcome={"action_event_id": event["id"], "result": redact(result)}, succeeded=True)
            self.last_observation = post_action or self.broker.observe()
            if (
                tool == "autopilot"
                and arguments.get("action") == "start"
                and arguments.get("mode") == "farm"
            ):
                # Failure handling is per launch, not per durable goal. A prior
                # route retreat may have been corrected and the goal resumed;
                # without resetting this marker, a later independent room hazard
                # would be quarantined but could not pause the goal, leaving the
                # planner to repeat a permanently blocked launch.
                self.storage.set_runtime(
                    f"background_farm_failure_handled_v1:{goal['id']}", False
                )
                self.storage.set_runtime(
                    f"background_farm_route_failure_handled_v1:{goal['id']}", False
                )
                self.storage.set_runtime(
                    "background_farm_owner_v1",
                    {
                        "goal_id": goal["id"],
                        "assigned_room": arguments.get("assigned_room"),
                        "hunt": str(arguments.get("hunt") or "").strip().casefold(),
                        "use_safe_spots": arguments.get("use_safe_spots"),
                        "origin_room": self._observation_room(observation),
                        "started_at": timestamp(),
                    },
                )
                self.storage.set_runtime(
                    f"background_farm_snapshot_v2:{goal['id']}",
                    {
                        "observed_at": timestamp(),
                        "room": arguments.get("assigned_room"),
                        "target": arguments.get("hunt"),
                        "use_safe_spots": arguments.get("use_safe_spots"),
                        "counters": {
                            name: self._farm_counter(result, name)
                            for name in (
                                "kills",
                                "deaths",
                                "withdrawals",
                                "mulligans",
                                "logoffs",
                                "deaths_in_safe_spot",
                                "deaths_in_proven_safe_spot",
                            )
                        },
                        "healing_supply_count": self.learning.profile(observation).get(
                            "healing_supply_count", 0
                        ),
                        "safe_spot": None,
                        "activity": "starting",
                        "health_fraction": self._vital_fraction(observation, "health"),
                        "flee_threshold": arguments.get("flee_below"),
                        "pass_floor": (
                            int(result.get("passes"))
                            if isinstance(result, dict)
                            and isinstance(result.get("passes"), (int, float))
                            else None
                        ),
                        # The keeper uses Date.now() for journal ids. Establish
                        # the launch boundary even when its compact tail has no
                        # kill record, so cumulative history cannot be replayed.
                        "last_kill_at": int(time.time() * 1000),
                    },
                )
            completion = self.criteria.evaluate(goal, self.last_observation)
            self.storage.set_goal_completion(goal["id"], completion, terminal="succeeded" if completion["all_met"] else None)
            if completion["all_met"]:
                self.storage.complete_campaign_run(goal["id"], status="succeeded")
                self.storage.emit_event("goal.succeeded", f"Goal succeeded: {goal['title']}", interesting=True, goal_id=goal["id"], data={"completion": completion})
                self.learning.record_success(goal)
            return {"action": tool, "result": redact(result), "completion": completion}
        except ToolCallError as exc:
            self.storage.update_action_attempt(attempt_id, "failed", error_code=exc.code)
            phase_result = finish_phase_attempt(
                "failed",
                action_attempt_id=attempt_id,
                result={"error": str(exc)[:500], "code": exc.code},
                verification={"tool_call_succeeded": False},
                reason=str(exc),
            )
            reconciled = self._reconcile_after_action_error(error=str(exc))
            event = self.storage.emit_event(
                "action.failed",
                f"Action failed: {tool}",
                severity="warning",
                interesting=tool == "fight" or tool in PVP_TOOL_NAMES,
                goal_id=goal["id"],
                data={
                    "tool": tool,
                    "arguments": redact(arguments),
                    "room": redact(deep_get(observation, "look.room")),
                    "error": str(exc)[:500],
                    "attempt_id": attempt_id,
                    "reconciled_state": None if reconciled is None else {
                        "room": redact(deep_get(reconciled, "look.room")),
                        "vitals": redact(deep_get(reconciled, "status.vitals", deep_get(reconciled, "look.vitals", {}))),
                    },
                },
                correlation_id=correlation_id,
                policy_decision_id=policy.id,
            )
            died = bool(
                (tool == "fight" or tool in PVP_TOOL_NAMES)
                and reconciled is not None
                and not self._underworld(observation)
                and self._underworld(reconciled)
            )
            if died and reconciled is not None:
                death = self._record_death(
                    goal,
                    tool=tool,
                    arguments=arguments,
                    before=observation,
                    after=reconciled,
                    error=str(exc),
                    correlation_id=correlation_id,
                    policy_decision_id=policy.id,
                )
                if assessment_id:
                    self.storage.complete_consequence(assessment_id, outcome={"error": str(exc)[:500], "died": True}, succeeded=False)
                return {"action": tool, "goal_blocked": True, "failed": True, "died": True, "error": str(exc), **death}
            if phase_result.get("breaker_tripped"):
                self._invalidate_execution_plan(
                    goal, "campaign breaker ended a phase after repeated broker rejection"
                )
                if assessment_id:
                    self.storage.complete_consequence(
                        assessment_id,
                        outcome={"error": str(exc)[:500], "campaign_breaker": phase_result},
                        succeeded=False,
                    )
                return {
                    "action": tool,
                    "failed": True,
                    "error": str(exc),
                    "campaign_breaker": phase_result,
                    "strategic_goal_preserved": True,
                }
            if tool == "fight" or tool in PVP_TOOL_NAMES:
                self.learning.record_combat_outcome(
                    tool=tool,
                    arguments=arguments,
                    before=observation,
                    after=reconciled,
                    error=str(exc),
                    died=False,
                )
            deferred = self.learning.maybe_defer(goal, observation, tool=tool, arguments=arguments, reason=str(exc), event=event)
            if assessment_id:
                self.storage.complete_consequence(assessment_id, outcome={"error": str(exc)[:500]}, succeeded=False)
            if deferred:
                return {
                    "action": tool,
                    "goal_blocked": bool(deferred.get("goal_blocked")),
                    "tactic_deferred": not bool(deferred.get("goal_blocked")),
                    "error": str(exc),
                    **deferred,
                }
            return {"action": tool, "failed": True, "error": str(exc)}
        except BrokerError:
            self.storage.update_action_attempt(attempt_id, "unknown", error_code="AMBIGUOUS_ACTION_RESULT")
            finish_phase_attempt(
                "unknown",
                action_attempt_id=attempt_id,
                verification={"result_ambiguous": True},
                reason="broker failure left mutation outcome ambiguous",
            )
            self.storage.emit_event("action.unknown", f"Action result unknown after broker failure: {tool}", severity="critical", interesting=True, goal_id=goal["id"], data={"tool": tool, "attempt_id": attempt_id}, correlation_id=correlation_id, policy_decision_id=policy.id)
            self.storage.block_goal(
                goal["id"],
                reason="mutation result could not be reconciled safely; refresh evidence before resuming",
                blocked_reason="unknown_external_state",
            )
            raise

    def _emit_pvp_result(
        self,
        goal: dict[str, Any],
        result: Any,
        *,
        correlation_id: str,
        policy_decision_id: str,
    ) -> None:
        if not isinstance(result, dict):
            return
        search = result.get("search") if isinstance(result.get("search"), dict) else None
        if search is not None:
            visits = search.get("rooms_visited") if isinstance(search.get("rooms_visited"), list) else []
            distinct_rooms = sorted(
                {
                    int(item["room_id"])
                    for item in visits
                    if isinstance(item, dict)
                    and item.get("arrived") is True
                    and isinstance(item.get("room_id"), int)
                    and not isinstance(item.get("room_id"), bool)
                }
            )
            search_failed = result.get("outcome") in {
                "route_unavailable",
                "travel_error",
            }
            route_failure = (
                result.get("route_failure")
                if isinstance(result.get("route_failure"), dict)
                else None
            )
            self.storage.emit_event(
                "pvp.search.failed" if search_failed else "pvp.search.completed",
                (
                    (
                        "PvP patrol failed before completing its route"
                        if search_failed
                        else f"PvP patrol searched {len(distinct_rooms)} room(s)"
                    )
                    + (f" and acquired {deep_get(result, 'target.name')}" if deep_get(result, "target.name") else "")
                ),
                severity="warning" if search_failed or result.get("outcome") in {"search_timeout", "search_aborted_low_health"} else "info",
                interesting=result.get("outcome") == "search_aborted_low_health",
                goal_id=goal["id"],
                data={
                    "outcome": result.get("outcome"),
                    "reason": result.get("reason"),
                    "target_requested": result.get("target_requested"),
                    "requested_route": search.get("requested_route"),
                    "route": search.get("route"),
                    "completed_patrol": search.get("completed_patrol") is True,
                    "skipped_rooms": redact(search.get("skipped_rooms", [])),
                    "guild_eligibility_verified": search.get("guild_eligibility_verified"),
                    "distinct_rooms": distinct_rooms,
                    "rooms_visited": redact(visits),
                    "route_failure": redact(route_failure),
                    "route_failures": redact(search.get("route_failures", [])),
                    "online_candidates": redact(search.get("online_candidates", [])),
                    "target_acquired": redact(result.get("target")),
                },
                correlation_id=correlation_id,
                policy_decision_id=policy_decision_id,
            )

        engagement = result.get("engagement") if isinstance(result.get("engagement"), dict) else result
        outcome = str(engagement.get("outcome", result.get("outcome", "unknown")))
        target = engagement.get("target") if isinstance(engagement.get("target"), dict) else {}
        target_name = target.get("name") or engagement.get("target_requested") or result.get("target_requested") or "unknown player"
        accepted_swings = int(engagement.get("accepted_swings", 0) or 0)
        engaged = engagement.get("engaged") is True
        if not engaged and accepted_swings <= 0:
            self.storage.emit_event(
                "pvp.engagement.skipped",
                f"PvP engagement with {target_name} did not start: {outcome.replace('_', ' ')}",
                severity="info",
                interesting=False,
                goal_id=goal["id"],
                data={
                    "target": target or {"name": target_name},
                    "outcome": outcome,
                    "accepted_swings": 0,
                    "reason": engagement.get("reason"),
                    "refused_room": engagement.get("refused_room"),
                    "room_policy": engagement.get("room_policy"),
                },
                correlation_id=correlation_id,
                policy_decision_id=policy_decision_id,
            )
            return

        loot = engagement.get("loot") if isinstance(engagement.get("loot"), dict) else None
        loot_attempted = bool(loot and loot.get("attempted") is True)
        items_taken = loot.get("items_taken", []) if loot_attempted and isinstance(loot.get("items_taken"), list) else []
        items_taken_count = int(loot.get("items_taken_count", 0) or 0) if loot_attempted else 0
        qualifying_phase = bool(
            outcome == "target_left_or_defeated"
            and accepted_swings > 0
            and loot_attempted
        )
        severity = "critical" if engagement.get("cleanup_errors") or result.get("cleanup_errors") else (
            "warning" if outcome in {"disengaged_low_health", "attack_refused"} else "notice"
        )
        self.storage.emit_event(
            "pvp.engagement.completed",
            f"PvP engagement with {target_name}: {outcome.replace('_', ' ')}",
            severity=severity,
            interesting=True,
            goal_id=goal["id"],
            data={
                "target": target or {"name": target_name},
                "outcome": outcome,
                "rounds": len(engagement.get("rounds", [])) if isinstance(engagement.get("rounds"), list) else 0,
                "accepted_swings": accepted_swings,
                "loot_attempted": loot_attempted,
                "items_taken_count": items_taken_count,
                "qualifying_phase": qualifying_phase,
                "disengage_at": engagement.get("disengage_at"),
                "cleanup": engagement.get("cleanup"),
                "cleanup_errors": engagement.get("cleanup_errors"),
                "reason": engagement.get("reason"),
                "refused_room": engagement.get("refused_room"),
            },
            correlation_id=correlation_id,
            policy_decision_id=policy_decision_id,
        )
        if loot_attempted:
            self.storage.emit_event(
                "pvp.loot.completed",
                f"PvP loot sweep after engaging {target_name}: {items_taken_count} item(s) taken",
                severity="notice" if items_taken_count else "info",
                interesting=items_taken_count > 0,
                goal_id=goal["id"],
                data={
                    "target": target or {"name": target_name},
                    "items_taken": redact(items_taken),
                    "items_taken_count": items_taken_count,
                    "nothing_available": items_taken_count == 0,
                },
                correlation_id=correlation_id,
                policy_decision_id=policy_decision_id,
            )
        if items_taken_count > 0:
            self.storage.emit_event(
                "property.transaction",
                f"PvP loot transaction after engaging {target_name}: {items_taken_count} item(s) taken",
                severity="notice",
                interesting=True,
                goal_id=goal["id"],
                data={
                    "transaction": "pvp_loot",
                    "target": target or {"name": target_name},
                    "items_taken": redact(items_taken),
                    "items_taken_count": items_taken_count,
                    "protected_or_valuable": True,
                    "approval_required": False,
                },
                correlation_id=correlation_id,
                policy_decision_id=policy_decision_id,
            )
        if qualifying_phase:
            self.storage.emit_event(
                "pvp.phase.completed",
                f"Observed qualifying PvP phase against {target_name}",
                severity="notice",
                interesting=True,
                goal_id=goal["id"],
                data={
                    "target": target or {"name": target_name},
                    "outcome": outcome,
                    "accepted_swings": accepted_swings,
                    "loot_attempted": True,
                    "items_taken": redact(items_taken),
                    "items_taken_count": items_taken_count,
                    "protocol_limit": "target departure cannot be distinguished from defeat",
                },
                correlation_id=correlation_id,
                policy_decision_id=policy_decision_id,
            )

    def _repeated_evidence_reason(
        self,
        tool: str,
        arguments: dict[str, Any],
        result: Any,
        observation: dict[str, Any],
    ) -> str | None:
        """Treat an identical static lookup replay as non-progress.

        Catalog and corpus lookups are useful evidence once.  Without this
        guard, the planner can repeat the same successful read forever while
        never acting on the result, and the normal failure budget never sees
        the loop because the RPC itself succeeded.
        """
        evidence_tools = {
            "map",
            "merchants",
            "prey",
            "hunting_grounds",
            KNOWLEDGE_TOOL_NAME,
        }
        is_bank_balance = tool == "bank" and str(
            arguments.get("action") or ""
        ).casefold() == "balance"
        if tool not in evidence_tools and not is_bank_balance:
            return None
        signature = canonical_json(
            {
                "tool": tool,
                "arguments": arguments,
                "room": self._observation_room(observation),
            }
        )
        result_fingerprint = canonical_json(redact(result))
        entries = self.storage.get_runtime("evidence_lookup_cache_v1", [])
        if not isinstance(entries, list):
            entries = []
        prior = next(
            (
                entry
                for entry in entries
                if isinstance(entry, dict) and entry.get("signature") == signature
            ),
            None,
        )
        if prior is not None and prior.get("result") == result_fingerprint:
            return "repeated identical evidence lookup returned no new evidence"
        entries = [
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("signature") != signature
        ]
        entries.append(
            {
                "signature": signature,
                "result": result_fingerprint,
                "updated_at": timestamp(),
            }
        )
        self.storage.set_runtime("evidence_lookup_cache_v1", entries[-50:])
        return None

    @staticmethod
    def _inventory_capacity_context(observation: dict[str, Any] | None) -> dict[str, Any]:
        carry = deep_get(observation or {}, "inventory.carry", {})
        if not isinstance(carry, dict):
            return {"known": False, "note": "broker carry estimate was unavailable"}
        load = carry.get("load") if isinstance(carry.get("load"), dict) else {}
        room_for = carry.get("room_for") if isinstance(carry.get("room_for"), dict) else None
        return {
            "known": carry.get("known") is True,
            "items": carry.get("items"),
            "weight": load.get("weight"),
            "weight_max": carry.get("weight_max"),
            "bulk": load.get("bulk"),
            "bulk_max": carry.get("bulk_max"),
            "exact": load.get("exact"),
            "unweighed": load.get("unweighed", []),
            "room_for": room_for,
            "note": carry.get("note"),
        }

    @classmethod
    def _failure_context(
        cls,
        tool: str,
        reason: str,
        observation: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if tool != "shop" or not is_inventory_capacity_refusal(reason):
            return None
        return {
            "kind": "inventory_capacity_refused",
            "source": "merchant_message",
            "reason": str(reason)[:500],
            "item_transfer_verified": False,
            "inventory_capacity": cls._inventory_capacity_context(observation),
        }

    @staticmethod
    def _failure_invalidates_plan(tool: str, reason: Any) -> bool:
        text = str(reason or "").casefold()
        return (
            tool == "shop" and is_inventory_capacity_refusal(text)
        ) or (
            tool == "sell_all" and "merchant bought zero" in text
        )

    @classmethod
    def _no_progress_guidance(
        cls,
        tool: str,
        reason: str,
        *,
        repeated: bool = False,
        observation: dict[str, Any] | None = None,
    ) -> str:
        prefix = f"The {'repeated ' if repeated else 'previous '}{tool} call made no progress: {reason}. "
        text = reason.casefold()
        if tool == PVP_SEEK_TOOL_NAME:
            if any(
                marker in text
                for marker in (
                    "route",
                    "travel",
                    "exit",
                    "boundary",
                    "no floor",
                    "could not reach",
                    "failed hop",
                )
            ):
                return (
                    prefix
                    + "The patrol did not complete, so this is route failure evidence—not target absence and "
                    "not insufficient combat power. Preserve the reported requested room, actual room, failed "
                    "hop, and broker reason. Do not vary room pairs that share this hop; relocate through a "
                    "verified working exit or wait for the route implementation/corpus to change."
                )
            if any(
                marker in text
                for marker in ("guild", "combat forbidden", "player combat forbidden", "safe_death", "safe death")
            ):
                return (
                    prefix
                    + "The room cannot support this PvP phase in the character's verified state. Exclude rooms marked "
                    "ROOM_GUILD_PK_ONLY unless guild eligibility is positively observed, and exclude ROOM_NO_PK, "
                    "ROOM_NO_COMBAT, and safe-death rooms for loot hunts. Use the controller's eligible route."
                )
            return (
                prefix
                + "The completed patrol did not acquire an eligible local player. Do not camp or repeat the same "
                "route unchanged. Choose at least two different grounded, PvP-eligible public room ids; global who "
                "only proves that a name is online and never proves the player is in the character's room."
            )
        if tool == PVP_TOOL_NAME and "visible" in text:
            return (
                prefix
                + "The named player was not freshly visible locally. If the durable goal is a fresh-local "
                "pvp_engage-only opportunity, end that stale opportunity and resume progression; never broaden "
                "it into who, pvp_seek, camping, or a replacement target. pvp_seek is permitted only for a "
                "specific hunt the operator explicitly requested."
            )
        if tool == "shop" and any(marker in text for marker in ("enough money", "insufficient fund", "cannot afford")):
            return (
                prefix
                + "The merchant refused for insufficient carried funds. Do not retry this purchase at any quantity "
                "until carried shillings increase. Inspect inventory, quote a different ordinary loot item with sell "
                "confirm:false and confirm an accepted offer, or travel to a verified bank and withdraw its confirmed "
                "balance; only then return and buy the remaining needed quantity."
            )
        if tool == "shop" and is_inventory_capacity_refusal(reason):
            capacity = cls._inventory_capacity_context(observation)
            facts = [
                "The merchant reported carried-inventory capacity as the reason the requested item was not transferred."
            ]
            if capacity.get("known"):
                facts.append(
                    "Broker carry evidence at failure: "
                    f"items={capacity.get('items')}, "
                    f"weight={capacity.get('weight')}/{capacity.get('weight_max')}, "
                    f"bulk={capacity.get('bulk')}/{capacity.get('bulk_max')}, "
                    f"exact={capacity.get('exact')}."
                )
                if capacity.get("exact") is not True:
                    facts.append(
                        "The broker marks that load as a lower bound because one or more carried items were unweighed."
                    )
            else:
                facts.append("A broker carry estimate was not available in the failure observation.")
            facts.append("The unchanged purchase step's expected item-transfer observation was disproved.")
            return prefix + " ".join(facts)
        if tool == "sell_all" and "merchant bought zero" in text:
            return (
                prefix
                + "No property transferred and the call did not reduce the carried inventory load."
            )
        if tool in {"sell", "sell_all"} and any(marker in text for marker in ("not interested", "did not buy", "no counteroffer")):
            return (
                prefix
                + "Do not retry the same item with this merchant. Quote a materially different ordinary item or use "
                "merchant evidence to choose a different buyer."
            )
        if tool == "bank" and "no banker" in text:
            return (
                prefix
                + "Do not repeat the same bank mutation. Verify that a banker NPC is visible; if already in a named "
                "bank, use broker route/merchant evidence to choose a working bank endpoint or report the bank-tool "
                "compatibility failure. A bank-room name alone does not prove that currency moved."
            )
        if tool == "bank" and "identical evidence lookup" in text:
            return (
                prefix
                + "The bank balance is already established. Do not ask for it again in the unchanged state; "
                "if carried shillings are zero, launch the goal-owned bounded keeper from this sanctuary."
            )
        if tool == "prey":
            return (
                prefix
                + "The prey ranking is already grounded. Select its eligible creature once, then call "
                "hunting_grounds for that exact creature or launch the already-validated bounded keeper; do not "
                "ask prey to return the same ranking again."
            )
        if tool == "hunting_grounds":
            return (
                prefix
                + "The hunting-room evidence is already grounded. Use the exact non-quarantined numeric room id "
                "to launch the bounded keeper from sanctuary, or choose a materially different room; do not repeat "
                "the same location lookup."
            )
        if tool in MOVEMENT_TOOLS and any(
            marker in text
            for marker in (
                "kept ending up somewhere other than the planned square",
                "every heading refused",
                "no_move",
            )
        ):
            return (
                prefix
                + "The server was refusing movement state, not disproving the destination. Issue rest with "
                "stand:true, then retry with materially distinct route arguments (for example an explicit "
                "max_hops) or use go_through for one verified neighbouring exit."
            )
        return (
            prefix
            + "Do not repeat the same call unchanged. Use returned route, exit, inventory, or location evidence to "
            "choose a materially different tool or target; prefer authoritative numeric ids."
        )

    @staticmethod
    def _no_progress_reason(
        result: Any,
        observation: dict[str, Any],
        *,
        tool: str | None = None,
        arguments: dict[str, Any] | None = None,
        after_observation: dict[str, Any] | None = None,
    ) -> str | None:
        if not isinstance(result, dict):
            return None
        if tool in PVP_TOOL_NAMES:
            outcome = str(result.get("outcome") or "")
            if outcome in {
                "target_not_visible",
                "target_escaped_before_attack",
                "target_offline",
                "target_not_found",
                "search_timeout",
                "route_unavailable",
                "travel_error",
                "no_eligible_search_rooms",
                "guild_required",
                "player_combat_forbidden",
                "combat_forbidden",
                "safe_death_ineligible",
                "attack_refused",
            }:
                return str(result.get("reason") or outcome.replace("_", " "))[:500]
        if tool == "bank" and after_observation is not None:
            # A completed bank RPC is not evidence that money moved.  Banker
            # dialogue can describe a refusal without using the small set of
            # generic refusal words below (for example, "you only have 133"),
            # and the broker may still return a normal result object.  Current
            # inventory is authoritative: a withdrawal must increase carried
            # currency and a deposit must decrease it.
            action = str(result.get("action") or "").casefold()
            before_currency = BotController._carried_currency(observation)
            after_currency = BotController._carried_currency(after_observation)
            wrong_direction = (
                action == "withdraw" and after_currency <= before_currency
            ) or (
                action == "deposit" and after_currency >= before_currency
            )
            if wrong_direction:
                banker_messages = result.get("banker_said")
                if isinstance(banker_messages, list) and banker_messages:
                    return str(banker_messages[0])[:500]
                note = result.get("note")
                if isinstance(note, str) and note.strip():
                    return note[:500]
                return (
                    f"carried shillings did not move in the requested direction after {action}: "
                    f"before {before_currency}, after {after_currency}"
                )[:500]
        if tool in {"map", KNOWLEDGE_TOOL_NAME} and result.get("matches") == []:
            return "authoritative lookup returned no matches"
        if tool == "shop" and isinstance(result.get("bought"), list):
            received = result.get("got")
            if not isinstance(received, list) or not received:
                messages = result.get("messages")
                if isinstance(messages, list) and messages:
                    return str(messages[0])[:500]
                return "purchase request yielded no items"
        if (
            tool == "act"
            and str((arguments or {}).get("verb") or "").casefold() in {"drop", "get"}
            and after_observation is not None
        ):
            before_inventory = deep_get(observation, "inventory.items", [])
            after_inventory = deep_get(after_observation, "inventory.items", [])
            if canonical_json(before_inventory) == canonical_json(after_inventory):
                messages = result.get("messages")
                if isinstance(messages, list) and messages:
                    return str(messages[0])[:500]
                verb = str((arguments or {}).get("verb") or "action")
                return f"carried inventory did not change after {verb}"[:500]
        if tool == "equip_best":
            # A completed broker call is not an equipped weapon.  The harness
            # deliberately returns a truthful result when every carried weapon
            # is broken or refused: wielding=null, verified=false, plus a useful
            # note.  Treating that RPC completion as action success refreshed
            # semantic liveness forever while the deterministic farm preflight
            # retried the same impossible equip step.
            wielding = result.get("wielding")
            has_weapon = (
                isinstance(wielding, str) and bool(wielding.strip())
            ) or (
                isinstance(wielding, (list, tuple, set)) and bool(wielding)
            )
            if not has_weapon:
                note = str(result.get("note") or "").strip()
                if note:
                    return note[:500]
                known_broken = result.get("known_broken")
                suffix = (
                    f"; {known_broken} carried weapon(s) are known broken"
                    if isinstance(known_broken, int) and known_broken > 0
                    else ""
                )
                return ("equip_best produced no wielded weapon" + suffix)[:500]
        if tool in {"sell", "sell_all"} and result.get("sold") is False:
            # A non-confirming sell call that returns a concrete counteroffer is
            # a useful quote, not a failed mutation.  No quote and no sale is
            # genuine non-progress even when the RPC itself completed normally.
            if result.get("offered_price") is None:
                messages = result.get("messages")
                if isinstance(messages, list) and messages:
                    return str(messages[0])[:500]
                return str(result.get("note") or "merchant did not buy the offered item")[:500]
        if tool == "sell_all" and isinstance(result.get("sold"), list):
            sold = result.get("sold")
            refused = result.get("refused")
            if not sold and isinstance(refused, list) and refused:
                refusal_facts = [
                    f"{str(item.get('name') or 'item')}: {str(item.get('why') or 'refused')}"
                    for item in refused[:4]
                    if isinstance(item, dict)
                ]
                detail = "; ".join(refusal_facts) or "merchant returned refusal evidence"
                return (
                    f"merchant bought zero of {len(refused)} offered inventory entries; {detail}"
                )[:500]
        failed_flag = any(result.get(name) is False for name in ("arrived", "left", "ok", "success"))
        if failed_flag:
            before_room = deep_get(observation, "look.room.num", deep_get(observation, "look.room.name"))
            after_room = deep_get(result, "now.room.num", deep_get(result, "now.room.name"))
            if before_room is not None and after_room is not None and str(before_room) != str(after_room):
                return None
            return str(result.get("reason") or "broker result reported that the requested outcome did not occur")[:500]
        banker_messages = result.get("banker_said")
        if isinstance(banker_messages, list):
            refusal = next(
                (
                    str(message)
                    for message in banker_messages
                    if any(marker in str(message).casefold() for marker in ("can't", "cannot", "couldn't", "no such"))
                ),
                None,
            )
            if refusal:
                return refusal[:500]
        note = result.get("note")
        if isinstance(note, str) and any(
            marker in note.casefold()
            for marker in (
                "banker said nothing",
                "no banker in this room",
                "no merchant in this room",
                "there is no merchant",
            )
        ):
            if after_observation is not None:
                before_inventory = deep_get(observation, "inventory.items", [])
                after_inventory = deep_get(after_observation, "inventory.items", [])
                if canonical_json(before_inventory) != canonical_json(after_inventory):
                    return None
                # A completed post-action observation with no inventory change
                # resolves the broker's ambiguous prose: the requested transfer
                # did not happen. Room-name verification authorizes attempting a
                # bank call; it is not evidence of economic success.
                return note[:500]
            # The broker can report its generic "banker said nothing" note even
            # when the server accepted a deposit or withdrawal.  At a verified
            # bank, treating that ambiguous note as a durable failure poisoned
            # retry memory and suppressed later valid transactions.  Location
            # preflight already prevents bank mutations elsewhere. Without a
            # post-action observation the note remains non-authoritative; the
            # next observation must still verify the economic result.
            room_name = str(
                deep_get(observation, "look.room.name", deep_get(observation, "look.room", ""))
                or ""
            ).casefold()
            if tool == "bank" and "bank" in room_name:
                return None
            return note[:500]
        messages = result.get("messages")
        if isinstance(messages, list):
            refusal_markers = (
                "unable to",
                "can't",
                "cannot",
                "couldn't",
                "don't have",
                "do not have",
                "not carrying",
                "that amount",
                "inventory is full",
                "pack is full",
                "too much to carry",
                "can't carry",
                "cannot carry",
                "not enough room",
                "nothing happens",
                "refused",
                "no such",
            )
            refusal = next(
                (
                    str(message)
                    for message in messages
                    if any(marker in str(message).casefold() for marker in refusal_markers)
                ),
                None,
            )
            if refusal:
                return refusal[:500]
        return None

    def _social_loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    self._social_tick()
                    self.dependencies["social"] = "healthy"
                except (BrokerError, ModelError) as exc:
                    self.dependencies["social"] = "degraded"
                    self._degrade("social", exc)
                except Exception as exc:
                    LOG.exception("social turn failed")
                    self.dependencies["social"] = "degraded"
                    self._degrade("social", exc)
                self.stop_event.wait(self.config.controller.social_poll_seconds)
        finally:
            # Storage owns one SQLite connection per thread.
            self.storage.close()

    def _social_tick(self) -> None:
        if self.offline_diagnostics or not self.config.controller.conversation_enabled:
            return
        if self._game_action_active.is_set():
            return
        self._reconcile_conversation_listener()
        if self._game_action_active.is_set():
            return
        look = self.broker.call_tool(
            "look",
            {"agent": self.config.game.agent, "cached": True},
            timeout=10,
        )
        if not isinstance(look, dict):
            look = {}
        self._greeting_turn(look)
        self._conversation_turn(look=look)

    def _reconcile_conversation_listener(self) -> None:
        now = time.time()
        if now - self._last_conversation_reconcile_at < 30:
            return
        status = self.broker.call_tool(
            "converse",
            {"agent": self.config.game.agent, "action": "status"},
            timeout=10,
        )
        policy = status.get("policy", {}) if isinstance(status, dict) else {}
        inbox = status.get("inbox", {}) if isinstance(status, dict) else {}
        desired = (
            status.get("attached") is True
            and status.get("running") is True
            and policy.get("ack") is False
            and policy.get("smallTalk") is False
            and policy.get("faceSpeaker") is False
            and policy.get("escalate") is True
            and inbox.get("reply_budget") == 20
        ) if isinstance(status, dict) else False
        if not desired:
            self._start_conversation_listener()
        self._last_conversation_reconcile_at = now

    @staticmethod
    def _speaker_key(name: Any, object_id: Any = None) -> str:
        cleaned = " ".join(str(name or "").split()).casefold()
        return f"name:{cleaned}" if cleaned else f"object:{object_id}"

    def _history_for(self, key: str) -> list[dict[str, Any]]:
        entries = self._conversation_history.get(key, [])
        if not isinstance(entries, list):
            return []
        return entries[-(self.config.controller.conversation_history_turns * 2) :]

    def _remember_conversation(self, key: str, role: str, content: str, *, speaker_kind: str) -> None:
        clean = " ".join(str(content).split())[:600]
        if not clean:
            return
        entries = self._conversation_history.setdefault(key, [])
        entries.append({"role": role, "content": clean, "speaker_kind": speaker_kind, "at": timestamp()})
        del entries[: -self.config.controller.conversation_history_turns * 2]
        if len(self._conversation_history) > 100:
            oldest = min(
                self._conversation_history,
                key=lambda candidate: str((self._conversation_history[candidate] or [{}])[-1].get("at", "")),
            )
            self._conversation_history.pop(oldest, None)
        self.storage.set_runtime("conversation_history_v1", self._conversation_history)

    @staticmethod
    def _visible_objects(look: dict[str, Any]) -> list[dict[str, Any]]:
        objects = look.get("objects", [])
        return [item for item in objects if isinstance(item, dict)] if isinstance(objects, list) else []

    def _speaker_kind(self, message: dict[str, Any], look: dict[str, Any]) -> str:
        source = message.get("from") if isinstance(message.get("from"), dict) else {}
        object_id = source.get("object_id")
        for item in self._visible_objects(look):
            if object_id is not None and str(item.get("id")) == str(object_id):
                return "player" if item.get("is_player") is True else "npc"
        return "player" if source.get("is_peer") is True else "unknown_in_game_speaker"

    def _public_context_for_look(self, look: dict[str, Any]) -> dict[str, Any]:
        value = self.public_game_context()
        value["room"] = deep_get(look, "room.name", look.get("room", value.get("room")))
        value["visible_players"] = [
            item.get("name")
            for item in self._visible_objects(look)
            if item.get("is_player") is True and item.get("name")
        ][:30]
        return value

    def _socially_unsafe(self, look: dict[str, Any]) -> bool:
        room = str(deep_get(look, "room.name", look.get("room", "")) or "")
        if "underworld" in room.casefold():
            return True
        health = deep_get(look, "vitals.health", {})
        if not isinstance(health, dict):
            return False
        maximum = health.get("max")
        current = health.get("current", health.get("value"))
        try:
            return bool(maximum) and float(current) / float(maximum) < self.config.policy.critical_health_fraction
        except (TypeError, ValueError, ZeroDivisionError):
            return False

    def _save_social_presence(self) -> None:
        self.storage.set_runtime(
            "social_presence_v1",
            {"greeted_at": self._greeted_at, "greeting_times": self._greeting_times[-60:]},
        )

    def _greeting_turn(self, look: dict[str, Any]) -> None:
        if not self.config.controller.proactive_greetings_enabled:
            return
        persona = self.storage.persona()
        if persona.get("version", 0) == 0:
            return

        now = time.time()
        self_name = str(
            deep_get(look, "self.name", self._character_name(self.last_observation or {}) or "")
        ).casefold()
        visible: dict[str, dict[str, Any]] = {}
        for item in self._visible_objects(look):
            if item.get("is_player") is not True or not item.get("name"):
                continue
            if str(item.get("name")).casefold() == self_name:
                continue
            key = self._speaker_key(item.get("name"), item.get("id"))
            visible[key] = {
                "name": str(item.get("name")),
                "object_id": item.get("id"),
                "distance": item.get("distance"),
                "speaker_kind": "player",
            }

        entered = set(visible) - self._visible_players
        self._visible_players = set(visible)
        for key in entered:
            if now - self._greeted_at.get(key, 0) >= self.config.controller.greeting_cooldown_seconds:
                self._pending_greetings[key] = visible[key]
        for key in list(self._pending_greetings):
            if key not in visible:
                self._pending_greetings.pop(key, None)

        self._greeting_times = [stamp for stamp in self._greeting_times if now - stamp < 60]
        if (
            not self._pending_greetings
            or len(self._greeting_times) >= self.config.controller.greetings_per_minute
            or self._socially_unsafe(look)
        ):
            return

        # At most one initiated line per social tick. The minute budget is generous,
        # but this spacing keeps a crowded-room arrival from becoming a burst of spam.
        key = next(iter(self._pending_greetings))
        encounter = self._pending_greetings[key]
        result = self.model.greet(
            persona=persona,
            encounter=encounter,
            context=self._public_context_for_look(look),
            history=self._history_for(key),
        )
        if result["ignore"] or not result["reply"]:
            self._pending_greetings.pop(key, None)
            self._greeted_at[key] = now
            self._save_social_presence()
            return
        if self._game_action_active.is_set():
            # Keep the pending greeting for the next quiet social tick.
            return
        if contains_secret(result["reply"], self.config.secrets):
            self._pending_greetings.pop(key, None)
            self._greeted_at[key] = now
            self._save_social_presence()
            self.storage.emit_event(
                "security.egress_blocked",
                "Blocked an initiated greeting matching private credential material",
                severity="critical",
                interesting=True,
                data={"speaker_kind": "player"},
            )
            return

        delivered = self.broker.call_tool(
            "say",
            {"agent": self.config.game.agent, "type": "say", "text": result["reply"]},
            timeout=15,
            # Speech is ambient, not part of the one-game-action transaction.
            # The broker's per-session pacer safely sequences its packet.
            mutation=False,
        )
        self._pending_greetings.pop(key, None)
        self._greeted_at[key] = now
        self._greeting_times.append(now)
        self._save_social_presence()
        self._remember_conversation(key, "assistant", result["reply"], speaker_kind="player")
        self.storage.emit_event(
            "conversation.greeted",
            f"Greeted visible player {encounter['name']}",
            data={
                "player": encounter["name"],
                "persona_version": persona["version"],
                "delivery": redact(delivered),
            },
        )

    def _conversation_turn(self, *, look: dict[str, Any] | None = None) -> None:
        persona = self.storage.persona()
        if self.offline_diagnostics or not self.config.controller.conversation_enabled or persona.get("version", 0) == 0:
            return
        if self._game_action_active.is_set():
            return
        look = look if isinstance(look, dict) else {}
        inbox = self.broker.call_tool(
            "inbox",
            {"agent": self.config.game.agent, "action": "read", "state": "escalated", "limit": 4},
            timeout=10,
        )
        for raw_message in inbox.get("messages", []) if isinstance(inbox, dict) else []:
            if not isinstance(raw_message, dict):
                continue
            message_id = raw_message.get("id")
            if not message_id:
                continue
            source = raw_message.get("from") if isinstance(raw_message.get("from"), dict) else {}
            speaker_kind = self._speaker_kind(raw_message, look)
            speaker_key = self._speaker_key(source.get("name"), source.get("object_id"))
            message = {**raw_message, "speaker_kind": speaker_kind}
            pending = self._pending_conversation_replies.get(str(message_id))
            if pending is not None and time.time() < float(pending.get("retry_after", 0) or 0):
                continue

            if pending is None:
                incoming = str(message.get("utterance", ""))
                history = self._history_for(speaker_key)
                result = self.model.respond(
                    persona=persona,
                    message=redact(message),
                    context=self._public_context_for_look(look),
                    history=history,
                )
                if contains_secret(result["reply"], self.config.secrets):
                    result = {"reply": "", "ignore": True, "reason": "credential egress filter"}
                    self.storage.emit_event(
                        "security.egress_blocked",
                        "Blocked a conversational reply matching private credential material",
                        severity="critical",
                        interesting=True,
                        data={"inbox_item_id": message_id, "speaker_kind": speaker_kind},
                    )
                self._remember_conversation(speaker_key, "speaker", incoming, speaker_kind=speaker_kind)
                if result["ignore"] or not result["reply"]:
                    self.broker.call_tool(
                        "inbox",
                        {
                            "agent": self.config.game.agent,
                            "action": "resolve",
                            "id": message_id,
                            "state": "refused",
                            "note": result["reason"][:200],
                        },
                        timeout=10,
                        mutation=False,
                    )
                    self.storage.emit_event(
                        "conversation.responded",
                        "Considered dialogue from an in-game speaker",
                        data={
                            "inbox_item_id": message_id,
                            "persona_version": persona["version"],
                            "speaker_kind": speaker_kind,
                            "ignored": True,
                        },
                    )
                    continue
                pending = {
                    "reply": result["reply"],
                    "speaker_key": speaker_key,
                    "speaker_kind": speaker_kind,
                    "persona_version": persona["version"],
                    "attempts": 0,
                    "retry_after": 0.0,
                }

            if self._game_action_active.is_set():
                self._pending_conversation_replies[str(message_id)] = pending
                continue
            delivery = self.broker.call_tool(
                "inbox",
                {
                    "agent": self.config.game.agent,
                    "action": "reply",
                    "id": message_id,
                    "text": pending["reply"],
                },
                timeout=15,
                mutation=False,
            )
            if isinstance(delivery, dict) and delivery.get("retry"):
                attempts = int(pending.get("attempts", 0) or 0) + 1
                pending["attempts"] = attempts
                pending["retry_after"] = time.time() + min(60.0, 2.0 ** attempts * 2.0)
                if attempts >= 3:
                    self.broker.call_tool(
                        "inbox",
                        {
                            "agent": self.config.game.agent,
                            "action": "resolve",
                            "id": message_id,
                            "state": "refused",
                            "note": "dialogue delivery exhausted after bounded retries",
                        },
                        timeout=10,
                        mutation=False,
                    )
                    self._pending_conversation_replies.pop(str(message_id), None)
                    self.storage.emit_event(
                        "conversation.delivery_abandoned",
                        "Stopped retrying an undeliverable dialogue reply",
                        severity="notice",
                        interesting=False,
                        data={"inbox_item_id": message_id, "attempts": attempts, "speaker_kind": pending["speaker_kind"]},
                    )
                    continue
                self._pending_conversation_replies[str(message_id)] = pending
                continue
            self._pending_conversation_replies.pop(str(message_id), None)
            replied = not isinstance(delivery, dict) or delivery.get("replied") is not False
            if replied:
                self._remember_conversation(
                    str(pending["speaker_key"]),
                    "assistant",
                    str(pending["reply"]),
                    speaker_kind=str(pending["speaker_kind"]),
                )
            else:
                self.broker.call_tool(
                    "inbox",
                    {
                        "agent": self.config.game.agent,
                        "action": "resolve",
                        "id": message_id,
                        "state": "refused",
                        "note": str(delivery.get("why", "reply could not be delivered"))[:200],
                    },
                    timeout=10,
                    mutation=False,
                )
            self.storage.emit_event(
                "conversation.responded",
                "Responded to dialogue from an in-game speaker" if replied else "Could not deliver dialogue reply",
                data={
                    "inbox_item_id": message_id,
                    "persona_version": pending["persona_version"],
                    "speaker_kind": pending["speaker_kind"],
                    "ignored": False,
                    "delivered": replied,
                },
            )

    def _degrade(self, dependency: str, exc: Exception) -> None:
        self.state = "degraded"
        warning = f"{dependency}: {str(exc)[:300]}"
        self.warnings = [warning]
        if self._active_degradations.get(dependency) == warning:
            return
        self._active_degradations[dependency] = warning
        goal = self.storage.active_goal()
        self.storage.emit_event(
            f"dependency.{dependency}.unhealthy",
            warning,
            severity="warning",
            interesting=True,
            goal_id=goal and goal["id"],
        )

    def _planner_feedback(self, goal: dict[str, Any]) -> dict[str, Any] | None:
        value = self.storage.get_runtime("planner_feedback")
        if not isinstance(value, dict) or value.get("goal_id") != goal.get("id"):
            return None
        blocked_action = value.get("blocked_action")
        if isinstance(blocked_action, dict) and self.last_observation:
            tool = str(blocked_action.get("tool") or "")
            arguments = blocked_action.get("arguments")
            if tool and isinstance(arguments, dict):
                blocked = self._blocked_action(goal, self.last_observation, tool, arguments)
                if blocked and blocked.get("reason"):
                    # Upgrade persisted feedback created by older controller
                    # versions.  Otherwise a restart can preserve the generic
                    # "try different arguments" wording that encourages models
                    # to cycle unaffordable quantities instead of satisfying the
                    # missing-funds prerequisite.
                    blocked_reason = str(blocked["reason"])
                    guidance = self._no_progress_guidance(
                        tool,
                        blocked_reason,
                        repeated=True,
                        observation=self.last_observation,
                    )
                    failure_context = self._failure_context(
                        tool,
                        blocked_reason,
                        self.last_observation,
                    )
                    if value.get("message") != guidance or value.get("failure_context") != failure_context:
                        value = {
                            **value,
                            "message": guidance,
                            "failure_context": redact(failure_context) if failure_context else None,
                            "updated_at": timestamp(),
                        }
                        self.storage.set_runtime("planner_feedback", value)
        return value

    def _set_planner_feedback(
        self,
        goal: dict[str, Any],
        message: str,
        *,
        consecutive_waits: int = 0,
        consecutive_plan_rejections: int = 0,
        blocked_action: dict[str, Any] | None = None,
        safety_suppression: dict[str, Any] | None = None,
        failure_context: dict[str, Any] | None = None,
    ) -> None:
        self.storage.set_runtime(
            "planner_feedback",
            {
                "goal_id": goal["id"],
                "message": message[:1000],
                "consecutive_waits": consecutive_waits,
                "consecutive_plan_rejections": consecutive_plan_rejections,
                "blocked_action": blocked_action,
                "safety_suppression": safety_suppression,
                "failure_context": redact(failure_context) if failure_context else None,
                "updated_at": timestamp(),
            },
        )

    def _clear_planner_feedback(self) -> None:
        self.storage.set_runtime("planner_feedback", None)

    def _record_safety_suppression(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        tool: str,
        arguments: dict[str, Any],
        blockers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        room = self._observation_room(observation)
        blocker_kinds = sorted(
            str(item.get("kind") or "unknown")
            for item in blockers
            if isinstance(item, dict)
        )
        signature = canonical_json(
            {
                "goal_id": goal.get("id"),
                "tool": tool,
                "arguments": redact(arguments),
                "room": room,
                "blocker_kinds": blocker_kinds,
            }
        )
        now_unix = time.time()
        now_text = timestamp()
        prior = self.storage.get_runtime("safety_suppression_v1", {})
        same = (
            isinstance(prior, dict)
            and prior.get("goal_id") == goal.get("id")
            and prior.get("signature") == signature
        )
        first_unix = (
            float(prior.get("first_blocked_unix", now_unix))
            if same
            else now_unix
        )
        value = {
            "goal_id": goal["id"],
            "signature": signature,
            "tool": tool,
            "room": room,
            "blocker_kinds": blocker_kinds,
            "same_blocker_count": int(prior.get("same_blocker_count", 0) or 0) + 1
            if same
            else 1,
            "first_blocked_at": prior.get("first_blocked_at", now_text)
            if same
            else now_text,
            "first_blocked_unix": first_unix,
            "last_blocked_at": now_text,
            "blocked_for_seconds": round(max(0.0, now_unix - first_unix), 1),
        }
        self.storage.set_runtime("safety_suppression_v1", value)
        return {
            key: value[key]
            for key in (
                "same_blocker_count",
                "first_blocked_at",
                "last_blocked_at",
                "blocked_for_seconds",
                "blocker_kinds",
            )
        }

    def _clear_safety_suppression(self, goal_id: str) -> None:
        value = self.storage.get_runtime("safety_suppression_v1")
        if isinstance(value, dict) and value.get("goal_id") == goal_id:
            self.storage.set_runtime("safety_suppression_v1", None)

    @staticmethod
    def _observation_room(observation: dict[str, Any]) -> Any:
        return deep_get(observation, "look.room.num", deep_get(observation, "look.room.name"))

    @staticmethod
    def _carried_currency(observation: dict[str, Any]) -> int:
        items = deep_get(observation, "inventory.items", [])
        total = 0
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or "shilling" not in str(item.get("name") or "").casefold():
                continue
            amount = item.get("amount", item.get("quantity", item.get("count", 1)))
            total += int(amount) if isinstance(amount, (int, float)) and amount > 0 else 1
        return total

    def _financial_context(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Summarize carried wealth for planning without turning it into a gate.

        Inventory valuation is deliberately best effort.  The knowledge corpus
        contains source-derived base values for many items, while the live
        server's sale price can differ and some items have no known value.
        """

        raw_items = deep_get(observation, "inventory.items", [])
        items = [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
        signature = canonical_json(items)
        if (
            signature == self._financial_context_signature
            and self._financial_context_value is not None
        ):
            return self._financial_context_value

        carried_shillings = self._carried_currency(observation)
        known_inventory_value = 0.0
        valued_items: list[dict[str, Any]] = []
        unknown_value_items: list[dict[str, Any]] = []
        for item in items:
            name = " ".join(str(item.get("name") or "").split())
            if not name or "shilling" in name.casefold():
                continue
            raw_quantity = item.get("amount", item.get("quantity", item.get("count", 1)))
            quantity = (
                int(raw_quantity)
                if isinstance(raw_quantity, (int, float))
                and not isinstance(raw_quantity, bool)
                and raw_quantity > 0
                else 1
            )
            valuation = self.knowledge.item_valuation(name)
            unit_value = valuation.get("unit_value")
            basis = valuation.get("basis")
            source_ref = valuation.get("source_ref")
            if not isinstance(unit_value, (int, float)) or isinstance(unit_value, bool):
                live_value = item.get("value")
                if isinstance(live_value, (int, float)) and not isinstance(live_value, bool):
                    unit_value = live_value
                    basis = "live inventory value reported by the broker"
                    source_ref = None
            if isinstance(unit_value, (int, float)) and not isinstance(unit_value, bool):
                subtotal = float(unit_value) * quantity
                known_inventory_value += subtotal
                valued_items.append(
                    {
                        "name": valuation.get("canonical_name") or name,
                        "quantity": quantity,
                        "unit_value": unit_value,
                        "subtotal": int(subtotal) if subtotal.is_integer() else subtotal,
                        "basis": basis,
                        "source_ref": source_ref,
                    }
                )
            else:
                unknown_value_items.append(
                    {
                        "name": name,
                        "quantity": quantity,
                        "reason": valuation.get("status", "value_unknown"),
                    }
                )

        inventory_total: int | float = (
            int(known_inventory_value)
            if known_inventory_value.is_integer()
            else known_inventory_value
        )
        known_total: int | float = carried_shillings + known_inventory_value
        if isinstance(known_total, float) and known_total.is_integer():
            known_total = int(known_total)
        value = {
            "carried_shillings": carried_shillings,
            "known_inventory_item_value": inventory_total,
            "known_total_carried_value": known_total,
            "valuation_complete": not unknown_value_items,
            "valued_items": valued_items[:30],
            "unknown_value_items": unknown_value_items[:30],
            "buyer_candidates": (
                self.knowledge.buyer_candidates(items)
                if hasattr(self.knowledge, "buyer_candidates")
                else []
            ),
            "valuation_note": (
                "Best-effort base/live values, not a guaranteed merchant resale quote. "
                "Unknown items mean the true total may be higher."
            ),
            "banking_policy": {
                "mode": "planner_discretion",
                "never_blocks_travel_or_combat": True,
                "canonical_tos_bank_room_id": TOS_BANK_ROOM_ID,
            },
        }
        self._financial_context_signature = signature
        self._financial_context_value = value
        return value

    def _blocked_action(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        signature = canonical_json({"tool": tool, "arguments": arguments})
        room = self._observation_room(observation)
        entries = self.storage.get_runtime("blocked_actions", [])
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if (
                isinstance(entry, dict)
                and entry.get("goal_id") == goal.get("id")
                and entry.get("signature") == signature
                and str(entry.get("room")) == str(room)
            ):
                # Historical broker ambiguity could record a successful bank
                # mutation as "no banker" even inside a verified bank.  Do not
                # let that stale fingerprint suppress a valid future withdrawal
                # or deposit forever; the bank-location preflight is the source
                # of truth for whether the action is in the right place.
                room_name = str(
                    deep_get(observation, "look.room.name", deep_get(observation, "look.room", ""))
                    or ""
                ).casefold()
                if (
                    tool == "bank"
                    and "bank" in room_name
                    and "no banker" in str(entry.get("reason", "")).casefold()
                ):
                    continue
                if (
                    tool == "shop"
                    and any(
                        marker in str(entry.get("reason", "")).casefold()
                        for marker in ("enough money", "insufficient fund", "cannot afford")
                    )
                    and self._carried_currency(observation) > int(entry.get("carried_currency", 0) or 0)
                ):
                    continue
                if (
                    tool == "shop"
                    and is_inventory_capacity_refusal(entry.get("reason"))
                    and self.learning.profile(observation).get("inventory_load_hash")
                    != entry.get("inventory_load_hash")
                ):
                    # Capacity refusals are state-specific. A changed load makes
                    # the old result historical evidence rather than a reason
                    # to suppress a newly grounded attempt.
                    continue
                if tool in {"equip_best", "wear_best"}:
                    current_equipment_hash = self._equipment_attempt_hash(tool, observation)
                    recorded_equipment_hash = entry.get("equipment_attempt_hash")
                    if recorded_equipment_hash is not None:
                        equipment_changed = current_equipment_hash != recorded_equipment_hash
                    else:
                        # Migration path for fingerprints written before equipment
                        # attempts had their own state key.  The legacy inventory
                        # hash is broader than ideal, but it safely releases an old
                        # refusal once after any observed inventory change.  New
                        # records below use only relevant equipment candidates.
                        equipment_changed = (
                            self.learning.profile(observation).get("inventory_load_hash")
                            != entry.get("inventory_load_hash")
                        )
                    if equipment_changed:
                        self._discard_blocked_action(entry)
                        continue
                return entry
        return None

    def _equipment_attempt_hash(
        self,
        tool: str,
        observation: dict[str, Any],
    ) -> str | None:
        """Fingerprint only the state that can change an equipment attempt.

        Currency and unrelated loot must not make the controller hammer the
        same known-bad item again.  A replacement candidate, a changed item
        condition/capability, or a changed equipped item should permit a fresh
        broker attempt immediately.
        """

        words = WEAPON_WORDS if tool == "equip_best" else ARMOR_WORDS if tool == "wear_best" else None
        if words is None:
            return None
        raw_items = deep_get(observation, "inventory.items", [])
        candidates: list[dict[str, Any]] = []
        for item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not any(word in name.casefold() for word in words):
                continue
            candidates.append(
                {
                    "id": item.get("id"),
                    "name": name.casefold(),
                    "amount": item.get("amount", item.get("quantity", item.get("count", 1))),
                    "can": sorted(
                        str(value).casefold()
                        for value in item.get("can", [])
                    )
                    if isinstance(item.get("can"), list)
                    else item.get("can"),
                    "condition": item.get("condition"),
                    "durability": item.get("durability"),
                    "broken": item.get("broken"),
                }
            )
        profile = self.learning.profile(observation)
        return json_hash(
            {
                "tool": tool,
                "candidates": sorted(candidates, key=canonical_json),
                "equipment_state": profile.get("equipment_state"),
                "equipment": profile.get("equipment"),
                "wielded_weapons": profile.get("wielded_weapons"),
            }
        )

    def _discard_blocked_action(self, discarded: dict[str, Any]) -> None:
        entries = self.storage.get_runtime("blocked_actions", [])
        if not isinstance(entries, list):
            return
        self.storage.set_runtime(
            "blocked_actions",
            [entry for entry in entries if entry != discarded],
        )

    def _save_blocked_action(self, updated: dict[str, Any]) -> None:
        entries = self.storage.get_runtime("blocked_actions", [])
        values = [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
        values = [
            entry
            for entry in values
            if not (
                entry.get("goal_id") == updated.get("goal_id")
                and entry.get("signature") == updated.get("signature")
                and str(entry.get("room")) == str(updated.get("room"))
            )
        ]
        values.append(updated)
        self.storage.set_runtime("blocked_actions", values[-20:])

    def _record_blocked_action(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        tool: str,
        arguments: dict[str, Any],
        reason: str,
    ) -> None:
        self._save_blocked_action(
            {
                "goal_id": goal["id"],
                "signature": canonical_json({"tool": tool, "arguments": arguments}),
                "tool": tool,
                "arguments": redact(arguments),
                "room": self._observation_room(observation),
                "carried_currency": self._carried_currency(observation),
                "inventory_load_hash": self.learning.profile(observation).get(
                    "inventory_load_hash"
                ),
                "equipment_attempt_hash": self._equipment_attempt_hash(tool, observation),
                "reason": reason[:500],
                "suppressed_count": 0,
                "updated_at": timestamp(),
            }
        )

    def public_game_context(self) -> dict[str, Any]:
        observation = self.last_observation or {}
        return {
            "character": self._character_name(observation),
            "room": deep_get(observation, "look.room.name", deep_get(observation, "look.room")),
            "activity": (self.storage.active_goal() or {}).get("title"),
            "combat_readiness": self.learning.readiness_summary(observation),
        }

    @staticmethod
    def _age_seconds(value: Any) -> float | None:
        if not value:
            return None
        try:
            if isinstance(value, (int, float)):
                return round(max(0.0, time.time() - float(value)), 1)
            text = str(value).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return round(
                max(0.0, time.time() - parsed.astimezone(timezone.utc).timestamp()),
                1,
            )
        except (TypeError, ValueError, OverflowError):
            return None

    def _compact_liveness(
        self,
        goal: dict[str, Any] | None,
        *,
        broker_activity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        suppression = self.storage.get_runtime("safety_suppression_v1")
        if not isinstance(suppression, dict) or (
            goal is not None and suppression.get("goal_id") != goal.get("id")
        ):
            suppression = None
        goal_id = goal.get("id") if isinstance(goal, dict) else None
        goal_is_active = bool(goal and goal.get("status") == "active")
        successful = (
            self.storage.goal_events(goal_id, kinds=["action.succeeded"], limit=1)
            if goal_id and goal_is_active
            else self.storage.latest_events(limit=1, kinds=["action.succeeded"])
        )
        progress_kinds = [
            "progress.hp_gained",
            "progress.skill_learned",
            "progress.spell_learned",
            "progress.skill_milestone",
            "progress.spell_milestone",
            "goal.succeeded",
            "pvp.phase.completed",
        ]
        progress = (
            self.storage.goal_events(goal_id, kinds=progress_kinds, limit=1)
            if goal_id and goal_is_active
            else self.storage.latest_events(limit=1, kinds=progress_kinds)
        )
        if goal_id and goal_is_active and self._direct_pvp_contract(goal) is None:
            goal_text = " ".join(
                (
                    str(goal.get("title") or ""),
                    str(goal.get("objective") or ""),
                    str(deep_get(goal, "constraints.operator_notes", "")),
                )
            ).casefold()
            explicit_hunt = (
                "pvp_seek" in goal_text
                or "pvp hunt" in goal_text
                or "player hunt" in goal_text
                or "pvp patrol" in goal_text
            ) and "do not use pvp_seek" not in goal_text
            if explicit_hunt:
                completed_searches = self.storage.goal_events(
                    goal_id, kinds=["pvp.search.completed"], limit=20
                )
                verified_searches = [
                    event
                    for event in completed_searches
                    if event.get("data", {}).get("completed_patrol") is True
                    and len(event.get("data", {}).get("distinct_rooms", [])) >= 2
                ]
                if verified_searches:
                    progress = [*progress, verified_searches[-1]]
                    progress.sort(key=lambda event: str(event.get("occurred_at") or ""))
                    progress = progress[-1:]
        last_action_at = successful[-1]["occurred_at"] if successful else None
        last_action = None
        if successful:
            event = successful[-1]
            event_data = event.get("data") if isinstance(event.get("data"), dict) else {}
            last_action = {
                "at": event.get("occurred_at"),
                "tool": event_data.get("tool"),
                "summary": event.get("summary"),
            }
        last_progress_at = progress[-1]["occurred_at"] if progress else None
        activity_text = str((broker_activity or {}).get("activity") or "")
        blocker_count = int((suppression or {}).get("same_blocker_count", 0) or 0)
        state = "idle" if goal is None else "active"
        if goal and goal.get("status") in {"paused", "blocked"}:
            state = str(goal["status"])
        if blocker_count >= 3 or any(
            marker in activity_text.casefold() for marker in ("stalled", "error")
        ):
            state = "stalled"
        compact_suppression = None
        if suppression:
            compact_suppression = {
                key: suppression.get(key)
                for key in (
                    "tool",
                    "room",
                    "blocker_kinds",
                    "same_blocker_count",
                    "first_blocked_at",
                    "last_blocked_at",
                    "blocked_for_seconds",
                )
            }
        return {
            "state": state,
            "goal_age_seconds": self._age_seconds(goal.get("created_at")) if goal else None,
            "last_successful_action": last_action,
            "last_successful_action_at": last_action_at,
            "seconds_since_successful_action": self._age_seconds(last_action_at),
            "last_verified_progress_at": last_progress_at,
            "seconds_since_verified_progress": self._age_seconds(last_progress_at),
            "broker_keeper": broker_activity,
            "safety_suppression": compact_suppression,
        }

    @staticmethod
    def _compact_lesson(lesson: dict[str, Any]) -> dict[str, Any]:
        evaluation = (
            lesson.get("retry_evaluation")
            if isinstance(lesson.get("retry_evaluation"), dict)
            else {}
        )
        unmet = [
            str(item.get("detail") or item.get("condition") or "")[:240]
            for item in evaluation.get("conditions", [])
            if isinstance(item, dict) and item.get("met") is not True
        ]
        if evaluation.get("met") is True:
            unmet = []
        return {
            "id": lesson.get("id"),
            "goal_id": lesson.get("goal_id"),
            "status": lesson.get("status"),
            "scope": lesson.get("scope"),
            "classification": lesson.get("classification"),
            "summary": str(lesson.get("summary") or "")[:300],
            "retry_ready": evaluation.get("met"),
            "retry_mode": evaluation.get("mode"),
            "unmet_retry_conditions": unmet[:4],
        }

    def _compact_readiness(self, observation: dict[str, Any]) -> dict[str, Any]:
        readiness = self.learning.readiness_summary(observation)
        quarantines = []
        for item in readiness.get("farm_tactic_quarantines", []):
            if not isinstance(item, dict):
                continue
            quarantines.append(
                {
                    "room": item.get("room", item.get("assigned_room")),
                    "target": item.get("target", item.get("hunt")),
                    "scope": item.get("quarantine_scope"),
                    "reasons": [str(reason)[:180] for reason in item.get("reasons", [])[:3]],
                }
            )
        scorecard = []
        for item in readiness.get("farm_room_scorecard", [])[:3]:
            if not isinstance(item, dict):
                continue
            scorecard.append(
                {
                    key: item.get(key)
                    for key in (
                        "room",
                        "target",
                        "strategy",
                        "target_kills",
                        "deaths",
                        "withdrawals",
                        "quarantined",
                        "last_observed_at",
                    )
                }
            )
        return {
            key: readiness.get(key)
            for key in (
                "max_health",
                "pvp_eligible_by_guide",
                "equipment_state",
                "equipped",
                "wielded_weapons",
                "carried_weapons",
                "carried_armor",
                "healing_supply_count",
                "recent_combat_deaths",
                "recommended_goal_type",
            )
        } | {"farm_tactic_quarantines": quarantines, "farm_room_scorecard": scorecard}

    @staticmethod
    def _compact_character_development(observation: dict[str, Any]) -> dict[str, Any]:
        """Expose live build evidence without leaking the raw broker surface."""
        abilities = observation.get("abilities")
        abilities = abilities if isinstance(abilities, dict) else {}

        def ability_rows(group: str) -> tuple[list[dict[str, Any]], int]:
            raw = abilities.get(group)
            raw = raw if isinstance(raw, list) else []
            singular = group[:-1]
            rows: list[dict[str, Any]] = []
            for item in raw[:24]:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                name = " ".join(str(item["name"]).split())
                rows.append(
                    {
                        key: item.get(key)
                        for key in ("name", "ability", "school", "level", "mana", "targets")
                        if item.get(key) is not None
                    }
                    | {"goal_metric": f"ability.{singular}.{name}"}
                )
            return rows, max(0, len(raw) - len(rows))

        skills, skills_omitted = ability_rows("skills")
        spells, spells_omitted = ability_rows("spells")
        freshness = abilities.get("freshness")
        freshness = freshness if isinstance(freshness, dict) else {"known": False}
        advancement = abilities.get("advancement")
        advancement = advancement if isinstance(advancement, dict) else {}
        recent = advancement.get("recent")
        recent = recent if isinstance(recent, list) else []
        atrophied = advancement.get("atrophied")
        atrophied = atrophied if isinstance(atrophied, list) else []

        spell_catalog = observation.get("spells")
        spell_catalog = spell_catalog if isinstance(spell_catalog, dict) else {}
        readiness_rows = spell_catalog.get("spells")
        readiness_rows = readiness_rows if isinstance(readiness_rows, list) else []
        spell_readiness = {
            key: spell_catalog.get(key)
            for key in (
                "your_karma",
                "your_mana",
                "known_spells",
                "identified",
                "castable_now",
            )
            if key in spell_catalog
        } | {
            "spells": [
                {
                    key: item.get(key)
                    for key in (
                        "name",
                        "school",
                        "level",
                        "mana",
                        "targets",
                        "reagents",
                        "castable",
                        "blocked_by",
                    )
                    if item.get(key) is not None
                }
                for item in readiness_rows[:24]
                if isinstance(item, dict)
            ],
            "omitted": max(0, len(readiness_rows) - 24),
        }
        return {
            "ability_scale": "0-100",
            "freshness": {
                key: freshness.get(key)
                for key in ("from", "age_ms", "known", "note")
                if key in freshness
            },
            "known_skill_count": len(abilities.get("skills", []))
            if isinstance(abilities.get("skills"), list)
            else 0,
            "known_spell_count": len(abilities.get("spells", []))
            if isinstance(abilities.get("spells"), list)
            else 0,
            "skills": skills,
            "skills_omitted": skills_omitted,
            "spells": spells,
            "spells_omitted": spells_omitted,
            "advancement": {
                "since_first_seen": advancement.get("since_first_seen"),
                "changes_on_record": advancement.get("changes_on_record", 0),
                "recent": recent[-6:],
                "atrophied": atrophied[:12],
                **({"note": advancement.get("note")} if advancement.get("note") else {}),
            },
            "spell_readiness": spell_readiness,
        }

    def _local_visible_players(self, observation: dict[str, Any]) -> list[dict[str, Any]]:
        self_name = str(self._character_name(observation) or "").strip().casefold()
        objects = deep_get(observation, "look.objects", [])
        players: list[dict[str, Any]] = []
        for item in objects if isinstance(objects, list) else []:
            if not isinstance(item, dict) or item.get("is_player") is not True:
                continue
            name = str(item.get("name") or "").strip()
            if not name or name.casefold() == self_name:
                continue
            players.append(
                {
                    "id": item.get("id"),
                    "name": name,
                    "distance": item.get("distance"),
                    "relation": item.get("relation"),
                    "safety_on": item.get("safety_on"),
                    "attacking_character": any(
                        item.get(field) is True
                        for field in ("attacking_self", "targeting_self", "damaging_self")
                    ),
                }
            )
        return players[:20]

    @staticmethod
    def _direct_pvp_contract(goal: dict[str, Any] | None) -> dict[str, Any] | None:
        """Recover the closed target contract for a fresh local PvP opportunity.

        The supervisor deliberately records this boundary in operator notes because an
        opportunistic encounter expires when that exact player leaves.  Keep a
        small parser here so the tactical model cannot broaden ``pvp_engage
        only`` into a patrol or substitute another online player.
        """

        if not isinstance(goal, dict):
            return None
        constraints = goal.get("constraints") if isinstance(goal.get("constraints"), dict) else {}
        notes = str(constraints.get("operator_notes") or "").strip()
        objective = str(goal.get("objective") or "").strip()
        note_text = notes.casefold()
        direct_only = "pvp_engage" in note_text and (
            "pvp_engage only" in note_text
            or "only against" in note_text
            or "do not use pvp_seek" in note_text
            or "do not use who" in note_text
        )
        if not direct_only:
            return None

        target: str | None = None
        patterns = (
            r"\bpvp_engage\s+only\s+against\s+([^;,.]+)",
            r"\bpvp_engage\s+(.+?)\s+only(?:\s*[;,.]|$)",
            r"\bengage\s+(.+?)\s+with\s+pvp_engage\b",
        )
        for source, pattern in ((notes, patterns[0]), (notes, patterns[1]), (objective, patterns[2])):
            match = re.search(pattern, source, re.IGNORECASE)
            if match:
                target = match.group(1).strip().strip("\"'")
                break
        if not target:
            return None
        cancel_if_absent = any(
            marker in note_text
            for marker in (
                "freshly locally visible",
                "fresh local observation",
                "present in the current",
                "if " + target.casefold() + " disappears",
                "if the peer disappears",
                "do not substitute another target",
            )
        )
        return {
            "target": target,
            "direct_only": True,
            "cancel_if_absent": cancel_if_absent,
        }

    @staticmethod
    def _pvp_phase_criterion_met(
        goal: dict[str, Any], completion: dict[str, Any]
    ) -> bool:
        phase_ids = {
            str(item.get("id") or f"criterion_{index + 1}")
            for index, item in enumerate(goal.get("success_criteria", []))
            if isinstance(item, dict)
            and item.get("kind") == "event_occurred"
            and item.get("event_kind") == "pvp.phase.completed"
        }
        return any(
            isinstance(item, dict)
            and str(item.get("id")) in phase_ids
            and item.get("met") is True
            for item in completion.get("criteria", [])
        )

    @staticmethod
    def _visible_player_matches(
        observation: dict[str, Any], target: str
    ) -> bool:
        requested = str(target).strip().casefold()
        if not requested:
            return False
        objects = deep_get(observation, "look.objects", [])
        for item in objects if isinstance(objects, list) else []:
            if not isinstance(item, dict) or item.get("is_player") is not True:
                continue
            if requested in {
                str(item.get("id") or "").strip().casefold(),
                str(item.get("name") or "").strip().casefold(),
            }:
                return True
        return False

    def _validate_direct_pvp_plan(
        self,
        goal: dict[str, Any],
        steps: list[dict[str, Any]],
        completion: dict[str, Any],
    ) -> None:
        contract = self._direct_pvp_contract(goal)
        if contract is None or self._pvp_phase_criterion_met(goal, completion):
            return
        forbidden = [
            str(step.get("tool"))
            for step in steps
            if step.get("tool") in {PVP_SEEK_TOOL_NAME, "who"}
        ]
        if forbidden:
            raise ModelError(
                "this is an expiring fresh-local PvP opportunity: the goal requires "
                f"pvp_engage only against {contract['target']}; pvp_seek, who, patrols, camping, "
                "and replacement targets are forbidden"
            )
        direct_steps = [step for step in steps if step.get("tool") == PVP_TOOL_NAME]
        if not direct_steps:
            raise ModelError(
                f"the unfinished direct PvP phase requires one pvp_engage step against {contract['target']}"
            )
        target_text = normalize(str(contract["target"]))
        if any(target_text not in normalize(canonical_json(step)) for step in direct_steps):
            raise ModelError(
                f"every pvp_engage step must name the goal-owned exact target {contract['target']}"
            )

    def _expire_direct_pvp_opportunity(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        completion: dict[str, Any],
    ) -> dict[str, Any] | None:
        contract = self._direct_pvp_contract(goal)
        if (
            contract is None
            or contract.get("cancel_if_absent") is not True
            or self._pvp_phase_criterion_met(goal, completion)
            or self._visible_player_matches(observation, str(contract["target"]))
        ):
            return None
        result = self.manage_goal(
            {
                "request_id": f"controller-pvp-opportunity-ended-{goal['id']}-{uuid7()}",
                "goal_id": goal["id"],
                "expected_version": goal.get("version"),
                "action": "cancel",
                "cause": "opportunity_ended",
                "reason": (
                    f"Fresh local PvP opportunity ended before a server-accepted swing: "
                    f"{contract['target']} is no longer locally visible"
                ),
            }
        )
        self.storage.emit_event(
            "pvp.opportunity.ended",
            f"Ended stale direct PvP opportunity for {contract['target']}",
            severity="notice",
            interesting=True,
            goal_id=goal["id"],
            data={
                "target": contract["target"],
                "local_visibility": False,
                "accepted_phase_complete": False,
                "resumed_progression_eligible": True,
            },
        )
        return {
            "opportunity_ended": True,
            "target": contract["target"],
            "goal": result["goal"],
            "cancellation_assessment": result.get("cancellation_assessment"),
        }

    def _pvp_today_summary(self) -> dict[str, Any]:
        now_day = self.notifications.journal.local_datetime(timestamp()).date().isoformat()
        phases = self.storage.latest_events(
            limit=200, kinds=["pvp.phase.completed"]
        )
        loot_events = self.storage.latest_events(
            limit=200, kinds=["property.transaction"]
        )
        searches = self.storage.latest_events(
            limit=200, kinds=["pvp.search.completed"]
        )
        qualifying = []
        seen_targets: set[str] = set()
        for event in phases:
            if self.notifications.journal.local_day(event) != now_day:
                continue
            target = event.get("data", {}).get("target", {})
            target_name = target.get("name") if isinstance(target, dict) else target
            target_key = str(target.get("id") if isinstance(target, dict) and target.get("id") is not None else target_name).casefold()
            if target_key and target_key in seen_targets:
                continue
            if target_key:
                seen_targets.add(target_key)
            qualifying.append(
                {
                    "at": event.get("occurred_at"),
                    "target": target_name,
                    "accepted_swings": event.get("data", {}).get("accepted_swings"),
                    "items_taken_count": event.get("data", {}).get("items_taken_count"),
                }
            )
        loot_count = sum(
            1
            for event in loot_events
            if self.notifications.journal.local_day(event) == now_day
            and event.get("data", {}).get("transaction") == "pvp_loot"
        )
        today_searches = [
            event
            for event in searches
            if self.notifications.journal.local_day(event) == now_day
            and event.get("data", {}).get("completed_patrol") is True
        ]
        last_search = None
        if today_searches:
            event = today_searches[-1]
            data = event.get("data", {}) if isinstance(event.get("data"), dict) else {}
            last_search = {
                "at": event.get("occurred_at"),
                "outcome": data.get("outcome"),
                "target_requested": data.get("target_requested"),
                "requested_route": data.get("requested_route", []),
                "eligible_route": data.get("route", []),
                "skipped_rooms": data.get("skipped_rooms", []),
                "guild_eligibility_verified": data.get("guild_eligibility_verified"),
                "distinct_rooms": data.get("distinct_rooms", []),
                "target_acquired": data.get("target_acquired"),
            }
        observation = self.last_observation or {}
        observed_at = observation.get("observed_at")
        observation_age = self._age_seconds(observed_at)
        visible_players = self._local_visible_players(observation)
        opportunity_fresh = bool(
            visible_players
            and isinstance(observation_age, (int, float))
            and observation_age <= 30
        )
        return {
            "local_day": now_day,
            "qualifying_victories": len(qualifying),
            "pvp_loot_transactions": loot_count,
            "search_patrols": len(today_searches),
            "last_search": last_search,
            "policy": "operator_goal_driven",
            "daily_limit": None,
            "initiation_available": True,
            "opportunity": {
                "fresh_local_visibility": opportunity_fresh,
                "observation_age_seconds": observation_age,
                "visible_players": visible_players,
                "note": (
                    "Informational visibility only. Start player combat only for an explicit goal "
                    "or immediate defense; an empty list never creates work."
                ),
            },
            "recent": qualifying[-10:],
        }

    def _campaign_execution_status(
        self, goal: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if not goal:
            return None
        run = self.storage.campaign_run(str(goal.get("id") or ""))
        if run is None:
            return None
        phase = self.storage.active_campaign_phase(run["id"])
        phases = self.storage.campaign_phases(run["id"])
        attempt_phase = phase or (phases[-1] if phases else None)
        attempts = (
            self.storage.phase_attempts(attempt_phase["id"], limit=8)
            if attempt_phase
            else []
        )
        return {
            "run_id": run["id"],
            "status": run["status"],
            "strategy_summary": run.get("strategy_summary"),
            "active_phase": phase,
            "recent_phases": phases[-8:],
            "recent_attempts": attempts,
            "progress_checkpoint": run.get("progress_checkpoint"),
            "external_blocker": run.get("external_blocker"),
            "action_breaker_limit": self.campaign.ACTION_FAILURE_LIMIT,
        }

    def _supervision_status(
        self,
        *,
        active: dict[str, Any] | None,
        queued: list[dict[str, Any]],
        health: dict[str, Any] | None,
        observation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        observation = observation or self.last_observation or {}
        inactive = self.storage.goals(["paused", "blocked"])
        current = active or (
            max(inactive, key=lambda item: str(item.get("updated_at") or ""))
            if inactive
            else None
        )
        keeper: dict[str, Any] | None = None
        if health and self.config.game.agent in health.get("sessions", []):
            try:
                raw_keeper = self.broker.call_tool(
                    "autopilot",
                    {"agent": self.config.game.agent, "action": "status"},
                    timeout=3,
                    mutation=False,
                )
                if isinstance(raw_keeper, dict):
                    keeper = {
                        key: raw_keeper.get(key)
                        for key in (
                            "running",
                            "inert",
                            "mode",
                            "activity",
                            "goal_id",
                            "started_at",
                            "updated_at",
                            "error",
                        )
                        if key in raw_keeper
                    }
            except (BrokerError, TypeError, ValueError):
                keeper = {"status": "unavailable"}
        foreground_action = (
            dict(self._foreground_action)
            if isinstance(self._foreground_action, dict)
            else None
        )
        if keeper is not None and foreground_action is not None:
            keeper["control_owner"] = "controller_foreground_action"
            keeper["suspension_expected"] = not self._keeper_is_driving(keeper)
        deferred = self.storage.goal_lessons(statuses=["deferred"], limit=20)
        unlocked = self.storage.goal_lessons(statuses=["unlocked"], limit=20)
        actionable_unlocked = [
            lesson for lesson in unlocked if lesson.get("scope") == "goal"
        ]
        lesson_values = []
        for lesson in [*deferred, *actionable_unlocked]:
            public = self.learning.public_lesson(
                lesson, evaluation=self.learning.evaluate_retry(lesson, observation)
            )
            lesson_values.append(self._compact_lesson(public))
        completion = current.get("completion", {}) if current else {}
        criteria = [
            {
                key: item.get(key)
                for key in ("id", "kind", "met", "detail")
            }
            for item in completion.get("criteria", [])
            if isinstance(item, dict)
        ]
        planner_feedback = self._planner_feedback(current) if current else None
        execution_plan = self._execution_plan(current) if current else None
        purchase_preflights = self.storage.get_runtime(PURCHASE_PREFLIGHT_RUNTIME_KEY, {})
        purchase_preflight = (
            purchase_preflights.get(current["id"])
            if current and isinstance(purchase_preflights, dict)
            else None
        )
        now_text = timestamp()
        value = {
            "now_utc": now_text,
            "now_local": self.notifications.journal.local_datetime(now_text).isoformat(
                timespec="seconds"
            ),
            "timezone": self.config.deployment.timezone,
            "controller": {
                "state": self.state,
                "since": self.started_at,
                "version": self.VERSION,
                "last_heartbeat_at": self.last_heartbeat_at,
                "heartbeat_age_seconds": self._age_seconds(self.last_heartbeat_at),
                "control_owner": (
                    "foreground_action"
                    if foreground_action is not None
                    else (
                        "keeper"
                        if self._keeper_is_driving(keeper)
                        else "controller"
                    )
                ),
                "foreground_action": foreground_action,
            },
            "game": {
                "connection": "joined"
                if health and self.config.game.agent in health.get("sessions", [])
                else "disconnected",
                "character_name": self._character_name(observation),
                "location": deep_get(observation, "look.room.name", deep_get(observation, "look.room")),
                "room_id": deep_get(observation, "look.room.num", deep_get(observation, "look.room_id")),
                "position": deep_get(observation, "status.position"),
                "vitals": deep_get(observation, "status.vitals", deep_get(observation, "look.vitals", {})),
                "risk": self._risk(observation),
                "carried_currency": self._carried_currency(observation),
                "finances": self._financial_context(observation),
                "visible_players": self._local_visible_players(observation),
                "observation_age_seconds": round(
                    max(0.0, time.time() - float(observation.get("observed_at", time.time()))),
                    1,
                ),
            },
            "onboarding": self._onboarding_status(observation),
            "goal": None
            if current is None
            else {
                "id": current.get("id"),
                "title": current.get("title"),
                "objective": current.get("objective"),
                "status": current.get("status"),
                "version": current.get("version"),
                "priority": current.get("priority"),
                "progress_percent": completion.get("percent_estimate", 0),
                "progress_summary": completion.get("summary"),
                "criteria": criteria,
                "execution_plan": None
                if execution_plan is None
                else {
                    "status": deep_get(execution_plan, "verification.status"),
                    "summary": execution_plan.get("summary"),
                    "steps": execution_plan.get("steps", []),
                    "last_action": execution_plan.get("last_action"),
                    "updated_at": execution_plan.get("updated_at"),
                },
                "purchase_preflight": purchase_preflight,
            },
            "queue": [
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "priority": item.get("priority"),
                }
                for item in queued[:3]
            ],
            "attention": {
                "liveness": self._compact_liveness(current, broker_activity=keeper),
                "planner_feedback": planner_feedback,
                "warnings": self.warnings[-5:],
                "pending_proposals": len(self.storage.proposals()),
                "deferred_goal_count": sum(
                    1 for item in deferred if item.get("scope") == "goal"
                ),
                "deferred_tactic_count": sum(
                    1 for item in deferred if item.get("scope") == "tactic"
                ),
                "eligible_retry_count": len(actionable_unlocked),
            },
            "campaign": {
                "execution": self._campaign_execution_status(current),
                "readiness": self._compact_readiness(observation),
                "development": self._compact_character_development(observation),
                "lessons": lesson_values[:8],
                "pvp_today": self._pvp_today_summary(),
            },
            "dependencies": self.dependencies,
        }
        return redact(value)

    def _journal_assessment_context(self) -> dict[str, Any]:
        observation = self.last_observation or {}
        active = self.storage.active_goal()
        inactive = self.storage.goals(["paused", "blocked"])
        current_goal = active or (
            max(inactive, key=lambda goal: str(goal.get("updated_at") or ""))
            if inactive
            else None
        )
        queued = self.storage.goals(["queued"])
        lessons = self.storage.goal_lessons(statuses=["deferred", "unlocked"], limit=20)
        liveness = self._compact_liveness(current_goal)
        current_plan = self._execution_plan(current_goal) if current_goal else None
        purchase_preflights = self.storage.get_runtime(PURCHASE_PREFLIGHT_RUNTIME_KEY, {})
        current_purchase_preflight = (
            purchase_preflights.get(current_goal["id"])
            if current_goal and isinstance(purchase_preflights, dict)
            else None
        )
        return redact(
            {
                "controller": {"state": self.state, "dependencies": self.dependencies},
                "character": self._character_name(observation),
                "location": deep_get(observation, "look.room.name", deep_get(observation, "look.room")),
                "vitals": deep_get(observation, "status.vitals", deep_get(observation, "look.vitals", {})),
                "risk": self._risk(observation),
                "liveness": liveness,
                "combat_readiness": self.learning.readiness_summary(observation),
                "combat_history": self.learning.combat_summary(observation, limit=12),
                "execution_plan": current_plan,
                "purchase_preflight": current_purchase_preflight,
                "abilities": {
                    "skills": deep_get(observation, "abilities.skills", []),
                    "spells": deep_get(observation, "abilities.spells", []),
                    "advancement": deep_get(observation, "abilities.advancement", {}),
                },
                "active_goal": None
                if active is None
                else {
                    "id": active["id"],
                    "title": active["title"],
                    "objective": active["objective"],
                    "status": active["status"],
                    "completion": active["completion"],
                },
                "current_goal": None
                if current_goal is None
                else {
                    "id": current_goal["id"],
                    "title": current_goal["title"],
                    "objective": current_goal["objective"],
                    "status": current_goal["status"],
                    "completion": current_goal["completion"],
                    "active": current_goal.get("status") == "active",
                },
                "queued_goals": [
                    {"id": goal["id"], "title": goal["title"], "priority": goal["priority"]}
                    for goal in queued[:5]
                ],
                "pending_proposals": len(self.storage.proposals()),
                "campaign_lessons": [
                    {
                        "id": lesson["id"],
                        "status": lesson["status"],
                        "scope": lesson["scope"],
                        "classification": lesson["classification"],
                        "summary": lesson["summary"],
                    }
                    for lesson in lessons
                ],
                # The source batch supplies the delta. Older operational events
                # made the assessor repeatedly recap already-journaled milestones.
                # Current state and durable combat/lesson summaries are enough to
                # explain significance without turning the journal into a log.
            }
        )

    @staticmethod
    def _character_name(observation: dict[str, Any]) -> Any:
        return deep_get(
            observation,
            "status.character",
            deep_get(observation, "look.self.name", deep_get(observation, "look.character")),
        )

    def _foreground_status_observation(
        self, observation: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Project cheap live progress while a synchronous action owns the turn."""
        if not isinstance(self._foreground_action, dict):
            return observation, None
        try:
            live = self.broker.call_tool(
                "status",
                {"agent": self.config.game.agent, "brief": True},
                timeout=3,
                mutation=False,
            )
        except (BrokerError, TypeError, ValueError):
            return observation, None
        if not isinstance(live, dict):
            return observation, None
        raw_room = (
            live.get("where")
            if isinstance(live.get("where"), dict)
            else live.get("room")
        )
        raw_room = raw_room if isinstance(raw_room, dict) else {}
        room_id = raw_room.get("num", raw_room.get("id"))
        room_name = raw_room.get("name")
        vitals = live.get("vitals") if isinstance(live.get("vitals"), dict) else None
        if room_id is None and room_name is None and vitals is None:
            return observation, None

        projected = dict(observation)
        look = dict(projected.get("look") or {})
        if room_id is not None or room_name is not None:
            previous_room = (
                look.get("room") if isinstance(look.get("room"), dict) else {}
            )
            look["room"] = {
                "num": room_id if room_id is not None else previous_room.get("num"),
                "name": (
                    room_name
                    if room_name is not None
                    else previous_room.get("name")
                ),
            }
        projected["look"] = look
        projected_status = dict(projected.get("status") or {})
        if vitals is not None:
            projected_status["vitals"] = vitals
        if isinstance(live.get("position"), dict):
            projected_status["position"] = live["position"]
        if live.get("character"):
            projected_status["character"] = live["character"]
        projected["status"] = projected_status
        projected["observed_at"] = time.time()
        return projected, {
            "observed_at": timestamp(),
            "room_id": room_id,
            "location": room_name,
            "position": redact(live.get("position")),
            "vitals": redact(vitals),
        }

    def status(self, *, detail: str = "summary", include_recent_events: int = 3) -> dict[str, Any]:
        if detail not in {"supervision", "summary", "goal", "diagnostic"}:
            raise ValueError("detail must be supervision, summary, goal, or diagnostic")
        observation = self.last_observation or {}
        foreground_action = (
            dict(self._foreground_action)
            if isinstance(self._foreground_action, dict)
            else None
        )
        observation, live_progress = self._foreground_status_observation(observation)
        if foreground_action is not None and live_progress is not None:
            foreground_action["progress"] = live_progress
        active = self.storage.active_goal()
        queued = self.storage.goals(["queued"])
        recent = self.storage.latest_events(limit=200, interesting_only=True)
        planner_feedback = self._planner_feedback(active) if active else None
        health = None
        try:
            health = self.broker.health(timeout=1)
        except BrokerError:
            pass
        if detail in {"supervision", "goal"}:
            compact = self._supervision_status(
                active=active,
                queued=queued,
                health=health,
                observation=observation,
            )
            if detail == "goal":
                inactive = self.storage.goals(["paused", "blocked"])
                current = active or (
                    max(inactive, key=lambda item: str(item.get("updated_at") or ""))
                    if inactive
                    else None
                )
                compact["goal_detail"] = current
                if include_recent_events:
                    compact["recent_events"] = recent[
                        -max(0, min(20, include_recent_events)) :
                    ]
                return redact(compact)
            return compact
        campaign_memory = self.learning.status_summary(observation)
        campaign_execution = self._campaign_execution_status(active)
        value: dict[str, Any] = {
            "controller": {
                "state": self.state,
                "since": self.started_at,
                "version": self.VERSION,
                "last_heartbeat_at": self.last_heartbeat_at,
                "control_owner": "foreground_action" if foreground_action else "controller",
                "foreground_action": foreground_action,
            },
            "game": {
                "connection": "joined" if health and self.config.game.agent in health.get("sessions", []) else "disconnected",
                "server": f"{self.config.game.host}:{self.config.game.port}",
                "character_name": self._character_name(observation),
                "location": deep_get(observation, "look.room.name", deep_get(observation, "look.room")),
                "vitals": deep_get(observation, "status.vitals", deep_get(observation, "look.vitals", {})),
                "risk": self._risk(observation),
                "finances": self._financial_context(observation),
                "observation_age_seconds": round(max(0.0, time.time() - float(observation.get("observed_at", time.time()))), 1),
            },
            "onboarding": self._onboarding_status(observation),
            "goal": None if not active else {"id": active["id"], "title": active["title"], "status": active["status"], "progress_percent": active["completion"].get("percent_estimate", 0), "current_step": active["completion"].get("summary")},
            "queue": {"count": len(queued), "next_title": queued[0]["title"] if queued else None},
            "attention": {
                "pending_proposals": len(self.storage.proposals()),
                "recent_consequential_actions": len(self.storage.recent_consequences()),
                "warnings": self.warnings,
                "planner_feedback": planner_feedback,
                "deferred_goals": len(campaign_memory["deferred_goals"]),
                "eligible_retries": len(campaign_memory["eligible_retries"]),
                "recent_combat_deaths": campaign_memory.get("combat_readiness", {}).get("recent_combat_deaths", 0),
            },
            "dependencies": self.dependencies,
            "knowledge": {
                "corpus_version": self.knowledge.corpus_version,
                "entities": self.knowledge.metadata().get("entity_count", 0),
                "harness_revision": self.knowledge.metadata().get("harness_revision"),
            },
            "campaign_memory": campaign_memory,
            "campaign_execution": campaign_execution,
            "last_interesting_event": recent[-1] if recent else None,
        }
        if include_recent_events:
            value["recent_events"] = recent[-max(0, min(20, include_recent_events)):]
        if detail == "diagnostic":
            value["goal_detail"] = active
            value["diagnostic"] = {"broker_health": health, "policy_version": self.policy.VERSION, "model": self.config.model.name, "knowledge": self.knowledge.metadata(), "recent_consequences": self.storage.recent_consequences(), "goal_lessons": self.storage.goal_lessons(limit=100)}
        return redact(value)

    @staticmethod
    def _risk(observation: dict[str, Any]) -> str:
        health = deep_get(observation, "status.vitals.health", deep_get(observation, "look.vitals.health"))
        if isinstance(health, dict) and health.get("max"):
            ratio = float(health.get("current", health.get("value", 0))) / float(health["max"])
            return "critical" if ratio < 0.4 else "elevated" if ratio < 0.7 else "low"
        return "unknown"

    def safe_stop(self) -> None:
        self.state = "stopping"
        self.stop_event.set()
        try:
            self._set_fallback()
        except BrokerError:
            pass

    def close(self) -> None:
        self.stop_event.set()
        if (
            self._notification_thread
            and self._notification_thread.is_alive()
            and self._notification_thread is not threading.current_thread()
        ):
            self._notification_thread.join(
                timeout=min(55.0, max(2.0, self.config.model.responder_timeout_seconds + 5.0))
            )
        if (
            self._social_thread
            and self._social_thread.is_alive()
            and self._social_thread is not threading.current_thread()
        ):
            self._social_thread.join(
                timeout=min(55.0, max(2.0, self.config.model.responder_timeout_seconds + 5.0))
            )
        self.broker.shutdown_owned_process()
        self.storage.close()
