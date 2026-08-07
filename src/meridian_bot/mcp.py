from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .contracts import (
    EVENTS_INPUT_SCHEMA,
    MANAGE_GOAL_INPUT_SCHEMA,
    PROPOSALS_INPUT_SCHEMA,
    STATUS_INPUT_SCHEMA,
    SUBMIT_GOAL_INPUT_SCHEMA,
)
from .persona import PERSONA_TOOL_INPUT_SCHEMA


PROTOCOL_VERSION = "2024-11-05"


TOOLS = [
    {
        "name": "status",
        "description": "Inspect the durable Meridian 59 bot, current game state, semantic liveness, goal queue, dependencies, and warnings. Use detail=supervision for routine management. Read-only; no LLM call or game move.",
        "inputSchema": STATUS_INPUT_SCHEMA,
    },
    {
        "name": "submit_goal",
        "description": "Submit typed high-level intent to the durable looping bot. The call queues a goal; it does not perform a game move or require the MCP supervisor to remain open.",
        "inputSchema": SUBMIT_GOAL_INPUT_SCHEMA,
    },
    {
        "name": "manage_goal",
        "description": "Pause, resume, cancel, reprioritize, or narrowly confirm an operator-confirmed durable goal. Fresh active goals are protected from premature cancellation: supply a documented cause only when it is true; pause is the ordinary reversible choice. Mutations are versioned and idempotent.",
        "inputSchema": MANAGE_GOAL_INPUT_SCHEMA,
    },
    {
        "name": "proposals",
        "description": "List or decide inert bot-proposed follow-up goals. Listing is read-only; accepting creates a queued durable goal.",
        "inputSchema": PROPOSALS_INPUT_SCHEMA,
    },
    {
        "name": "persona",
        "description": (
            "Read or set the character's versioned persona and initiate first-run character onboarding. "
            "Call get first. Set requires request_id "
            "and a persona containing name plus the documented voice/style fields; pass the get response's "
            "version as expected_version, not as version. An established differently named character is "
            "preserved unless replace_existing_character=true is explicitly supplied. Persona does not alter "
            "ordinary gameplay authority."
        ),
        "inputSchema": PERSONA_TOOL_INPUT_SCHEMA,
    },
    {
        "name": "events",
        "description": "Read redacted durable bot events with reliable cursor pagination after supervisor downtime. Read-only; does not poll or move the game character.",
        "inputSchema": EVENTS_INPUT_SCHEMA,
    },
]


class ControlClient:
    def __init__(self) -> None:
        self.base_url = os.environ.get("M59_BOT_CONTROL_URL", "http://127.0.0.1:8903").rstrip("/")
        self.token = os.environ.get("M59_BOT_CONTROL_TOKEN", "")
        secret_file = os.environ.get("M59_BOT_SECRET_FILE", "")
        if not self.token and secret_file:
            try:
                for raw in open(secret_file, encoding="utf-8-sig"):
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
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except Exception:
                detail = {"code": "HTTP_ERROR", "message": str(exc)}
            raise RuntimeError(json.dumps(detail)) from exc

    def call(self, name: str, args: dict[str, Any]) -> Any:
        if name == "status":
            return self.request("GET", "/v1/status?" + urllib.parse.urlencode({"detail": args.get("detail", "supervision"), "include_recent_events": args.get("include_recent_events", 0)}))
        if name == "submit_goal":
            return self.request("POST", "/v1/goals", args)
        if name == "manage_goal":
            goal_id = urllib.parse.quote(str(args["goal_id"]), safe="")
            return self.request("POST", f"/v1/goals/{goal_id}/commands", {key: value for key, value in args.items() if key != "goal_id"})
        if name == "proposals":
            if args.get("action") == "list":
                return self.request("GET", "/v1/proposals")
            proposal_id = urllib.parse.quote(str(args["proposal_id"]), safe="")
            return self.request("POST", f"/v1/proposals/{proposal_id}/decision", {"request_id": args.get("request_id"), "action": args["action"], "reason": args.get("reason")})
        if name == "persona":
            if args.get("action") == "get":
                return self.request("GET", "/v1/persona")
            return self.request("PUT", "/v1/persona", {key: value for key, value in args.items() if key != "action"})
        if name == "events":
            query = dict(args)
            if isinstance(query.get("kinds"), list):
                query["kinds"] = ",".join(query["kinds"])
            return self.request("GET", "/v1/events?" + urllib.parse.urlencode(query))
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
                result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "meridian_bot", "version": "0.2.0"}}
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
                sys.stderr.write(f"meridian_bot MCP ignored malformed notification: {exc}\n")
                sys.stderr.flush()


def _write(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
