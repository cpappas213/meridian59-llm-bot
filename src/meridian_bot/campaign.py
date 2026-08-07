from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .criteria import CriteriaEvaluator
from .storage import Storage
from .utils import canonical_json, deep_get, json_hash, redact, timestamp


PHASE_KINDS = {
    "general",
    "research_progression",
    "prepare_combat",
    "free_inventory_capacity",
    "liquidate_inventory",
    "acquire_item",
    "train_ability",
    "farm",
    "recover",
    "return_home",
    "pvp_opportunity",
}


# The phase vocabulary is semantic, while the temporary execution adapter still
# maps each selected intent to tested broker tools.  This is deliberately much
# smaller than the broker's full ordinary-player surface and can shrink further
# as composite capability runners replace raw mechanics.
PHASE_TOOL_NAMES: dict[str, set[str]] = {
    "general": {
        "look",
        "map",
        "travel",
        "inventory",
        "equipment",
        "abilities",
        "merchants",
        "act",
        "rest_up",
        "equip_best",
        "bank",
        "shop",
        "sell",
        "sell_all",
        "knowledge_search",
    },
    "research_progression": {
        "prey",
        "hunting_grounds",
        "safe_spots",
        "history",
        "abilities",
        "equipment",
        "knowledge_search",
    },
    "prepare_combat": {
        "inventory",
        "equipment",
        "abilities",
        # Raw act is required to drop a known-broken weapon or explicitly
        # use/unuse a selected item when the composite equipment helpers have
        # already proved they cannot make progress.
        "act",
        "equip_best",
        "wear_best",
        "rest_up",
        "spells",
        "cast",
        "shop",
        "bank",
        "map",
        "travel",
        "knowledge_search",
    },
    "free_inventory_capacity": {
        "inventory",
        "merchants",
        "map",
        "travel",
        "sell",
        "sell_all",
        "bank",
        "act",
        "knowledge_search",
    },
    "liquidate_inventory": {
        "inventory",
        "merchants",
        "map",
        "travel",
        # Liquidation may end by replacing required equipment with the
        # proceeds, or by dropping confirmed junk/broken property when no
        # grounded buyer accepts it. Both are ordinary game actions and remain
        # subject to the controller's plan and transaction logging.
        "shop",
        "sell",
        "sell_all",
        "bank",
        "act",
        "knowledge_search",
    },
    "acquire_item": {
        "inventory",
        "equipment",
        "merchants",
        "map",
        "travel",
        "shop",
        "sell",
        "sell_all",
        "bank",
        "equip_best",
        "wear_best",
        "act",
        "knowledge_search",
    },
    "train_ability": {
        "abilities",
        "spells",
        "inventory",
        "merchants",
        "map",
        "travel",
        "shop",
        "bank",
        "cast",
        "fight",
        "equip_best",
        "knowledge_search",
    },
    "farm": {
        "prey",
        "hunting_grounds",
        "safe_spots",
        "inventory",
        "equipment",
        "abilities",
        "equip_best",
        "wear_best",
        "rest_up",
        "shop",
        "bank",
        "map",
        "travel",
        "autopilot",
        "fight",
        "knowledge_search",
    },
    "recover": {
        "look",
        "status",
        "rest_up",
        "escape_underworld",
        "leave_raza",
        "map",
        "travel",
        "inventory",
        "spells",
        "cast",
        "shop",
        "bank",
        "knowledge_search",
    },
    "return_home": {"look", "map", "travel", "walk_to", "go_through", "knowledge_search"},
    "pvp_opportunity": {"look", "inventory", "equipment", "bank", "map", "travel", "pvp_engage"},
}


@dataclass(frozen=True)
class PhaseOutcome:
    completed: bool
    failed: bool
    phase: dict[str, Any] | None
    detail: dict[str, Any]


