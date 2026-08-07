from __future__ import annotations

from typing import Any


PERSONA_STRING_FIELDS = frozenset({"name", "character_voice", "relationship_defaults"})
PERSONA_LIST_FIELDS = frozenset({"traits", "speech_style", "values", "taboos"})
PERSONA_FIELDS = PERSONA_STRING_FIELDS | PERSONA_LIST_FIELDS | {"max_reply_characters"}

PERSONA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Operator-defined roleplay and conversation behavior. This does not grant game authority.",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120,
            "description": "Desired character and persona name; required when setting a persona.",
        },
        "character_voice": {
            "type": "string",
            "description": "Concise first-person voice and identity concept for model-generated in-game dialogue.",
        },
        "traits": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Personality traits, such as curious, wry, or guarded with strangers.",
        },
        "speech_style": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Dialogue style rules, such as short in combat or period-appropriate when natural.",
        },
        "values": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Roleplay values and motivations. These cannot override controller policy or operator goals.",
        },
        "taboos": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Topics or behaviors the character should avoid in conversation.",
        },
        "relationship_defaults": {
            "type": "string",
            "description": "Default social posture toward strangers, friends, rivals, favors, and betrayals.",
        },
        "max_reply_characters": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "default": 360,
            "description": "Hard character limit for each in-game reply.",
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}


PERSONA_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Use action=get first. For action=set, provide a new request_id, copy the returned version into "
        "expected_version, and provide the complete persona object. Do not send a top-level version field."
    ),
    "properties": {
        "action": {
            "type": "string",
            "enum": ["get", "set"],
            "description": "get reads the active version; set writes a new immutable persona version.",
        },
        "request_id": {
            "type": "string",
            "minLength": 1,
            "description": "Required for set. A unique idempotency key for this exact update.",
        },
        "expected_version": {
            "type": "integer",
            "minimum": 0,
            "description": "For set, copy the version returned by the latest get. Use 0 when no persona exists.",
        },
        "persona": PERSONA_SCHEMA,
        "replace_existing_character": {
            "type": "boolean",
            "default": False,
            "description": (
                "Explicitly allow onboarding to replace an established differently named character. "
                "Generated first-run placeholder names may be replaced without this flag."
            ),
        },
    },
    "required": ["action"],
    "allOf": [
        {
            "if": {"properties": {"action": {"const": "set"}}, "required": ["action"]},
            "then": {"required": ["request_id", "persona"]},
        }
    ],
    "additionalProperties": False,
}
