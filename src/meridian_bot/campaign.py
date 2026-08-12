from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .contracts import parse_ability_metric
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
        # Progression research may need to move out of a sanctuary and inspect
        # the locally connected route before the read-only creature/room
        # adapters can produce useful evidence. Mutation still requires a
        # verified execution-plan step and the ordinary controller safeguards.
        "look",
        "map",
        "travel",
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
        # The controller-owned purchase plan may use one exact live-map go
        # exit after ordinary travel reports an unusable duplicate edge.
        "go_through",
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
        # Paid training shares the guarded purchase-route recovery path.
        "go_through",
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
    "return_home": {
        "look",
        "map",
        "travel",
        "walk_to",
        "go_through",
        "leave_raza",
        "knowledge_search",
    },
    "pvp_opportunity": {"look", "inventory", "equipment", "bank", "map", "travel", "pvp_engage"},
}

PHASE_ACTION_SUCCEEDED = "phase_action_succeeded"
PHASE_ACTION_CRITERION_FIELDS = frozenset({"id", "kind", "tools"})

# Campaign-manager output uses this small semantic vocabulary. The controller
# compiles targets into verifier criteria, so an LLM can select an outcome but
# cannot invent an observation path or verifier implementation.
PHASE_TARGET_FIELDS: dict[str, frozenset[str]] = {
    "max_health_at_least": frozenset({"id", "type", "value"}),
    "current_health_at_least": frozenset({"id", "type", "value"}),
    "vigor_at_least": frozenset({"id", "type", "value"}),
    "carried_currency_at_least": frozenset({"id", "type", "amount"}),
    "inventory_items_at_most": frozenset({"id", "type", "count"}),
    "inventory_room_for_at_least": frozenset(
        {"id", "type", "dimension", "value"}
    ),
    "item_count_at_least": frozenset({"id", "type", "item", "count"}),
    "inventory_not_full": frozenset({"id", "type"}),
    "location_reached": frozenset({"id", "type", "room_id", "name"}),
    "equipment_known": frozenset({"id", "type"}),
    "wielding_equals": frozenset({"id", "type", "items"}),
    "ability_at_least": frozenset(
        {"id", "type", "ability_kind", "name", "value"}
    ),
    PHASE_ACTION_SUCCEEDED: frozenset({"id", "type", "tools"}),
}

PHASE_NUMERIC_METRICS = frozenset(
    {
        "max_health",
        "current_health",
        "vigor",
        "carried_currency",
        "status.vitals.health.max",
        "status.vitals.health.value",
        "status.vitals.health.current",
        "look.vitals.health.max",
        "look.vitals.health.value",
        "look.vitals.health.current",
        "status.vitals.vigor.value",
        "status.vitals.vigor.current",
        "look.vitals.vigor.value",
        "look.vitals.vigor.current",
        "inventory.carry.items",
        "inventory.carry.room_for.weight",
        "inventory.carry.room_for.bulk",
    }
)

