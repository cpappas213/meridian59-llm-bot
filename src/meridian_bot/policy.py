from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import PolicyConfig
from .utils import canonical_json, deep_get, uuid7


class PolicyDenied(RuntimeError):
    code = "POLICY_DENIED"


@dataclass(frozen=True)
class Decision:
    id: str
    decision: str
    action_class: str
    matched_rules: tuple[str, ...]
    facts: dict[str, Any]
    summary: str
    notify: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.id,
            "decision": self.decision,
            "action_class": self.action_class,
            "matched_rules": list(self.matched_rules),
            "facts": self.facts,
            "summary": self.summary,
            "notify": self.notify,
        }


class PolicyEngine:
    VERSION = "fair-play-v2"
    PLANNER_DENY = {
        "join",
        "leave",
        "fleet",
        "recording",
        "converse",
        "inbox",
        # The harness implements every reroll action through a destructive
        # character-lifecycle surface.  This is a hard boundary, not an
        # autonomous consequence that a planner may accept with caution.
        "reroll",
    }
    CONSEQUENCE_TOOLS = {"trade", "sell", "sell_all", "supply", "split"}

    def __init__(self, config: PolicyConfig):
        self.config = config

    def summary(self, observation: dict[str, Any] | None = None) -> dict[str, Any]:
        value = {
            "version": self.VERSION,
            "hard_boundary": "ordinary player play only; no cheating",
            "autonomous": True,
            "no_approval_steps": True,
            "guidance": {
                "avoid_death": self.config.avoid_death,
                "bank_before_hazard": self.config.bank_before_hazard,
                "protected_property": "strong caution plus informational pre/post log; action remains autonomous",
                "character_replacement": "permanently disabled; the controller cannot suicide, reroll, replace, or recreate a character",
                "drop_alignment": "strong caution plus informational pre/post log; action remains autonomous",
            },
        }
        if observation is not None:
            value["current_advisories"] = self.advisories(observation)
        return value

    def advisories(self, observation: dict[str, Any]) -> list[dict[str, Any]]:
        advisories: list[dict[str, Any]] = []
        health = deep_get(observation, "status.vitals.health", deep_get(observation, "look.vitals.health"))
        if isinstance(health, dict) and health.get("max"):
            current = health.get("current", health.get("value", 0))
            ratio = float(current) / float(health["max"])
            if ratio < self.config.critical_health_fraction:
                advisories.append({"kind": "survival_interrupt", "urgency": "critical", "guidance": "do not initiate danger; let survival autopilot recover or withdraw", "health_fraction": ratio})
            elif ratio < self.config.rest_health_fraction:
                advisories.append({"kind": "recover_before_danger", "urgency": "high", "guidance": "recover before initiating a hazardous phase", "health_fraction": ratio})
        items = deep_get(observation, "inventory.items", [])
        carried = 0
        for item in items if isinstance(items, list) else []:
            if any(word in str(item.get("name", "")).lower() for word in ("shilling", "money")):
                carried += int(item.get("amount", 1) or 1)
        if self.config.bank_before_hazard and carried >= self.config.carried_currency_bank_threshold:
            advisories.append({
                "kind": "consider_banking",
                "urgency": "informational",
                "guidance": (
                    "consider whether banking fits the plan; carried wealth never blocks travel or combat"
                ),
                "carried_currency": carried,
                "threshold": self.config.carried_currency_bank_threshold,
                "advisory_only": True,
            })
        return advisories

    def evaluate(
        self,
        tool: str,
        arguments: dict[str, Any],
        observation: dict[str, Any],
        goal: dict[str, Any],
        *,
        known_tools: set[str],
    ) -> Decision:
        decision_id = uuid7()
        if tool not in known_tools:
            return Decision(decision_id, "deny", "invalid_action", ("INTEGRITY-CAPABILITY-001",), {}, "Capability is unavailable.", False)
        if tool in self.PLANNER_DENY:
            return Decision(decision_id, "deny", "invalid_action", ("AUTH-TOOL-001",), {}, "The planner cannot invoke controller-owned tools.", True)
        if "agent" in arguments and not isinstance(arguments["agent"], str):
            return Decision(decision_id, "deny", "invalid_action", ("INTEGRITY-SCHEMA-001",), {}, "Agent identifier has an invalid type.", False)

        action_class, rules, facts = self._classify(tool, arguments, observation)
        if action_class:
            return Decision(
                decision_id,
                "allow_with_caution",
                action_class,
                tuple(rules),
                facts,
                f"{action_class.replace('_', ' ').title()} may proceed autonomously after durable preflight.",
                True,
            )
        return Decision(decision_id, "allow", "ordinary_game_action", ("FAIR-ORDINARY-001",), {}, "Ordinary player capability allowed.", False)

    def _classify(self, tool: str, arguments: dict[str, Any], observation: dict[str, Any]) -> tuple[str | None, list[str], dict[str, Any]]:
        if tool == "act" and arguments.get("verb") == "drop":
            item = self._inventory_match(arguments.get("target"), observation)
            return "item_drop", ["GUIDE-PROPERTY-001"], {"transaction": "drop", "item": item, "protected": self._protected(item), "uncertainty": "medium" if not item else "low"}
        if tool == "bank" and arguments.get("action") in {"deposit", "withdraw"}:
            return "protected_property_transaction", ["GUIDE-PROPERTY-001"], {
                "transaction": f"bank_{arguments.get('action')}",
                "item": "shillings",
                "amount": arguments.get("amount"),
                "protected": True,
                "uncertainty": "low",
            }
        if tool in self.CONSEQUENCE_TOOLS:
            target = arguments.get("item") or arguments.get("target") or arguments.get("what")
            item = self._inventory_match(target, observation)
            return "protected_property_transaction", ["GUIDE-PROPERTY-001"], {"transaction": tool, "item": item or target, "protected": self._protected(item), "uncertainty": "medium"}
        if tool == "safety" and arguments.get("on") is False:
            return "alignment_change", ["GUIDE-ALIGNMENT-001"], {"expected_direction": "possible decrease", "uncertainty": "high"}
        if tool in {"pvp_engage", "pvp_seek"}:
            return "player_combat", ["GUIDE-PVP-001", "GUIDE-PROPERTY-001"], {
                "target": arguments.get("target"),
                "search_rooms": arguments.get("rooms") if tool == "pvp_seek" else None,
                "disengage_at": arguments.get("disengage_at", self.config.rest_health_fraction),
                "loot_requested": arguments.get("loot", True) is not False,
                "possible_alignment_change": True,
                "possible_property_transfer": True,
                "uncertainty": "high",
            }
        return None, [], {}

    def consequence_assessment(self, decision: Decision, goal: dict[str, Any], rationale: str) -> dict[str, Any]:
        return {
            "id": uuid7(),
            "action_class": decision.action_class,
            "target": {"goal_id": goal.get("id"), **decision.facts},
            "expected_effects": {"permanence": "persistent", "uncertainty": decision.facts.get("uncertainty", "medium")},
            "goal_rationale": rationale,
            "safer_alternatives": ["Replan to preserve progression or property"] if decision.action_class != "alignment_change" else ["Choose a tactic without an expected karma change"],
            "guidance": self.config.consequential_action_guidance,
            "decision": "allow_with_caution",
            "summary": decision.summary,
            "goal_id": goal.get("id"),
            "policy_decision_id": decision.id,
            "notify": True,
        }

    def _inventory_match(self, target: Any, observation: dict[str, Any]) -> dict[str, Any] | None:
        items = deep_get(observation, "inventory.items", [])
        for item in items if isinstance(items, list) else []:
            if target == item.get("id") or (target is not None and str(target).lower() in str(item.get("name", "")).lower()):
                return item
        return None

    def _protected(self, item: dict[str, Any] | None) -> bool:
        if item is None:
            return True
        name = str(item.get("name", "")).lower()
        return any(fragment.lower() in name for fragment in self.config.protected_item_names)

    @staticmethod
    def prompt_injection_signature(arguments: dict[str, Any]) -> bool:
        text = canonical_json(arguments).lower()
        return any(marker in text for marker in ("ignore previous instructions", "system prompt", "m59_account_password"))
