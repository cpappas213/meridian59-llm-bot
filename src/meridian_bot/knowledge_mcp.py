from __future__ import annotations

import copy
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .contracts import SUBMIT_GOAL_INPUT_SCHEMA
from .knowledge import ENTITY_KINDS


PROTOCOL_VERSION = "2024-11-05"


def _goal_draft_schema() -> dict[str, Any]:
    properties = copy.deepcopy(SUBMIT_GOAL_INPUT_SCHEMA["properties"])
    properties.pop("request_id", None)
    return {
        "type": "object",
        "description": "One proposed durable Meridian goal to ground before submission.",
        "properties": properties,
        "required": ["objective", "success_criteria"],
        "additionalProperties": False,
    }


KINDS_SCHEMA = {
    "type": "array",
    "items": {"type": "string", "enum": list(ENTITY_KINDS)},
    "uniqueItems": True,
    "description": "Optional entity-type filter: location, region, spell, skill, creature, NPC, item, equipment, reagent, or guide.",
}


TOOLS = [
    {
        "name": "search",
        "description": (
            "Search the pinned, source-derived Meridian 59 corpus. Returns canonical entities, structured facts, "
            "source citations, hashes, and corpus version. Read-only and never invokes an LLM or moves the character."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "description": "Game fact or name to search for."},
                "kinds": KINDS_SCHEMA,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 8,
                    "description": "Maximum number of ranked matches to return.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "resolve",
        "description": (
            "Resolve an exact game name, alias, class, slug, or numeric room id. Returns found, ambiguous, or "
            "not_found and never silently converts a guess into a canonical entity."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "description": "Exact entity name or identifier."},
                "kinds": KINDS_SCHEMA,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 8,
                    "description": "Maximum number of candidates to return when resolution is ambiguous.",
                },
                "allow_fuzzy": {
                    "type": "boolean",
                    "default": False,
                    "description": "Permit a single very-high-confidence fuzzy match; leave false for goal validation.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get",
        "description": "Read one canonical entity with full indexed text, structured facts, relationships, and provenance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Canonical id returned by search or resolve, for example location:52 or spell:blink.",
                }
            },
            "required": ["entity_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "validate_goal",
        "description": (
            "Validate and canonicalize a goal before submit_goal. Unknown locations fail, ambiguous rooms require a "
            "numeric id, common observation metrics are normalized, and every result identifies the corpus used."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"goal": _goal_draft_schema()},
            "required": ["goal"],
            "additionalProperties": False,
        },
    },
    {
        "name": "progression_context",
        "description": (
            "Get evidence-backed max-HP progression options. Combines the source-derived creature corpus with live "
            "ordinary-client advancement and hunting-ground data when the controller is connected. The compact "
            "default preserves decision-critical complete spawn mixes and empirical summaries without raw history. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_health": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional current maximum HP; omitted uses the controller's latest observation.",
                },
                "karma": {
                    "type": "string",
                    "enum": ["evil", "good", "neutral"],
                    "description": "Optional desired karma direction for live prey recommendations.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 8,
                    "description": "Maximum number of progression candidates to return.",
                },
                "detail": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": "Use compact for routine goal selection; full includes raw evidence and combat/readiness history for a specific diagnosis.",
                },
            },
            "additionalProperties": False,
        },
    },
]


class ControlClient:
    def __init__(self) -> None:
        self.base_url = os.environ.get("M59_BOT_CONTROL_URL", "http://127.0.0.1:8903").rstrip("/")
        self.token = os.environ.get("M59_BOT_CONTROL_TOKEN", "")
        secret_file = os.environ.get("M59_BOT_SECRET_FILE", "")
        if not self.token and secret_file:
            try:
                with open(secret_file, encoding="utf-8-sig") as handle:
                    for raw in handle:
                        key, separator, value = raw.strip().partition("=")
                        if separator and key == "M59_BOT_CONTROL_TOKEN":
                            self.token = value.strip().strip('"').strip("'")
                            break
            except OSError:
                pass

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"accept": "application/json", "authorization": f"Bearer {self.token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except Exception:
                detail = {"code": "HTTP_ERROR", "message": str(exc)}
            raise RuntimeError(json.dumps(detail, ensure_ascii=False)) from exc

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name in {"search", "resolve"}:
            query: dict[str, Any] = {
                "q": arguments["query"],
                "limit": arguments.get("limit", 8),
            }
            if isinstance(arguments.get("kinds"), list):
                query["kinds"] = ",".join(arguments["kinds"])
            if name == "resolve":
                query["allow_fuzzy"] = str(bool(arguments.get("allow_fuzzy", False))).lower()
            return self.request("GET", f"/v1/knowledge/{name}?" + urllib.parse.urlencode(query))
        if name == "get":
            entity_id = urllib.parse.quote(str(arguments["entity_id"]), safe="")
            return self.request("GET", f"/v1/knowledge/entities/{entity_id}")
        if name == "validate_goal":
            return self.request("POST", "/v1/knowledge/validate-goal", {"goal": arguments["goal"]})
        if name == "progression_context":
            body: dict[str, Any] = {
                "limit": arguments.get("limit", 8),
                "detail": arguments.get("detail", "compact"),
            }
            if arguments.get("max_health") is not None:
                body["character_state"] = {"max_health": arguments["max_health"]}
            if arguments.get("karma") is not None:
                body["karma"] = arguments["karma"]
            return self.request("POST", "/v1/knowledge/progression-context", body)
        raise ValueError(f"unknown tool {name}")


def serve() -> None:
    client = ControlClient()
    for raw in sys.stdin:
        request_id: Any = None
        try:
            message = json.loads(raw)
            request_id = message.get("id")
            method = message.get("method")
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "meridian_knowledge", "version": "0.2.0"},
                }
            elif method in {"notifications/initialized", "initialized"}:
                continue
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = message.get("params") or {}
                try:
                    output = client.call(str(params.get("name")), params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False, indent=2)}]}
                except Exception as exc:
                    result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            elif method in {"resources/list", "prompts/list"}:
                result = {"resources": []} if method.startswith("resources") else {"prompts": []}
            else:
                _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}})
                continue
            if request_id is not None:
                _write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as exc:
            if request_id is not None:
                _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32700, "message": str(exc)}})
            else:
                sys.stderr.write(f"meridian_knowledge MCP ignored malformed notification: {exc}\n")
                sys.stderr.flush()


def _write(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