PHASE_STATE_PATHS = frozenset(
    {
        "inventory.full",
        "inventory.items",
        "equipment.known",
        "equipment.wielding",
        "status.position.col",
        "status.position.row",
    }
)


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
                            "kind": PHASE_ACTION_SUCCEEDED,
                            "tools": ["hunting_grounds"],
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

    @staticmethod
    def _target_number(target: dict[str, Any], field: str) -> int | float:
        value = target.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(
                f"phase target {target.get('type')!r}.{field} must be numeric"
            )
        return value

    @classmethod
    def compile_phase_targets(
        cls, phase_kind: str, targets: Any
    ) -> list[dict[str, Any]]:
        """Compile model-selected semantic outcomes into trusted verifiers."""

        if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
            raise ValueError("campaign phase targets must contain 1-20 objects")
        compiled: list[dict[str, Any]] = []
        ids: set[str] = set()
        for index, target in enumerate(targets):
            if not isinstance(target, dict):
                raise ValueError(f"campaign phase target {index + 1} must be an object")
            target_type = str(target.get("type") or "").strip()
            allowed_fields = PHASE_TARGET_FIELDS.get(target_type)
            if allowed_fields is None:
                raise ValueError(
                    f"unsupported campaign phase target type {target_type!r}; supported: "
                    + ", ".join(sorted(PHASE_TARGET_FIELDS))
                )
            unknown = set(target) - allowed_fields
            if unknown:
                raise ValueError(
                    f"phase target {target_type!r} contains unknown field(s): "
                    + ", ".join(sorted(unknown))
                )
            target_id = str(target.get("id") or f"target_{index + 1}").strip()
            if not target_id or target_id in ids:
                raise ValueError(f"duplicate or empty phase target id: {target_id!r}")
            ids.add(target_id)

            if target_type == "max_health_at_least":
                criterion = {
                    "id": target_id,
                    "kind": "numeric_threshold",
                    "metric": "max_health",
                    "operator": ">=",
                    "value": cls._target_number(target, "value"),
                }
            elif target_type == "current_health_at_least":
                criterion = {
                    "id": target_id,
                    "kind": "numeric_threshold",
                    "metric": "current_health",
                    "operator": ">=",
                    "value": cls._target_number(target, "value"),
                }
            elif target_type == "vigor_at_least":
                criterion = {
                    "id": target_id,
                    "kind": "numeric_threshold",
                    "metric": "vigor",
                    "operator": ">=",
                    "value": cls._target_number(target, "value"),
                }
            elif target_type == "carried_currency_at_least":
                criterion = {
                    "id": target_id,
                    "kind": "numeric_threshold",
                    "metric": "carried_currency",
                    "operator": ">=",
                    "value": cls._target_number(target, "amount"),
                }
            elif target_type == "inventory_items_at_most":
                count = cls._target_number(target, "count")
                if count < 0:
                    raise ValueError("inventory_items_at_most.count must be non-negative")
                criterion = {
                    "id": target_id,
                    "kind": "numeric_threshold",
                    "metric": "inventory.carry.items",
                    "operator": "<=",
                    "value": count,
                }
            elif target_type == "inventory_room_for_at_least":
                dimension = str(target.get("dimension") or "").strip()
                if dimension not in {"weight", "bulk"}:
                    raise ValueError(
                        "inventory_room_for_at_least.dimension must be weight or bulk"
                    )
                criterion = {
                    "id": target_id,
                    "kind": "numeric_threshold",
                    "metric": f"inventory.carry.room_for.{dimension}",
                    "operator": ">=",
                    "value": cls._target_number(target, "value"),
                }
            elif target_type == "item_count_at_least":
                item = " ".join(str(target.get("item") or "").split())
                count = target.get("count", 1)
                if (
                    not item
                    or not isinstance(count, int)
                    or isinstance(count, bool)
                    or count < 1
                ):
                    raise ValueError(
                        "item_count_at_least requires a non-empty item and positive integer count"
                    )
                criterion = {
                    "id": target_id,
                    "kind": "inventory_contains",
                    "item": item,
                    "count": count,
                }
            elif target_type == "inventory_not_full":
                criterion = {
                    "id": target_id,
                    "kind": "state_equals",
                    "path": "inventory.full",
                    "value": False,
                }
            elif target_type == "location_reached":
                room_id = target.get("room_id")
                name = " ".join(str(target.get("name") or "").split())
                if room_id is None and not name:
                    raise ValueError("location_reached requires room_id or name")
                if room_id is not None and (
                    not isinstance(room_id, int)
                    or isinstance(room_id, bool)
                    or room_id < 1
                ):
                    raise ValueError("location_reached.room_id must be a positive integer")
                criterion = {
                    "id": target_id,
                    "kind": "location_reached",
                    **({"room_id": room_id} if room_id is not None else {}),
                    **({"location": name} if name else {}),
                }
            elif target_type == "equipment_known":
                criterion = {
                    "id": target_id,
                    "kind": "state_equals",
                    "path": "equipment.known",
                    "value": True,
                }
            elif target_type == "wielding_equals":
                items = target.get("items")
                if items is not None and (
                    not isinstance(items, list)
                    or any(not isinstance(item, str) or not item.strip() for item in items)
                ):
                    raise ValueError(
                        "wielding_equals.items must be null or an array of canonical names"
                    )
                criterion = {
                    "id": target_id,
                    "kind": "state_equals",
                    "path": "equipment.wielding",
                    "value": items,
                }
            elif target_type == "ability_at_least":
                ability_kind = str(target.get("ability_kind") or "").casefold()
                name = " ".join(str(target.get("name") or "").split())
                if ability_kind not in {"skill", "spell"} or not name:
                    raise ValueError(
                        "ability_at_least requires ability_kind=skill|spell and a name"
                    )
                criterion = {
                    "id": target_id,
                    "kind": "numeric_threshold",
                    "metric": f"ability.{ability_kind}.{name}",
                    "operator": ">=",
                    "value": cls._target_number(target, "value"),
                }
            else:
                tools = target.get("tools")
                if (
                    not isinstance(tools, list)
                    or not tools
                    or any(not isinstance(tool, str) or not tool.strip() for tool in tools)
                ):
                    raise ValueError(
                        "phase_action_succeeded.tools must be a non-empty array of tool names"
                    )
                criterion = {
                    "id": target_id,
                    "kind": PHASE_ACTION_SUCCEEDED,
                    "tools": tools,
                }
            compiled.append(criterion)
        return compiled

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
            raw_targets = phase.get("targets")
            if raw_targets is not None:
                if phase.get("success_criteria") is not None:
                    raise ValueError(
                        "campaign phase must use targets or legacy success_criteria, not both"
                    )
                criteria = self.compile_phase_targets(kind, raw_targets)
            else:
                criteria = phase.get("success_criteria")
                if not isinstance(criteria, list) or not criteria:
                    raise ValueError(
                        "campaign phase requires at least one typed target"
                    )
            if any(
                isinstance(criterion, dict)
                and criterion.get("kind") == "operator_confirmed"
                for criterion in criteria
            ):
                raise ValueError(
                    "internal campaign phases cannot require operator_confirmed; "
                    "use observable state or phase_action_succeeded evidence"
                )
            criteria = self._normalize_phase_success_criteria(
                criteria, phase_kind=kind, migrate_legacy=True
            )
            self._validate_phase_success_criteria(kind, criteria, goal, phase)
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
            if raw_targets is not None:
                phase_context["phase_targets"] = redact(raw_targets)
            if ignored_abandon_predicates:
                phase_context["ignored_invalid_abandon_predicates"] = (
                    ignored_abandon_predicates
                )
            if raw_targets is None and criteria != phase.get("success_criteria"):
                phase_context["normalized_success_criteria"] = [
                    "legacy success criteria converted to controller-owned canonical verifiers"
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
            phase.pop("targets", None)
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
        *,
        phase_kind: str | None = None,
        migrate_legacy: bool = False,
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
            if (
                migrate_legacy
                and isinstance(value, dict)
            ):
                if (
                    phase_kind == "research_progression"
                    and value.get("kind") == "operator_confirmed"
                ):
                    value = {
                        "id": value.get("id"),
                        "kind": PHASE_ACTION_SUCCEEDED,
                        "tools": ["hunting_grounds"],
                    }
                elif (
                    value.get("kind") in {"numeric_threshold", "numeric_delta"}
                    and str(value.get("metric") or "").casefold()
                    in {
                        "inventory.items.shilling.amount",
                        "inventory.items.shillings.amount",
                        "inventory.items.shilling.quantity",
                        "inventory.items.shillings.quantity",
                    }
                ):
                    value["metric"] = "carried_currency"
            normalized.append(value)
        return normalized

    def _validate_phase_success_criteria(
        self,
        phase_kind: str,
        criteria: list[dict[str, Any]],
        goal: dict[str, Any],
        phase: dict[str, Any],
    ) -> None:
        public_criteria: list[dict[str, Any]] = []
        ids: set[str] = set()
        has_internal = False
        for index, criterion in enumerate(criteria):
            if not isinstance(criterion, dict):
                raise ValueError("campaign phase success criteria must be objects")
            criterion_id = str(criterion.get("id") or f"criterion_{index + 1}")
            if criterion_id in ids:
                raise ValueError(f"duplicate criterion id: {criterion_id}")
            ids.add(criterion_id)
            if criterion.get("kind") != PHASE_ACTION_SUCCEEDED:
                self._validate_public_phase_criterion(criterion)
                public_criteria.append(criterion)
                continue
            has_internal = True
            unknown = set(criterion) - PHASE_ACTION_CRITERION_FIELDS
            if unknown:
                raise ValueError(
                    "unknown phase_action_succeeded criterion field(s): "
                    + ", ".join(sorted(unknown))
                )
            tools = criterion.get("tools")
            if (
                not isinstance(tools, list)
                or not tools
                or any(not isinstance(tool, str) or not tool.strip() for tool in tools)
            ):
                raise ValueError(
                    "phase_action_succeeded.tools must be a non-empty array of tool names"
                )
            unavailable = sorted(set(tools) - PHASE_TOOL_NAMES.get(phase_kind, set()))
            if unavailable:
                raise ValueError(
                    "phase_action_succeeded names tools unavailable to this phase: "
                    + ", ".join(unavailable)
                )
        if has_internal and any(
            criterion.get("kind") in {"composite_all", "composite_any"}
            for criterion in public_criteria
        ):
            raise ValueError(
                "phase_action_succeeded cannot be referenced by composite criteria"
            )
        if phase_kind == "farm" and has_internal and not public_criteria:
            raise ValueError(
                "farm phase cannot complete merely because an action launched; "
                "require an observable farming outcome such as a max-health milestone"
            )
        if public_criteria:
            # Reuse the public criterion validator without creating a public goal.
            self.storage._validate_goal(
                {
                    "title": str(phase.get("objective") or "Internal campaign phase")[:120],
                    "objective": str(phase.get("objective") or "Internal campaign phase"),
                    "success_criteria": public_criteria,
                    "constraints": {},
                    "priority": int(goal.get("priority", 50)),
                    "activation": "queue",
                }
            )

    @staticmethod
    def _validate_public_phase_criterion(criterion: dict[str, Any]) -> None:
        """Reject semantically invalid observation paths before persistence."""

        kind = str(criterion.get("kind") or "")
        if kind in {"numeric_threshold", "numeric_delta"}:
            metric = str(criterion.get("metric") or "")
            if metric not in PHASE_NUMERIC_METRICS and parse_ability_metric(metric) is None:
                raise ValueError(
                    f"unsupported internal phase numeric metric {metric!r}; "
                    "use a typed target instead of inventing an observation path"
                )
            return
        if kind == "state_equals":
            path = str(criterion.get("path") or "")
            if path not in PHASE_STATE_PATHS:
                raise ValueError(
                    f"unsupported internal phase state path {path!r}; "
                    "use a typed target instead of inventing an observation path"
                )
            return
        if kind in {
            "inventory_contains",
            "location_reached",
            "event_occurred",
            "composite_all",
            "composite_any",
        }:
            return
        raise ValueError(
            f"unsupported internal phase criterion kind {kind!r}; use a typed target"
        )

    def _evaluate_phase_success_criteria(
        self,
        goal: dict[str, Any],
        phase: dict[str, Any],
        criteria: list[dict[str, Any]],
        observation: dict[str, Any],
    ) -> dict[str, Any]:
        annotated = [
            {
                **criterion,
                "id": str(criterion.get("id") or f"criterion_{index + 1}"),
            }
            for index, criterion in enumerate(criteria)
            if isinstance(criterion, dict)
        ]
        public = [
            criterion
            for criterion in annotated
            if criterion.get("kind") != PHASE_ACTION_SUCCEEDED
        ]
        public_results: dict[str, dict[str, Any]] = {}
        if public:
            evaluated = self.criteria.evaluate(
                {"id": goal["id"], "success_criteria": public}, observation
            )
            public_results = {
                str(result.get("id") or ""): result
                for result in evaluated.get("criteria", [])
                if isinstance(result, dict)
            }

        attempts = self.storage.phase_attempts(phase["id"], limit=200)
        successful = [attempt for attempt in attempts if attempt.get("status") == "succeeded"]
        results: list[dict[str, Any]] = []
        evidence_event_ids: list[str] = []
        for criterion in annotated:
            criterion_id = str(criterion["id"])
            if criterion.get("kind") != PHASE_ACTION_SUCCEEDED:
                result = public_results.get(criterion_id)
                if result is not None:
                    results.append(result)
                continue
            allowed_tools = {str(tool) for tool in criterion.get("tools", [])}
            matches = [
                attempt
                for attempt in successful
                if str(attempt.get("semantic_action") or "") in allowed_tools
            ]
            met = bool(matches)
            if matches:
                evidence_event_ids.extend(
                    str(attempt.get("id"))
                    for attempt in matches
                    if attempt.get("id") is not None
                )
            results.append(
                {
                    "id": criterion_id,
                    "kind": PHASE_ACTION_SUCCEEDED,
                    "met": met,
                    "detail": (
                        "verified successful phase action(s): "
                        + ", ".join(
                            sorted(
                                {
                                    str(attempt.get("semantic_action") or "")
                                    for attempt in matches
                                }
                            )
                        )
                        if met
                        else "awaiting a successful phase action from: "
                        + ", ".join(sorted(allowed_tools))
                    ),
                }
            )
        met_count = sum(result.get("met") is True for result in results)
        return {
            "percent_estimate": round(100 * met_count / len(results)) if results else 0,
            "summary": f"{met_count} of {len(results)} criteria verified",
            "evidence_event_ids": evidence_event_ids,
            "criteria": results,
            "all_met": bool(results) and met_count == len(results),
        }

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
        *,
        allow_completion: bool = True,
    ) -> PhaseOutcome:
        if phase is None:
            return PhaseOutcome(False, False, None, {"reason": "no_active_phase"})
        raw_criteria = phase.get("success_criteria", [])
        criteria = self._normalize_phase_success_criteria(
            raw_criteria,
            phase_kind=str(phase.get("kind") or ""),
            migrate_legacy=True,
        )
        abandon = phase.get("abandon_predicates", [])
        context = (
            dict(phase.get("context"))
            if isinstance(phase.get("context"), dict)
            else {}
        )
        normalization_reasons: list[str] = []
        if criteria != raw_criteria:
            context["normalized_success_criteria"] = [
                "converted legacy phase criteria to deterministic controller representations"
            ]
            normalization_reasons.append("normalized internal phase success criteria")
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
        completion = self._evaluate_phase_success_criteria(
            goal, phase, criteria, observation
        )
        if completion.get("all_met") is True:
            if not allow_completion:
                return PhaseOutcome(
                    False,
                    False,
                    phase,
                    {**completion, "completion_deferred": True},
                )
            return self.complete_phase(run, phase, completion)
        return PhaseOutcome(False, False, phase, completion)

    def complete_phase(
        self,
        run: dict[str, Any],
        phase: dict[str, Any],
        completion: dict[str, Any],
    ) -> PhaseOutcome:
        """Commit a previously verified phase outcome after completion hygiene."""

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