class CampaignCoordinator:
    """Durable internal execution hierarchy beneath one public strategic goal."""

    ACTION_FAILURE_LIMIT = 2

    def __init__(self, storage: Storage, criteria: CriteriaEvaluator):
        self.storage = storage
        self.criteria = criteria

    def ensure(self, goal: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        run = self.storage.ensure_campaign_run(goal)
        return run, self.storage.active_campaign_phase(run["id"])

    @staticmethod
    def compatible_phase(goal: dict[str, Any]) -> dict[str, Any]:
        """Fallback for test doubles and legacy callers without a manager role."""
        return {
            "kind": "general",
            "objective": str(goal.get("objective") or goal.get("title") or "Advance the active goal."),
            "success_criteria": list(goal.get("success_criteria", [])),
            "abandon_predicates": [],
            "budget": {"max_actions": 24, "max_minutes": 45},
            "context": {"compatibility_phase": True},
            "rationale": "Wrap the legacy executor in a durable internal phase.",
        }

    @staticmethod
    def fallback_phase(
        goal: dict[str, Any], observation: dict[str, Any]
    ) -> dict[str, Any]:
        """Choose a conservative local milestone after invalid manager output."""
        for criterion in goal.get("success_criteria", []):
            if not isinstance(criterion, dict) or criterion.get("kind") != "numeric_threshold":
                continue
            metric = str(criterion.get("metric") or "")
            if metric not in {
                "max_health",
                "status.vitals.health.max",
                "look.vitals.health.max",
            }:
                continue
            current = deep_get(observation, metric)
            if current is None and metric == "max_health":
                current = deep_get(
                    observation,
                    "status.vitals.health.max",
                    deep_get(observation, "look.vitals.health.max"),
                )
            target = criterion.get("value")
            if (
                isinstance(current, (int, float))
                and not isinstance(current, bool)
                and isinstance(target, (int, float))
                and not isinstance(target, bool)
                and current < target
            ):
                milestone = min(float(target), float(current) + 1)
                if isinstance(current, int) and isinstance(target, int):
                    milestone = int(milestone)
                return {
                    "kind": "research_progression",
                    "objective": (
                        f"Identify an executable prey, room, and combat tactic for raising "
                        f"maximum HP from {current} to {milestone}."
                    ),
                    "success_criteria": [
                        {
                            "id": f"local-farm-recipe-{milestone}",
                            "kind": "operator_confirmed",
                        }
                    ],
                    "abandon_predicates": [],
                    "budget": {"max_actions": 40, "max_minutes": 90},
                    "context": {
                        "deterministic_fallback": True,
                        "next_hp_milestone": milestone,
                        "required_farm_context": [
                            "target",
                            "room",
                            "use_safe_spots",
                            "flee_below",
                            "fight_above_vigor",
                        ],
                    },
                    "rationale": (
                        "Invalid campaign-manager output cannot safely launch a generic farm. "
                        "Research one grounded executable recipe before farming."
                    ),
                }
        return CampaignCoordinator.compatible_phase(goal)

    def apply_manager_decision(
        self,
        run: dict[str, Any],
        goal: dict[str, Any],
        decision: dict[str, Any],
        observation: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        action = str(decision.get("decision") or "").strip()
        if action in {"start_phase", "replace_phase", "push_support_phase"}:
            phase = decision.get("phase")
            if not isinstance(phase, dict):
                raise ValueError(f"{action} requires a phase object")
            kind = str(phase.get("kind") or "").strip()
            if kind not in PHASE_KINDS:
                raise ValueError(
                    f"unsupported campaign phase kind {kind!r}; supported: {', '.join(sorted(PHASE_KINDS))}"
                )
            criteria = phase.get("success_criteria")
            if not isinstance(criteria, list) or not criteria:
                raise ValueError("campaign phase requires at least one deterministic success criterion")
            criteria = self._normalize_phase_success_criteria(criteria)
            # Reuse the public criterion validator without creating a public goal.
            self.storage._validate_goal(
                {
                    "title": str(phase.get("objective") or "Internal campaign phase")[:120],
                    "objective": str(phase.get("objective") or "Internal campaign phase"),
                    "success_criteria": criteria,
                    "constraints": {},
                    "priority": int(goal.get("priority", 50)),
                    "activation": "queue",
                }
            )
            abandon_predicates = phase.get("abandon_predicates", [])
            if not isinstance(abandon_predicates, list):
                raise ValueError("campaign phase abandon_predicates must be an array")
            valid_abandon_predicates: list[dict[str, Any]] = []
            ignored_abandon_predicates: list[Any] = []
            for predicate in abandon_predicates:
                try:
                    self.storage._validate_goal(
                        {
                            "title": "Internal campaign phase abandonment predicate",
                            "objective": "Detect a verified reason to reconsider this internal phase.",
                            "success_criteria": [predicate],
                            "constraints": {},
                            "priority": int(goal.get("priority", 50)),
                            "activation": "queue",
                        }
                    )
                    valid_abandon_predicates.append(predicate)
                except (TypeError, ValueError):
                    # Abandonment is optional. Dropping an untyped model suggestion is
                    # safer than discarding an otherwise valid phase or treating prose
                    # as a deterministic reason to quit useful work.
                    ignored_abandon_predicates.append(redact(predicate))
            raw_budget = phase.get("budget") if isinstance(phase.get("budget"), dict) else {}
            try:
                requested_actions = int(raw_budget.get("max_actions", 24))
                requested_minutes = int(raw_budget.get("max_minutes", 45))
            except (TypeError, ValueError) as exc:
                raise ValueError("campaign phase budget values must be integers") from exc
            phase_context = (
                dict(phase.get("context"))
                if isinstance(phase.get("context"), dict)
                else {}
            )
            if ignored_abandon_predicates:
                phase_context["ignored_invalid_abandon_predicates"] = (
                    ignored_abandon_predicates
                )
            if criteria != phase.get("success_criteria"):
                phase_context["normalized_success_criteria"] = [
                    "equipment.wielding scalar expected value converted to the broker's canonical name array"
                ]
            if observation is not None and valid_abandon_predicates:
                valid_abandon_predicates, initially_true = (
                    self._filter_initially_true_abandonment(
                        goal, valid_abandon_predicates, observation
                    )
                )
                if initially_true:
                    phase_context["ignored_initially_true_abandon_predicates"] = (
                        initially_true
                    )
            phase = {
                **phase,
                "success_criteria": criteria,
                "abandon_predicates": valid_abandon_predicates,
                "context": phase_context,
                # A model cannot abandon an otherwise viable phase after one
                # or two ordinary actions. Breakers still react immediately to
                # repeated equivalent failure, which is a materially good reason.
                "budget": {
                    "max_actions": max(8, min(requested_actions, 200)),
                    "max_minutes": max(30, min(requested_minutes, 480)),
                },
            }
            mode = "push" if action == "push_support_phase" else "replace"
            if self.storage.active_campaign_phase(run["id"]) is None:
                mode = "start"
            return self.storage.create_campaign_phase(run, phase, mode=mode)
        if action == "resume_parent_phase":
            current = self.storage.active_campaign_phase(run["id"])
            if current is None:
                return None
            self.storage.transition_campaign_phase(
                current["id"], "succeeded", reason=str(decision.get("rationale") or "supporting work complete"), resume_parent=True
            )
            return self.storage.active_campaign_phase(run["id"])
        if action in {"complete_campaign_candidate", "report_external_blocker_candidate"}:
            return None
        raise ValueError(
            "campaign manager decision must be start_phase, replace_phase, push_support_phase, "
            "resume_parent_phase, complete_campaign_candidate, or report_external_blocker_candidate"
        )

    @staticmethod
    def _normalize_phase_success_criteria(
        criteria: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for criterion in criteria:
            value = dict(criterion) if isinstance(criterion, dict) else criterion
            if (
                isinstance(value, dict)
                and value.get("kind") == "state_equals"
                and value.get("path") == "equipment.wielding"
                and isinstance(value.get("value"), str)
            ):
                value["value"] = [value["value"]]
            normalized.append(value)
        return normalized

    def _filter_initially_true_abandonment(
        self,
        goal: dict[str, Any],
        predicates: list[dict[str, Any]],
        observation: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        remaining: list[dict[str, Any]] = []
        ignored: list[dict[str, Any]] = []
        for predicate in predicates:
            result = self.criteria.evaluate(
                {"id": goal["id"], "success_criteria": [predicate]}, observation
            )
            if result.get("all_met") is True:
                ignored.append(redact(predicate))
            else:
                remaining.append(predicate)
        return remaining, ignored

    def evaluate_phase(
        self,
        goal: dict[str, Any],
        run: dict[str, Any],
        phase: dict[str, Any] | None,
        observation: dict[str, Any],
    ) -> PhaseOutcome:
        if phase is None:
            return PhaseOutcome(False, False, None, {"reason": "no_active_phase"})
        raw_criteria = phase.get("success_criteria", [])
        criteria = self._normalize_phase_success_criteria(raw_criteria)
        abandon = phase.get("abandon_predicates", [])
        context = (
            dict(phase.get("context"))
            if isinstance(phase.get("context"), dict)
            else {}
        )
        normalization_reasons: list[str] = []
        if criteria != raw_criteria:
            context["normalized_success_criteria"] = [
                "equipment.wielding scalar expected value converted to the broker's canonical name array"
            ]
            normalization_reasons.append("normalized equipment.wielding success value")
        if (
            isinstance(abandon, list)
            and abandon
            and int(phase.get("attempt_count", 0) or 0) == 0
        ):
            abandon, initially_true = self._filter_initially_true_abandonment(
                goal, abandon, observation
            )
            if initially_true:
                context["ignored_initially_true_abandon_predicates"] = initially_true
                normalization_reasons.append(
                    "ignored abandonment already true before the first attempt"
                )
        if normalization_reasons:
            phase = self.storage.update_campaign_phase_guardrails(
                phase["id"],
                abandon_predicates=abandon,
                context=context,
                success_criteria=criteria,
                reason="; ".join(normalization_reasons),
            )
        pseudo_goal = {"id": goal["id"], "success_criteria": criteria}
        if isinstance(abandon, list) and abandon:
            abandonment = self.criteria.evaluate(
                {"id": goal["id"], "success_criteria": abandon}, observation
            )
            triggered = [
                item
                for item in abandonment.get("criteria", [])
                if isinstance(item, dict) and item.get("met") is True
            ]
            if triggered:
                reason = "verified phase abandonment predicate: " + "; ".join(
                    str(item.get("detail") or item.get("id") or item.get("kind"))
                    for item in triggered[:5]
                )
                finished = self.storage.transition_campaign_phase(
                    phase["id"], "failed", reason=reason, resume_parent=False
                )
                return PhaseOutcome(
                    False,
                    True,
                    finished,
                    {"reason": reason, "abandonment": abandonment},
                )
        completion = self.criteria.evaluate(pseudo_goal, observation)
        if completion.get("all_met") is True:
            finished = self.storage.transition_campaign_phase(
                phase["id"],
                "succeeded",
                reason="all deterministic phase criteria verified",
                resume_parent=bool(phase.get("parent_phase_id")),
            )
            self.storage.update_campaign_memory(
                run["id"],
                progress_checkpoint={
                    "phase_id": phase["id"],
                    "phase_kind": phase["kind"],
                    "completion": completion,
                    "recorded_at": timestamp(),
                },
            )
            return PhaseOutcome(True, False, finished, completion)
        return PhaseOutcome(False, False, phase, completion)

    @staticmethod
    def budget_exhausted(phase: dict[str, Any]) -> dict[str, Any] | None:
        budget = phase.get("budget") if isinstance(phase.get("budget"), dict) else {}
        max_actions = max(8, int(budget.get("max_actions", 24) or 24))
        max_minutes = max(30, int(budget.get("max_minutes", 45) or 45))
        attempts = int(phase.get("attempt_count", 0) or 0)
        activated_at = str(phase.get("activated_at") or phase.get("created_at") or "")
        elapsed_minutes = 0.0
        try:
            activated = datetime.fromisoformat(activated_at.replace("Z", "+00:00"))
            elapsed_minutes = max(
                0.0,
                (datetime.now(timezone.utc) - activated.astimezone(timezone.utc)).total_seconds()
                / 60.0,
            )
        except (TypeError, ValueError):
            pass
        if attempts >= max_actions:
            return {
                "kind": "action_budget",
                "attempts": attempts,
                "limit": max_actions,
                "elapsed_minutes": round(elapsed_minutes, 1),
            }
        if elapsed_minutes >= max_minutes:
            return {
                "kind": "time_budget",
                "elapsed_minutes": round(elapsed_minutes, 1),
                "limit": max_minutes,
                "attempts": attempts,
            }
        return None

    @classmethod
    def _stable_state_value(cls, value: Any) -> Any:
        """Remove cache age/provenance noise from breaker state signatures."""
        if isinstance(value, dict):
            stable: dict[str, Any] = {}
            for raw_key, nested in value.items():
                key = str(raw_key)
                folded = key.casefold()
                if (
                    folded in {
                        "source",
                        "source_ref",
                        "note",
                        "wielding_note",
                        "observed_at",
                        "recorded_at",
                        "updated_at",
                        "created_at",
                    }
                    or folded.endswith("_ms")
                    or folded.endswith("_at")
                ):
                    continue
                stable[key] = cls._stable_state_value(nested)
            return stable
        if isinstance(value, list):
            stable_items = [cls._stable_state_value(item) for item in value]
            # Inventory/equipment/catalog ordering is presentation, not state.
            return sorted(stable_items, key=canonical_json)
        return value

    @classmethod
    def state_fingerprint(cls, observation: dict[str, Any]) -> dict[str, Any]:
        return cls._stable_state_value({
            "room": deep_get(observation, "look.room.num", deep_get(observation, "look.room.name")),
            "health": deep_get(observation, "status.vitals.health", deep_get(observation, "look.vitals.health")),
            "vigor": deep_get(observation, "status.vitals.vigor.value", deep_get(observation, "look.vitals.vigor.value")),
            "inventory": deep_get(observation, "inventory.items", []),
            "carry": deep_get(observation, "inventory.carry", {}),
            "equipment": observation.get("equipment"),
            "abilities": observation.get("abilities"),
        })

    def action_signature(
        self,
        phase: dict[str, Any],
        tool: str,
        arguments: dict[str, Any],
        observation: dict[str, Any],
        expected_effect: Any,
    ) -> str:
        return json_hash(
            {
                "phase_kind": phase.get("kind"),
                "tool": tool,
                "arguments": arguments,
                "expected_effect": expected_effect or {},
                "state": self.state_fingerprint(observation),
            }
        )

    def prepare_attempt(
        self,
        phase: dict[str, Any] | None,
        *,
        tool: str,
        arguments: dict[str, Any],
        observation: dict[str, Any],
        expected_effect: Any,
    ) -> tuple[str | None, str | None]:
        if phase is None:
            return None, None
        signature = self.action_signature(phase, tool, arguments, observation, expected_effect)
        attempts = self.storage.phase_attempts(phase["id"], signature=signature, limit=10)
        failures = [
            item for item in attempts if item.get("status") in {"failed", "suppressed"}
        ]
        if len(failures) >= self.ACTION_FAILURE_LIMIT:
            return None, signature
        attempt_id = self.storage.create_phase_attempt(
            phase["id"],
            semantic_action=tool,
            signature=signature,
            expected_effect=expected_effect or {},
        )
        return attempt_id, None

    def finish_attempt(
        self,
        goal: dict[str, Any],
        run: dict[str, Any] | None,
        phase: dict[str, Any] | None,
        phase_attempt_id: str | None,
        *,
        status: str,
        action_attempt_id: str | None = None,
        result: Any = None,
        verification: Any = None,
        reason: str = "",
    ) -> dict[str, Any]:
        if phase is None or phase_attempt_id is None:
            return {"breaker_tripped": False}
        attempt = self.storage.update_phase_attempt(
            phase_attempt_id,
            status,
            action_attempt_id=action_attempt_id,
            result=redact(result),
            verification=redact(verification),
        )
        attempts = self.storage.phase_attempts(
            phase["id"], signature=attempt["signature"], limit=10
        )
        failures = [
            item for item in attempts if item.get("status") in {"failed", "suppressed"}
        ]
        if len(failures) < self.ACTION_FAILURE_LIMIT:
            return {"breaker_tripped": False, "failure_count": len(failures)}
        return self.trip_breaker(
            goal,
            phase,
            signature=attempt["signature"],
            semantic_action=attempt["semantic_action"],
            failure_count=len(failures),
            reason=reason,
        )

    def trip_breaker(
        self,
        goal: dict[str, Any],
        phase: dict[str, Any] | None,
        *,
        signature: str,
        semantic_action: str,
        failure_count: int,
        reason: str = "",
    ) -> dict[str, Any]:
        if phase is None:
            return {"breaker_tripped": False}
        # End the smallest unit. The public strategic goal deliberately remains
        # active and the manager will select a materially different phase.
        current = self.storage.active_campaign_phase(phase["run_id"])
        if current and current["id"] == phase["id"]:
            self.storage.transition_campaign_phase(
                phase["id"],
                "failed",
                reason=reason or "two equivalent semantic actions failed in unchanged state",
                resume_parent=False,
            )
            self.storage.emit_event(
                "campaign.breaker.tripped",
                f"Changed internal phase after repeated {semantic_action} failure",
                severity="warning",
                interesting=True,
                goal_id=goal["id"],
                data={
                    "run_id": phase["run_id"],
                    "phase_id": phase["id"],
                    "signature": signature,
                    "failure_count": failure_count,
                    "reason": reason[:1000],
                    "strategic_goal_preserved": True,
                },
            )
        return {
            "breaker_tripped": True,
            "failure_count": failure_count,
            "phase_id": phase["id"],
        }

    @staticmethod
    def tools_for_phase(
        phase: dict[str, Any] | None,
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        kind = str((phase or {}).get("kind") or "general")
        allowed = PHASE_TOOL_NAMES.get(kind, PHASE_TOOL_NAMES["general"])
        selected = [tool for tool in tools if str(tool.get("name") or "") in allowed]
        # Never strand a model because an older broker lacks one preferred
        # adapter. A phase still gets the read-only knowledge tool when present.
        if not selected:
            selected = [
                tool
                for tool in tools
                if str(tool.get("name") or "") in PHASE_TOOL_NAMES["general"]
            ]
        return selected

    def context(
        self,
        run: dict[str, Any],
        phase: dict[str, Any] | None,
    ) -> dict[str, Any]:
        history = self.storage.campaign_phases(run["id"])[-12:]
        attempt_phase = phase or (history[-1] if history else None)
        attempts = (
            self.storage.phase_attempts(attempt_phase["id"], limit=12)
            if attempt_phase
            else []
        )
        return {
            "run": run,
            "active_phase": phase,
            "recent_phases": history,
            "recent_phase_attempts": attempts,
            "action_breaker_limit": self.ACTION_FAILURE_LIMIT,
            "instructions": (
                "Preserve the strategic goal across routine failures. Supporting preparation is an internal "
                "child phase, never a public supervisor goal. Select a materially different tactic after a breaker."
            ),
        }
