from __future__ import annotations

import re
from typing import Any


CRITERION_KINDS = (
    "state_equals",
    "numeric_threshold",
    "numeric_delta",
    "inventory_contains",
    "location_reached",
    "event_occurred",
    "composite_all",
    "composite_any",
    "operator_confirmed",
)

# Event-backed criteria are deliberately closed over durable controller events
# whose semantics are stable enough to verify a future goal outcome.  The
# legacy engagement kind remains accepted only so pre-phase PvP goals can be
# loaded and upgraded; new guidance uses the correlated phase event instead.
GOAL_EVENT_KINDS = (
    "pvp.phase.completed",
    "property.transaction",
    "conversation.responded",
    "pvp.engagement.completed",
)

CRITERION_FIELDS_BY_KIND = {
    "state_equals": frozenset({"id", "kind", "path", "value"}),
    "numeric_threshold": frozenset({"id", "kind", "metric", "operator", "value"}),
    "numeric_delta": frozenset({"id", "kind", "metric", "operator", "value", "baseline"}),
    "inventory_contains": frozenset({"id", "kind", "item", "count"}),
    "location_reached": frozenset({"id", "kind", "location", "room", "room_id"}),
    "event_occurred": frozenset({"id", "kind", "event_kind", "after_cursor"}),
    "composite_all": frozenset({"id", "kind", "criteria", "criterion_ids"}),
    "composite_any": frozenset({"id", "kind", "criteria", "criterion_ids"}),
    "operator_confirmed": frozenset({"id", "kind"}),
}


def parse_ability_metric(metric: Any) -> tuple[str, str] | None:
    """Parse a stable virtual metric such as ``ability.spell.Blink``."""
    parts = str(metric or "").split(".", 2)
    if (
        len(parts) != 3
        or parts[0].casefold() != "ability"
        or parts[1].casefold() not in {"skill", "spell"}
        or not parts[2].strip()
    ):
        return None
    return parts[1].casefold(), " ".join(parts[2].split())


