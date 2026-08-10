from __future__ import annotations

import copy
from typing import Any

from .broker import Tool, ToolCallError
from .utils import uuid7


class SimulatedBroker:
    """Deterministic harness substitute used for policy and recovery tests."""

    def __init__(self) -> None:
        self.agent = "primary"
        self.room = {"num": 100, "name": "Training Hall"}
        self.vitals = {"health": {"current": 100, "max": 100}, "mana": {"current": 50, "max": 50}}
        self.inventory_items = [{"id": 1, "name": "Rusty sword", "amount": 1, "can": ["use", "drop"]}]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.joined = True
        schema_agent = {"type": "object", "properties": {"agent": {"type": "string"}}, "required": ["agent"]}
        self.tools = {
            "join": Tool("join", "join", {"type": "object", "properties": {"agent": {}, "account": {}, "password": {}}, "required": ["agent", "account", "password"]}),
            "look": Tool("look", "look", {"type": "object", "properties": {"agent": {"type": "string"}, "cached": {"type": "boolean"}}, "required": ["agent"]}),
            "status": Tool("status", "status", {"type": "object", "properties": {"agent": {}, "brief": {}}, "required": ["agent"]}),
            "inventory": Tool("inventory", "inventory", schema_agent),
            "autopilot": Tool("autopilot", "autopilot", {"type": "object", "properties": {"agent": {}, "action": {}, "mode": {}, "hunt": {}, "assigned_room": {}, "max_carry": {}, "rest_below": {}, "flee_below": {}, "fight_above_vigor": {}, "use_safe_spots": {}, "hold_resume_above": {}, "bank_above": {}, "pull_within": {}, "break_out_via_logoff": {}}, "required": ["agent", "action"]}),
            "wait_for_event": Tool("wait_for_event", "wait", {"type": "object", "properties": {"agent": {}}, "required": ["agent"]}),
            "act": Tool("act", "act", {"type": "object", "properties": {"agent": {}, "verb": {"enum": ["use", "unuse", "get", "drop", "activate", "go"]}, "target": {}}, "required": ["agent", "verb"]}),
            "travel": Tool("travel", "travel", {"type": "object", "properties": {"agent": {}, "to": {}}, "required": ["agent", "to"]}),
            "rest": Tool("rest", "rest", {"type": "object", "properties": {"agent": {}, "stand": {}}, "required": ["agent"]}),
            "progress": Tool("progress", "advancement progress", schema_agent),
            "hunting_grounds": Tool("hunting_grounds", "hunting grounds", {"type": "object", "properties": {"near": {"type": "string"}}}),
            "prey": Tool("prey", "rank prey", {"type": "object", "properties": {"agent": {}, "purpose": {}, "goals": {}, "karma": {}, "over": {}, "limit": {}}}),
            "converse": Tool("converse", "converse", {"type": "object", "properties": {"agent": {}, "action": {}, "ack": {}, "small_talk": {}, "face_speaker": {}, "escalate": {}, "answer_peers": {}, "replies_per_min": {}, "speaker_cooldown_ms": {}, "per_speaker_per_min": {}}, "required": []}),
            "inbox": Tool("inbox", "inbox", {"type": "object", "properties": {"agent": {}, "action": {}, "state": {}, "limit": {}, "id": {}, "text": {}, "note": {}}, "required": ["action"]}),
            "say": Tool("say", "say", {"type": "object", "properties": {"agent": {}, "text": {}, "type": {}}, "required": ["agent", "text"]}),
        }

    def health(self, timeout: float = 3) -> dict[str, Any]:
        return {"ok": True, "sessions": [self.agent] if self.joined else [], "tools": len(self.tools), "root": "simulated"}

    def ensure_started(self) -> dict[str, Any]:
        return self.health()

    def ensure_joined(self) -> dict[str, Any]:
        self.joined = True
        return {"joined": True}

    def shutdown_owned_process(self) -> None:
        return None

    def capabilities(self, *, refresh: bool = False) -> dict[str, Tool]:
        return self.tools

    def planner_tools(self) -> list[dict[str, Any]]:
        return [tool.planner_view() for name, tool in self.tools.items() if name not in {"join", "converse", "inbox"}]

    def observe(self) -> dict[str, Any]:
        return {
            "id": uuid7(),
            "observed_at": 0,
            "look": {"room": copy.deepcopy(self.room), "self": {"name": "Simone"}, "vitals": copy.deepcopy(self.vitals)},
            "status": {"vitals": copy.deepcopy(self.vitals)},
            "inventory": {"items": copy.deepcopy(self.inventory_items)},
        }

    def call_tool(self, name: str, arguments: dict[str, Any], *, timeout: float = 180, mutation: bool = False) -> Any:
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "look":
            return self.observe()["look"]
        if name == "status":
            return self.observe()["status"]
        if name == "inventory":
            return self.observe()["inventory"]
        if name == "inbox":
            return {"untrusted": True, "count": 0, "messages": []}
        if name == "act" and arguments.get("verb") == "drop":
            target = arguments.get("target")
            before = len(self.inventory_items)
            self.inventory_items = [item for item in self.inventory_items if item["id"] != target and str(target).lower() not in item["name"].lower()]
            if len(self.inventory_items) == before:
                raise ToolCallError("simulator could not find item")
            return {"verb": "drop", "events": ["inventory_changed"]}
        if name == "travel":
            destination = int(arguments.get("to"))
            self.room = {"num": destination, "name": f"Room {destination}"}
            return {"arrived": True, "room_id": destination}
        return {"ok": True, "tool": name}