def ability_name_key(value: Any) -> str:
    """Match broker and corpus ability names despite punctuation/case variants."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


GOAL_CONSTRAINT_FIELDS = frozenset(
    {"avoid_death", "bank_before_hazard", "operator_notes", "purchase_plan"}
)

PURCHASE_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Required static feasibility and funding claim for a goal that explicitly buys an item or "
        "learns a paid skill/spell. The knowledge validator canonicalizes the exact offering, "
        "instantiated merchant/teacher class, catalogue relation, and numeric room before submission. "
        "A fresh live in-room shop quote is still required before the controller permits the transaction."
    ),
    "properties": {
        "offering_kind": {
            "type": "string",
            "enum": ["item", "skill", "spell"],
            "default": "item",
            "description": (
                "Kind of offering. Omit only for a physical item; paid training must explicitly use skill or spell."
            ),
        },
        "item": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Exact canonical item, skill, or spell name, never a category such as armor or weapon skills."
            ),
        },
        "merchant_class": {
            "type": "string",
            "minLength": 1,
            "description": "Exact class from the merchant catalogue, such as CorNothSergeant.",
        },
        "room_id": {
            "type": "integer",
            "minimum": 1,
            "description": "Exact instantiated room returned for this merchant class.",
        },
        "maximum_price": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Maximum live quoted price the controller may authorize. Required and positive for paid skill/spell training so funds can be withdrawn before travel."
            ),
        },
    },
    "required": ["item", "merchant_class", "room_id"],
    "additionalProperties": False,
}

CRITERION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "One deterministic success verifier. Use a stable unique id when another composite criterion refers to it. "
        "Fields not used by the selected kind are rejected."
    ),
    "properties": {
        "id": {"type": "string", "minLength": 1, "description": "Stable criterion identifier; generated if omitted."},
        "kind": {
            "type": "string",
            "enum": list(CRITERION_KINDS),
            "description": "Verifier implementation to use.",
        },
        "path": {"type": "string", "minLength": 1, "description": "Dot path in the verified observation for state_equals."},
        "metric": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Dot path to a numeric observed value, or a named live ability metric in the form "
                "ability.skill.<canonical name> or ability.spell.<canonical name>."
            ),
        },
        "operator": {
            "type": "string",
            "enum": [">=", ">", "<=", "<", "=="],
            "default": ">=",
            "description": "Numeric comparison operator.",
        },
        "value": {"description": "Expected JSON value for state_equals, or numeric target for numeric criteria."},
        "baseline": {"type": "number", "description": "Starting observed value subtracted by numeric_delta."},
        "item": {"type": "string", "minLength": 1, "description": "Case-insensitive item-name substring."},
        "count": {"type": "integer", "minimum": 1, "default": 1, "description": "Minimum verified inventory count."},
        "location": {"type": "string", "minLength": 1, "description": "Case-insensitive room-name substring."},
        "room": {"type": "string", "minLength": 1, "description": "Alias for location."},
        "room_id": {
            "type": ["integer", "string"],
            "description": "Exact room identifier, useful when names are ambiguous.",
        },
        "event_kind": {
            "type": "string",
            "enum": list(GOAL_EVENT_KINDS),
            "description": (
                "Exact goal-verifiable durable event kind. Use pvp.phase.completed for a correlated PvP phase; "
                "pvp.engagement.completed is accepted only for legacy-goal migration."
            ),
        },
        "after_cursor": {"type": "integer", "minimum": 0, "default": 0, "description": "Only events after this cursor count."},
        "criteria": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
            "description": "Criterion ids referenced by a composite verifier.",
        },
        "criterion_ids": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
            "description": "Alias for criteria.",
        },
    },
    "required": ["kind"],
    "allOf": [
        {"if": {"properties": {"kind": {"const": "state_equals"}}}, "then": {"required": ["path", "value"]}},
        {"if": {"properties": {"kind": {"const": "numeric_threshold"}}}, "then": {"required": ["metric", "value"]}},
        {"if": {"properties": {"kind": {"const": "numeric_delta"}}}, "then": {"required": ["metric", "value", "baseline"]}},
        {"if": {"properties": {"kind": {"const": "inventory_contains"}}}, "then": {"required": ["item"]}},
        {
            "if": {"properties": {"kind": {"const": "location_reached"}}},
            "then": {"anyOf": [{"required": ["location"]}, {"required": ["room"]}, {"required": ["room_id"]}]},
        },
        {"if": {"properties": {"kind": {"const": "event_occurred"}}}, "then": {"required": ["event_kind"]}},
        {
            "if": {"properties": {"kind": {"enum": ["composite_all", "composite_any"]}}},
            "then": {"anyOf": [{"required": ["criteria"]}, {"required": ["criterion_ids"]}]},
        },
    ],
    "additionalProperties": False,
}

GOAL_CONSTRAINTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Goal-specific guidance. It cannot weaken the no-cheating policy or grant player-chat authority.",
    "properties": {
        "avoid_death": {"type": "boolean", "description": "Prefer tactics that reduce avoidable death risk."},
        "bank_before_hazard": {"type": "boolean", "description": "Bank carried value before planned danger when practical."},
        "purchase_plan": PURCHASE_PLAN_SCHEMA,
        "operator_notes": {
            "type": "string",
            "maxLength": 4000,
            "description": "Additional operator guidance for this goal; never an approval gate or policy override.",
        },
    },
    "additionalProperties": False,
}

STATUS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Read controller, game, goal, queue, durable campaign-memory lessons, dependency, and recent-event status without invoking the LLM. Use supervision for frequent management checks.",
    "properties": {
        "detail": {
            "type": "string",
            "enum": ["supervision", "summary", "goal", "diagnostic"],
            "default": "supervision",
            "description": (
                "supervision is a compact management view with semantic liveness, current/paused goal, "
                "readiness, live character development, lessons, and today's PvP count; summary preserves the richer campaign-memory "
                "view; goal adds the full active goal; diagnostic also adds broker/model/policy details."
            ),
        },
        "include_recent_events": {
            "type": "integer",
            "minimum": 0,
            "maximum": 20,
            "default": 3,
            "description": "Number of recent interesting redacted events to include; 0 omits them.",
        },
    },
    "additionalProperties": False,
}

SUBMIT_GOAL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Create one durable high-level goal. Supply observable deterministic success criteria; this queues work and does "
        "not execute a game action during the MCP call. Reusing request_id is safe only with byte-equivalent intent. "
        "Equivalent goals learned to be impossible in the current state return GOAL_DEFERRED with exact retry conditions "
        "and supporting-goal suggestions; do not paraphrase around that gate."
    ),
    "properties": {
        "request_id": {"type": "string", "minLength": 1, "description": "Unique idempotency key for this exact submission."},
        "title": {"type": "string", "minLength": 1, "maxLength": 120, "description": "Optional short title; derived from objective if omitted."},
        "objective": {"type": "string", "minLength": 1, "maxLength": 4000, "description": "Required outcome, not raw broker-tool instructions."},
        "success_criteria": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": CRITERION_SCHEMA,
            "description": "Typed verifiers the controller must observe before declaring success.",
        },
        "constraints": GOAL_CONSTRAINTS_SCHEMA,
        "priority": {"type": "integer", "minimum": 0, "maximum": 100, "default": 50, "description": "Higher queued goals run first."},
        "activation": {
            "type": "string",
            "enum": ["queue", "replace_active_pause", "replace_active_cancel"],
            "default": "queue",
            "description": "Queue normally, or atomically pause/cancel the current goal before promoting this one.",
        },
    },
    "required": ["request_id", "objective", "success_criteria"],
    "additionalProperties": False,
}

MANAGE_GOAL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Mutate an existing durable goal. Read status first and pass its goal version as expected_version to detect stale decisions. "
        "confirm_complete works only for an explicit operator_confirmed criterion after every observable criterion is verified."
    ),
    "properties": {
        "request_id": {"type": "string", "minLength": 1, "description": "Unique idempotency key for this exact mutation."},
        "goal_id": {"type": "string", "minLength": 1, "description": "Durable goal id returned by status or submit_goal."},
        "expected_version": {"type": "integer", "minimum": 1, "description": "Latest goal version; stale values are rejected without mutation."},
        "action": {
            "type": "string",
            "enum": ["pause", "resume", "cancel", "reprioritize", "confirm_complete"],
            "description": "resume requeues; reprioritize requires priority; confirm_complete is narrowly restricted.",
        },
        "priority": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Required only for reprioritize."},
        "reason": {"type": "string", "description": "Optional human-readable audit reason."},
        "cause": {
            "type": "string",
            "enum": [
                "operator_requested",
                "safety",
                "invalid",
                "durably_stalled",
                "superseded",
                "opportunity_ended",
            ],
            "description": (
                "Structured justification for cancelling an active goal. The controller verifies every cause except "
                "operator_requested, which the supervisor may use only when the human explicitly requested cancellation. "
                "opportunity_ended applies only to an exact fresh-local pvp_engage-only goal whose target is no "
                "longer locally visible before a qualifying phase. Without a verified cause, fresh or recently "
                "progressing goals are protected from cancellation."
            ),
        },
    },
    "required": ["request_id", "goal_id", "action"],
    "allOf": [
        {"if": {"properties": {"action": {"const": "reprioritize"}}}, "then": {"required": ["priority"]}}
    ],
    "additionalProperties": False,
}

PROPOSALS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "List inert bot-proposed goals, or accept/reject one. Only accept creates a queued goal; accept may return GOAL_DEFERRED until a learned retry predicate is met.",
    "properties": {
        "action": {"type": "string", "enum": ["list", "accept", "reject"], "description": "list is read-only; accept/reject decide one pending proposal."},
        "request_id": {"type": "string", "minLength": 1, "description": "Unique idempotency key; required for accept or reject."},
        "proposal_id": {"type": "string", "minLength": 1, "description": "Pending proposal id; required for accept or reject."},
        "reason": {"type": "string", "description": "Optional audit reason for the decision."},
    },
    "required": ["action"],
    "allOf": [
        {
            "if": {"properties": {"action": {"enum": ["accept", "reject"]}}},
            "then": {"required": ["request_id", "proposal_id"]},
        }
    ],
    "additionalProperties": False,
}

EVENTS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Read the durable redacted event stream in ascending cursor order; use next_cursor for reliable catch-up.",
    "properties": {
        "after_cursor": {"type": "integer", "minimum": 0, "default": 0, "description": "Return only events with cursor greater than this value."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50, "description": "Maximum events returned in this page."},
        "interesting_only": {"type": "boolean", "default": False, "description": "When true, return only notification-worthy events."},
        "kinds": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
            "description": "Optional exact event-kind allowlist, for example goal.succeeded or character.death.",
        },
    },
    "additionalProperties": False,
}
