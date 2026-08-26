from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .contracts import parse_ability_metric
from .criteria import CriteriaEvaluator
from .storage import CAMPAIGN_PHASE_DOWNTIME_RUNTIME_KEY, Storage
from .utils import canonical_json, deep_get, json_hash, redact, timestamp


CAMPAIGN_PHASE_PROGRESS_LEASE_RUNTIME_KEY = "campaign_phase_progress_lease_v1"

# These fields are written only after controller-owned validation or live keeper
# evidence. A campaign-manager response may propose tactics, but it cannot mint
# durable evidence that later outranks operator intent or recipe selection.
CONTROLLER_OWNED_TACTIC_CONTEXT_FIELDS = frozenset(
    {
        "deterministic_research_handoff",
        "farm_recipe",
        "initial_observable_outcome_unmet",
        "positioning_preference",
        "positioning_preference_repair",
        "recipe_validation",
        "research_attempt_id",
        "research_fingerprint",
        "research_phase_id",
        "safe_spot_fallback",
        "selection_basis",
    }
)


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
        "merchants",
        "shop",
        "sell",
        "sell_all",
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
        # Combat training needs the source spawn index to choose prey and a
        # real room. `map.search` only matches room names and cannot establish
        # that a creature lives there.
        "prey",
        "hunting_grounds",
        "inventory",
        "merchants",
        "map",
        "travel",
        # Paid training shares the guarded purchase-route recovery path.
        "go_through",
        "shop",
        "bank",
        "cast",
        # Sustained combat must be owned by the fast, health-aware keeper. A
        # foreground fight is only one swing and leaves the character exposed
        # while the next model turn is being inferred.
        "autopilot",
        "rest_up",
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
        # Provisioning is part of sustaining an already-selected farm, not a
        # phase change.  `shop` without the read-only catalogue forces the
        # tactical model to guess a seller/room or revise the plan through
        # weaker map and free-text knowledge searches.  MANIAC did exactly
        # that for four full model turns after Create Food reported one missing
        # elderberry.  Keep the lookup and the purchase in the same bounded
        # farm phase; ordinary live-quote and transaction safeguards still
        # apply before money moves.
        "merchants",
        "equip_best",
        "wear_best",
        "rest_up",
        # Farm preparation may need to create a weapon or food in safe
        # staging. Expose the typed spell adapter so the planner can use the
        # capability described in grounded development context without
        # falling back to the overly broad `act` tool.
        "cast",
        "shop",
        "bank",
        "map",
        "travel",
        "autopilot",
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
PHASE_KEEPER_TARGET_KILLS = "phase_keeper_target_kills"
PHASE_KEEPER_TARGET_KILL_FIELDS = frozenset({"id", "kind", "count"})
MAX_PHASE_KEEPER_TARGET_KILLS = 25
RESEARCH_RETRY_UNLOCKED = "research_retry_unlocked"
RESEARCH_RETRY_CRITERION_FIELDS = frozenset({"id", "kind"})

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
    "equipment_count_at_least": frozenset(
        {"id", "type", "category", "count"}
    ),
    "inventory_not_full": frozenset({"id", "type"}),
    "location_reached": frozenset({"id", "type", "room_id", "name"}),
    "equipment_known": frozenset({"id", "type"}),
    "wielding_equals": frozenset({"id", "type", "items"}),
    "wielding_contains": frozenset({"id", "type", "item", "category"}),
    "ability_at_least": frozenset(
        {"id", "type", "ability_kind", "name", "value"}
    ),
    "keeper_target_kills_at_least": frozenset({"id", "type", "count"}),
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
                equipment_category = {
                    "weapon": "weapon",
                    "weapons": "weapon",
                    "armor": "armor",
                    "armour": "armor",
                }.get(item.casefold())
                criterion = (
                    {
                        "id": target_id,
                        "kind": "equipment_count",
                        "category": equipment_category,
                        "count": count,
                    }
                    if equipment_category is not None
                    else {
                        "id": target_id,
                        "kind": "inventory_contains",
                        "item": item,
                        "count": count,
                    }
                )
            elif target_type == "equipment_count_at_least":
                category = str(target.get("category") or "").strip().casefold()
                count = target.get("count")
                if (
                    category not in {"weapon", "armor"}
                    or not isinstance(count, int)
                    or isinstance(count, bool)
                    or count < 1
                ):
                    raise ValueError(
                        "equipment_count_at_least requires category=weapon|armor and a positive integer count"
                    )
                criterion = {
                    "id": target_id,
                    "kind": "equipment_count",
                    "category": category,
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
                if (
                    isinstance(items, list)
                    and len(items) == 1
                    and " ".join(items[0].split()).casefold() in {"weapon", "weapons"}
                ):
                    item = " ".join(items[0].split())
                    category = {
                        "weapon": "weapon",
                        "weapons": "weapon",
                    }.get(item.casefold())
                    criterion = {
                        "id": target_id,
                        "kind": "equipment_wielding",
                        **(
                            {"category": category}
                            if category is not None
                            else {"item": item}
                        ),
                    }
                else:
                    criterion = {
                        "id": target_id,
                        "kind": "state_equals",
                        "path": "equipment.wielding",
                        "value": items,
                    }
            elif target_type == "wielding_contains":
                item = " ".join(str(target.get("item") or "").split())
                category = str(target.get("category") or "").strip().casefold()
                has_item = bool(item)
                has_category = category == "weapon"
                if has_item == has_category:
                    raise ValueError(
                        "wielding_contains requires exactly one canonical item or category=weapon"
                    )
                criterion = {
                    "id": target_id,
                    "kind": "equipment_wielding",
                    **({"item": item} if has_item else {"category": category}),
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
            elif target_type == "keeper_target_kills_at_least":
                count = target.get("count")
                if (
                    not isinstance(count, int)
                    or isinstance(count, bool)
                    or count < 1
                    or count > MAX_PHASE_KEEPER_TARGET_KILLS
                ):
                    raise ValueError(
                        "keeper_target_kills_at_least.count must be an integer from "
                        f"1 through {MAX_PHASE_KEEPER_TARGET_KILLS}"
                    )
                criterion = {
                    "id": target_id,
                    "kind": PHASE_KEEPER_TARGET_KILLS,
                    "count": count,
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

    def validate_manager_decision(
        self,
        run: dict[str, Any],
        goal: dict[str, Any],
        decision: dict[str, Any],
        observation: dict[str, Any] | None = None,
    ) -> None:
        """Validate manager output without mutating campaign state."""

        action = str(decision.get("decision") or "").strip()
        if action in {"start_phase", "replace_phase", "push_support_phase"}:
            phase = decision.get("phase")
            if not isinstance(phase, dict):
                raise ValueError(f"{action} requires a phase object")
            self._validated_manager_phase(goal, phase, observation)
            return
        if action in {
            "resume_parent_phase",
            "complete_campaign_candidate",
            "report_external_blocker_candidate",
        }:
            return
        raise ValueError(
            "campaign manager decision must be start_phase, replace_phase, push_support_phase, "
            "resume_parent_phase, complete_campaign_candidate, or report_external_blocker_candidate"
        )

    def _validated_manager_phase(
        self,
        goal: dict[str, Any],
        phase: dict[str, Any],
        observation: dict[str, Any] | None,
        *,
        controller_owned_context: bool = False,
    ) -> dict[str, Any]:
        """Compile and normalize one manager-selected phase without persisting it."""

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
                raise ValueError("campaign phase requires at least one typed target")
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
        initial_observable_outcome: dict[str, Any] | None = None
        if observation is not None:
            self._validate_material_equipment_targets(kind, criteria, observation)
            initial_observable_outcome = self.observable_success_evaluation(
                goal, criteria, observation
            )
            if (
                initial_observable_outcome is not None
                and initial_observable_outcome.get("evidence_complete") is not True
            ):
                raise ValueError(
                    "campaign phase observable outcome cannot be verified from the "
                    "current observation; refresh the required live state before "
                    "selecting this milestone"
                )
            if (
                initial_observable_outcome is not None
                and initial_observable_outcome.get("all_met") is True
            ):
                details = "; ".join(
                    str(result.get("detail") or "")
                    for result in initial_observable_outcome.get("criteria", [])[:3]
                    if isinstance(result, dict) and result.get("detail")
                )
                raise ValueError(
                    "campaign phase observable outcome is already true before any "
                    "phase action"
                    + (f": {details}" if details else "")
                    + "; select an unmet observable improvement instead of a no-op milestone"
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
        if not controller_owned_context:
            ignored_controller_fields = sorted(
                field
                for field in CONTROLLER_OWNED_TACTIC_CONTEXT_FIELDS
                if field in phase_context
            )
            for field in ignored_controller_fields:
                phase_context.pop(field, None)
            if ignored_controller_fields:
                phase_context["ignored_controller_owned_tactic_context"] = (
                    ignored_controller_fields
                )
        has_phase_local_kill_target = any(
            isinstance(criterion, dict)
            and criterion.get("kind") == PHASE_KEEPER_TARGET_KILLS
            for criterion in criteria
        )
        if initial_observable_outcome is not None or has_phase_local_kill_target:
            # This controller-owned receipt distinguishes a newly validated
            # phase whose outcome was genuinely unmet from markerless phases
            # persisted by older builds.  It lets resume-time migration retire
            # the latter without misclassifying later progress on a valid phase.
            # A new phase-local kill counter is definitionally zero here and is
            # therefore also a verified unmet outcome.
            phase_context["initial_observable_outcome_unmet"] = True
        unverified_preferences: dict[str, Any] = {}
        for field in ("avoid_rooms", "avoid_targets"):
            proposed = phase_context.pop(field, None)
            if proposed not in (None, []):
                unverified_preferences[field] = redact(proposed)
        if unverified_preferences:
            # An LLM phase is allowed to choose work, not to manufacture durable
            # negative evidence. Exact route failures, farm quarantines, and
            # empirical room outcomes are controller-owned elsewhere. Persist
            # this only as an audit note; compact manager context deliberately
            # does not replay it into the next decision.
            phase_context["ignored_unverified_tactic_preferences"] = (
                unverified_preferences
            )
        if raw_targets is not None:
            # Replay only the compiled contract. Keeping a repaired raw target
            # such as item="weapon" in planner context can resurrect the exact
            # invalid literal interpretation the compiler just removed.
            phase_context["phase_targets"] = redact(criteria)
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
        prepared = {
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
        prepared.pop("targets", None)
        return prepared

    def apply_manager_decision(
        self,
        run: dict[str, Any],
        goal: dict[str, Any],
        decision: dict[str, Any],
        observation: dict[str, Any] | None = None,
        *,
        controller_owned_context: bool = False,
    ) -> dict[str, Any] | None:
        action = str(decision.get("decision") or "").strip()
        if action in {"start_phase", "replace_phase", "push_support_phase"}:
            phase = decision.get("phase")
            if not isinstance(phase, dict):
                raise ValueError(f"{action} requires a phase object")
            phase = self._validated_manager_phase(
                goal,
                phase,
                observation,
                controller_owned_context=controller_owned_context,
            )
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
            ):
                expected = value.get("value")
                if isinstance(expected, str):
                    expected = [expected]
                if (
                    isinstance(expected, list)
                    and len(expected) == 1
                    and " ".join(str(expected[0] or "").split()).casefold()
                    in {"weapon", "weapons"}
                ):
                    item = " ".join(str(expected[0] or "").split())
                    category = (
                        "weapon" if item.casefold() in {"weapon", "weapons"} else None
                    )
                    value = {
                        "id": value.get("id"),
                        "kind": "equipment_wielding",
                        **(
                            {"category": category}
                            if category is not None
                            else {"item": item}
                        ),
                    }
                elif isinstance(expected, list) and isinstance(value, dict):
                    value["value"] = expected
            if (
                isinstance(value, dict)
                and value.get("kind") == "inventory_contains"
                and " ".join(str(value.get("item") or "").split()).casefold()
                in {"weapon", "weapons", "armor", "armour"}
            ):
                raw_item = " ".join(str(value.get("item") or "").split()).casefold()
                value = {
                    "id": value.get("id"),
                    "kind": "equipment_count",
                    "category": "weapon" if raw_item.startswith("weapon") else "armor",
                    "count": value.get("count", 1),
                }
            if (
                isinstance(value, dict)
                and value.get("kind") == "inventory_contains"
                and " ".join(str(value.get("item") or "").split()).casefold()
                in {"gear", "equipment"}
            ):
                raise ValueError(
                    "inventory_contains cannot verify a generic gear/equipment category; "
                    "use equipment_count_at_least with category=weapon or armor"
                )
            if (
                isinstance(value, dict)
                and phase_kind == "prepare_combat"
                and value.get("kind") == "inventory_contains"
                and " ".join(str(value.get("item") or "").split()).casefold()
                in {"food", "edible food", "snack", "snacks"}
            ):
                # Create Food materializes concrete edible items (at MANIAC's
                # current ability, apples); there is no literal inventory item
                # named Snack.  Keep the phase's semantic intent while giving
                # the deterministic evaluator a canonical category.
                value["item"] = "food"
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
                    phase_kind == "research_progression"
                    and value.get("kind") == PHASE_ACTION_SUCCEEDED
                    and isinstance(value.get("tools"), list)
                    and "hunting_grounds" in value["tools"]
                ):
                    # Only hunting_grounds returns the typed room/prey payload
                    # consumed by the deterministic farm-recipe handoff. A
                    # broad "any research tool succeeded" target can complete
                    # on prey or prose knowledge while yielding no executable
                    # recipe, so narrow mixed legacy/model output here.
                    value["tools"] = ["hunting_grounds"]
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

    @staticmethod
    def _validate_material_equipment_targets(
        phase_kind: str,
        criteria: list[dict[str, Any]],
        observation: dict[str, Any],
    ) -> None:
        """Reject support milestones that are already true when proposed."""

        if phase_kind != "prepare_combat":
            return
        for criterion in criteria:
            if not isinstance(criterion, dict):
                continue
            kind = criterion.get("kind")
            if kind == "equipment_count":
                category = str(criterion.get("category") or "")
                required = int(criterion.get("count", 1) or 1)
                current = CriteriaEvaluator.equipment_count(observation, category)
                if required <= current:
                    raise ValueError(
                        f"prepare_combat equipment target is already true: verified {category} "
                        f"count is {current}, so the target must exceed {current} or select a "
                        "different observable improvement; do not create gear merely to reopen research"
                    )
            elif kind == "equipment_wielding" and CriteriaEvaluator.equipment_wielding(
                observation,
                item=criterion.get("item"),
                category=criterion.get("category"),
            ):
                target = criterion.get("item") or criterion.get("category")
                raise ValueError(
                    f"prepare_combat wielding target {target!r} is already verified; select a "
                    "different concrete item or another observable capability improvement"
                )

    def _validate_phase_success_criteria(
        self,
        phase_kind: str,
        criteria: list[dict[str, Any]],
        goal: dict[str, Any],
        phase: dict[str, Any],
    ) -> None:
        public_criteria: list[dict[str, Any]] = []
        ids: set[str] = set()
        has_action_receipt = False
        has_keeper_target_kills = False
        for index, criterion in enumerate(criteria):
            if not isinstance(criterion, dict):
                raise ValueError("campaign phase success criteria must be objects")
            criterion_id = str(criterion.get("id") or f"criterion_{index + 1}")
            if criterion_id in ids:
                raise ValueError(f"duplicate criterion id: {criterion_id}")
            ids.add(criterion_id)
            if criterion.get("kind") == RESEARCH_RETRY_UNLOCKED:
                unknown = set(criterion) - RESEARCH_RETRY_CRITERION_FIELDS
                if unknown:
                    raise ValueError(
                        "unknown research_retry_unlocked criterion field(s): "
                        + ", ".join(sorted(unknown))
                    )
                if phase_kind != "prepare_combat":
                    raise ValueError(
                        "research_retry_unlocked is reserved for controller-owned "
                        "prepare_combat recovery phases"
                    )
                continue
            if criterion.get("kind") == PHASE_KEEPER_TARGET_KILLS:
                has_keeper_target_kills = True
                unknown = set(criterion) - PHASE_KEEPER_TARGET_KILL_FIELDS
                if unknown:
                    raise ValueError(
                        "unknown phase_keeper_target_kills criterion field(s): "
                        + ", ".join(sorted(unknown))
                    )
                count = criterion.get("count")
                if (
                    phase_kind != "farm"
                    or not isinstance(count, int)
                    or isinstance(count, bool)
                    or count < 1
                    or count > MAX_PHASE_KEEPER_TARGET_KILLS
                ):
                    raise ValueError(
                        "phase_keeper_target_kills is reserved for farm phases "
                        "and requires an integer count from 1 through "
                        f"{MAX_PHASE_KEEPER_TARGET_KILLS}"
                    )
                continue
            if criterion.get("kind") != PHASE_ACTION_SUCCEEDED:
                self._validate_public_phase_criterion(criterion)
                public_criteria.append(criterion)
                continue
            has_action_receipt = True
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
        if has_action_receipt and any(
            criterion.get("kind") in {"composite_all", "composite_any"}
            for criterion in public_criteria
        ):
            raise ValueError(
                "phase_action_succeeded cannot be referenced by composite criteria"
            )
        if (
            phase_kind == "farm"
            and has_action_receipt
            and not public_criteria
            and not has_keeper_target_kills
        ):
            raise ValueError(
                "farm phase cannot complete merely because an action launched; "
                "require max-health progress or a phase-local verified target-kill milestone"
            )
        invalid_farm_outcomes = self.invalid_farm_outcome_criteria(criteria)
        if phase_kind == "farm" and invalid_farm_outcomes:
            kinds = ", ".join(
                sorted(
                    {
                        str(criterion.get("kind") or "unknown")
                        for criterion in invalid_farm_outcomes
                    }
                )
            )
            raise ValueError(
                "farm phase outcome must be forward max-health progress or a "
                "phase-local verified target-kill count caused by the configured "
                "keeper hunt; unsupported farm outcome kind(s): "
                f"{kinds}. Inventory, food, equipment, currency, location, and "
                "recovery targets belong to preparation or support phases and "
                "cannot define farm completion"
            )
        preparation_mutations = {
            tool
            for criterion in criteria
            if isinstance(criterion, dict)
            and criterion.get("kind") == PHASE_ACTION_SUCCEEDED
            for tool in criterion.get("tools", [])
            if tool in {"act", "cast", "sell", "sell_all", "shop"}
        }
        if (
            phase_kind == "prepare_combat"
            and preparation_mutations
            and not public_criteria
        ):
            raise ValueError(
                "prepare_combat cannot use mutating action success alone for "
                + ", ".join(sorted(preparation_mutations))
                + "; require an observable target such as item_count_at_least, "
                "equipment_count_at_least, wielding_contains, equipment_known, or inventory_not_full"
            )
        max_health_goal = any(
            isinstance(criterion, dict)
            and criterion.get("kind") in {"numeric_threshold", "numeric_delta"}
            and str(criterion.get("metric") or "")
            in {"max_health", "status.vitals.health.max", "look.vitals.health.max"}
            for criterion in goal.get("success_criteria", [])
        )
        if phase_kind == "research_progression" and max_health_goal:
            invalid_research_tools = [
                criterion.get("tools")
                for criterion in criteria
                if isinstance(criterion, dict)
                and criterion.get("kind") == PHASE_ACTION_SUCCEEDED
                and criterion.get("tools") != ["hunting_grounds"]
            ]
            if invalid_research_tools:
                raise ValueError(
                    "max-health progression research must require exactly "
                    "phase_action_succeeded.tools=['hunting_grounds']; prey and "
                    "knowledge_search do not return the typed farm recipe needed "
                    "for deterministic handoff"
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
    def invalid_farm_outcome_criteria(
        criteria: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return public outcomes that cannot prove keeper farming work.

        An inventory target can be satisfied by casting, shopping, or pickup
        before the keeper ever fights, so treating it as farm completion
        recreates a skip-work checkpoint under a different observable. Farm
        completion is limited to forward max-health progress or the controller's
        phase-local exact-target kill receipt.
        """

        max_health_metrics = {
            "max_health",
            "status.vitals.health.max",
            "look.vitals.health.max",
        }

        def valid_max_health_progress(criterion: dict[str, Any]) -> bool:
            value = criterion.get("value")
            return bool(
                criterion.get("kind") == "numeric_threshold"
                and str(criterion.get("metric") or "") in max_health_metrics
                and str(criterion.get("operator") or ">=") in {">", ">="}
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            )

        return [
            criterion
            for criterion in criteria
            if isinstance(criterion, dict)
            and criterion.get("kind")
            not in {
                PHASE_ACTION_SUCCEEDED,
                PHASE_KEEPER_TARGET_KILLS,
                RESEARCH_RETRY_UNLOCKED,
            }
            and not valid_max_health_progress(criterion)
        ]

    def observable_success_evaluation(
        self,
        goal: dict[str, Any],
        criteria: list[dict[str, Any]],
        observation: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Evaluate only the phase outcomes that live state can verify.

        Internal action receipts are deliberately excluded.  If every public
        outcome is already true, an action receipt would be the phase's only
        remaining gate and could turn a no-op launch into apparent progress.
        """

        public = [
            {
                **criterion,
                "id": str(criterion.get("id") or f"criterion_{index + 1}"),
            }
            for index, criterion in enumerate(criteria)
            if isinstance(criterion, dict)
            and criterion.get("kind")
            not in {
                PHASE_ACTION_SUCCEEDED,
                PHASE_KEEPER_TARGET_KILLS,
                RESEARCH_RETRY_UNLOCKED,
            }
        ]
        if not public:
            return None
        evaluation = self.criteria.evaluate(
            {"id": str(goal.get("id") or "campaign-phase"), "success_criteria": public},
            observation,
        )
        results = {
            str(result.get("id") or ""): result
            for result in evaluation.get("criteria", [])
            if isinstance(result, dict)
        }
        sentinel = object()

        def known(criterion: dict[str, Any]) -> bool:
            kind = str(criterion.get("kind") or "")
            result = results.get(str(criterion.get("id") or ""), {})
            if kind in {"composite_all", "composite_any", "event_occurred"}:
                return True
            if kind in {"numeric_threshold", "numeric_delta"}:
                metric = str(criterion.get("metric") or "")
                if metric == "carried_currency" and not isinstance(
                    deep_get(observation, "inventory.items", sentinel), list
                ):
                    return False
                value = self.criteria._numeric_metric(observation, metric)
                return isinstance(value, (int, float)) and not isinstance(value, bool)
            if kind == "state_equals":
                path = str(criterion.get("path", criterion.get("metric", "")))
                return self.criteria._state_value(observation, path) is not None
            if kind == "inventory_contains":
                return isinstance(
                    deep_get(observation, "inventory.items", sentinel), list
                )
            if kind == "equipment_count":
                if result.get("met") is True:
                    return True
                inventory_known = isinstance(
                    deep_get(observation, "inventory.items", sentinel), list
                )
                equipped_known = isinstance(
                    deep_get(observation, "equipment.equipped", sentinel), list
                )
                if str(criterion.get("category") or "") == "weapon":
                    wielding = deep_get(
                        observation, "equipment.wielding", sentinel
                    )
                    return (
                        inventory_known
                        and equipped_known
                        and isinstance(wielding, (list, str))
                    )
                return inventory_known and equipped_known
            if kind == "equipment_wielding":
                if result.get("met") is True:
                    return True
                return isinstance(
                    deep_get(observation, "equipment.wielding", sentinel),
                    (list, str),
                )
            if kind == "location_reached":
                if result.get("met") is True:
                    return True
                room_id = deep_get(
                    observation,
                    "look.room.num",
                    deep_get(observation, "look.room_id", sentinel),
                )
                room_name = deep_get(observation, "look.room.name", sentinel)
                requires_id = criterion.get("room_id") is not None
                requires_name = bool(
                    str(
                        criterion.get("location", criterion.get("room", ""))
                        or ""
                    ).strip()
                )
                return (
                    (not requires_id or room_id is not sentinel)
                    and (not requires_name or room_name is not sentinel)
                )
            return False

        return {
            **evaluation,
            "evidence_complete": all(known(criterion) for criterion in public),
        }

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
            "equipment_count",
            "equipment_wielding",
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
            if criterion.get("kind")
            not in {
                PHASE_ACTION_SUCCEEDED,
                PHASE_KEEPER_TARGET_KILLS,
                RESEARCH_RETRY_UNLOCKED,
            }
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
        leases = self.storage.get_runtime(
            CAMPAIGN_PHASE_PROGRESS_LEASE_RUNTIME_KEY, {}
        )
        lease = (
            leases.get(str(phase["id"]), {})
            if isinstance(leases, dict)
            else {}
        )
        lease = lease if isinstance(lease, dict) else {}
        results: list[dict[str, Any]] = []
        evidence_event_ids: list[str] = []
        for criterion in annotated:
            criterion_id = str(criterion["id"])
            if criterion.get("kind") == PHASE_KEEPER_TARGET_KILLS:
                required = int(criterion.get("count", 1) or 1)
                observed = int(lease.get("phase_target_kills", 0) or 0)
                met = observed >= required
                results.append(
                    {
                        "id": criterion_id,
                        "kind": PHASE_KEEPER_TARGET_KILLS,
                        "met": met,
                        "detail": (
                            f"verified phase-local keeper target kills {observed}; "
                            f"required {required}"
                        ),
                    }
                )
                continue
            if criterion.get("kind") == RESEARCH_RETRY_UNLOCKED:
                # The CampaignCoordinator deliberately has no access to the
                # controller's retained research-exhaustion evidence.  The
                # controller evaluates this internal criterion against the
                # phase's durable baseline before calling ordinary completion.
                results.append(
                    {
                        "id": criterion_id,
                        "kind": RESEARCH_RETRY_UNLOCKED,
                        "met": False,
                        "detail": (
                            "awaiting a controller-verified material capability "
                            "or world-evidence change"
                        ),
                    }
                )
                continue
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
        allow_abandonment: bool = True,
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
                if not allow_abandonment:
                    # A keeper-owned combat phase may still be outside safety
                    # when an abandonment predicate becomes true.  Report the
                    # verified outcome without terminalizing the phase so the
                    # controller can retain survival ownership until a source-
                    # verified sanctuary is reached.
                    return PhaseOutcome(
                        False,
                        False,
                        phase,
                        {
                            "reason": reason,
                            "abandonment": abandonment,
                            "abandonment_deferred": True,
                        },
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

    def budget_exhausted(self, phase: dict[str, Any]) -> dict[str, Any] | None:
        budget = phase.get("budget") if isinstance(phase.get("budget"), dict) else {}
        max_actions = max(8, int(budget.get("max_actions", 24) or 24))
        max_minutes = max(30, int(budget.get("max_minutes", 45) or 45))
        attempts = int(phase.get("attempt_count", 0) or 0)
        activated_at = str(phase.get("activated_at") or phase.get("created_at") or "")
        elapsed_minutes = 0.0
        downtime_seconds = 0.0
        downtime_at_lease_seconds = 0.0
        elapsed_basis = "phase_activation"
        last_verified_progress_at: str | None = None
        downtime = self.storage.campaign_phase_downtime(
            str(phase.get("id") or "")
        )
        if isinstance(downtime, dict):
            try:
                downtime_seconds = max(0.0, float(downtime.get("seconds", 0.0) or 0.0))
            except (TypeError, ValueError):
                downtime_seconds = 0.0
        reference_at = activated_at
        if str(phase.get("kind") or "") == "farm":
            leases = self.storage.get_runtime(
                CAMPAIGN_PHASE_PROGRESS_LEASE_RUNTIME_KEY, {}
            )
            lease = (
                leases.get(str(phase.get("id") or ""))
                if isinstance(leases, dict)
                else None
            )
            if isinstance(lease, dict):
                renewed_at = str(lease.get("renewed_at") or "")
                try:
                    activated_value = datetime.fromisoformat(
                        activated_at.replace("Z", "+00:00")
                    )
                    renewed_value = datetime.fromisoformat(
                        renewed_at.replace("Z", "+00:00")
                    )
                except (TypeError, ValueError):
                    pass
                else:
                    if renewed_value >= activated_value:
                        reference_at = renewed_at
                        elapsed_basis = "last_verified_keeper_progress"
                        last_verified_progress_at = renewed_at
                        try:
                            downtime_at_lease_seconds = max(
                                0.0,
                                float(lease.get("downtime_seconds", 0.0) or 0.0),
                            )
                        except (TypeError, ValueError):
                            downtime_at_lease_seconds = 0.0
        try:
            activated = datetime.fromisoformat(reference_at.replace("Z", "+00:00"))
            elapsed_minutes = max(
                0.0,
                (
                    (
                        datetime.now(timezone.utc)
                        - activated.astimezone(timezone.utc)
                    ).total_seconds()
                    - max(0.0, downtime_seconds - downtime_at_lease_seconds)
                )
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
                "downtime_minutes": round(downtime_seconds / 60.0, 1),
                "elapsed_basis": elapsed_basis,
                "last_verified_progress_at": last_verified_progress_at,
            }
        if elapsed_minutes >= max_minutes:
            return {
                "kind": "time_budget",
                "elapsed_minutes": round(elapsed_minutes, 1),
                "limit": max_minutes,
                "attempts": attempts,
                "downtime_minutes": round(downtime_seconds / 60.0, 1),
                "elapsed_basis": elapsed_basis,
                "last_verified_progress_at": last_verified_progress_at,
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
        # ``expected_effect`` is retained on the attempt as audit and verifier
        # input, but it is model-authored prose/shape and therefore cannot own
        # semantic action identity.  Equivalent actions used to evade the
        # breaker whenever the model rephrased this field between turns.
        return json_hash(
            {
                "phase_kind": phase.get("kind"),
                "tool": tool,
                "arguments": arguments,
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
        failure_context: dict[str, Any] | None = None,
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
            failure_context=failure_context,
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
        failure_context: dict[str, Any] | None = None,
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
                failure_context=redact(failure_context),
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
                    "failure_context": redact(failure_context),
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

    @staticmethod
    def _compact_research_rejection(value: Any) -> dict[str, Any] | None:
        """Project one rejected farm candidate without nested duplicate evidence."""

        if not isinstance(value, dict):
            return None
        blocker = value.get("blocker")
        blocker = blocker if isinstance(blocker, dict) else {}
        compact: dict[str, Any] = {
            "room": value.get("room", blocker.get("assigned_room")),
            "target": value.get("target", blocker.get("hunt")),
            "use_safe_spots": value.get(
                "use_safe_spots", blocker.get("use_safe_spots")
            ),
            "tactic_id": value.get("tactic_id"),
            "reason": blocker.get("kind") or "unknown",
        }
        disposition = value.get("disposition")
        if isinstance(disposition, dict):
            compact["disposition"] = {
                key: disposition.get(key)
                for key in ("class", "scope")
                if disposition.get(key) is not None
            }
        for key in ("danger_limit",):
            if blocker.get(key) is not None:
                compact[key] = blocker.get(key)
        reasons = deep_get(blocker, "evidence.reasons", blocker.get("reasons"))
        if isinstance(reasons, list) and reasons:
            compact["details"] = [str(item)[:240] for item in reasons[:4]]
        hostiles = blocker.get("hostiles")
        if isinstance(hostiles, list) and hostiles:
            compact["hostiles"] = [
                {
                    key: hostile.get(key)
                    for key in ("name", "level", "chance", "cap")
                    if hostile.get(key) is not None
                }
                for hostile in hostiles[:6]
                if isinstance(hostile, dict)
            ]
        return {key: item for key, item in compact.items() if item not in (None, "", [])}

    @classmethod
    def _compact_phase_context(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        compact = {
            key: value.get(key)
            for key in (
                "target",
                "room",
                "use_safe_spots",
                "flee_below",
                "fight_above_vigor",
                "buy_food",
                "next_hp_milestone",
                "selection_basis",
                "deterministic_fallback",
                "deterministic_research_handoff",
                "compatibility_phase",
            )
            if value.get(key) is not None
        }
        constraints = value.get("constraints")
        if isinstance(constraints, dict):
            compact["constraints"] = {
                str(key): item
                for key, item in list(constraints.items())[:16]
                if isinstance(item, (str, int, float, bool)) or item is None
            }
        recipe = value.get("farm_recipe")
        if isinstance(recipe, dict):
            compact["farm_recipe"] = {
                key: recipe.get(key)
                for key in (
                    "target",
                    "room",
                    "use_safe_spots",
                    "flee_below",
                    "fight_above_vigor",
                    "buy_food",
                    "selection_basis",
                )
                if recipe.get(key) is not None
            }
        validation = value.get("recipe_validation")
        if isinstance(validation, dict):
            compact_validation: dict[str, Any] = {
                key: validation.get(key)
                for key in (
                    "status",
                    "fingerprint",
                    "candidate_count",
                    "candidate_set_fingerprint",
                    "disposition_fingerprint",
                    "tactic_count",
                    "progress",
                )
                if validation.get(key) is not None
            }
            selected = validation.get("recipe")
            if isinstance(selected, dict):
                compact_validation["selected"] = {
                    key: selected.get(key)
                    for key in ("target", "room", "use_safe_spots", "selection_basis")
                    if selected.get(key) is not None
                }
            rejection_counts: dict[str, int] = {}
            for rejected in validation.get("rejected", []):
                projected = cls._compact_research_rejection(rejected)
                if projected is not None:
                    reason = str(projected.get("reason") or "unknown")
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            if rejection_counts:
                compact_validation["rejection_count"] = sum(rejection_counts.values())
                compact_validation["rejection_reasons"] = rejection_counts
            compact["recipe_validation"] = compact_validation
        return compact

    @staticmethod
    def _compact_failure_context(value: Any) -> dict[str, Any] | None:
        """Keep exact causal action identity without replaying arbitrary payloads."""

        if not isinstance(value, dict):
            return None
        compact = {
            key: value.get(key)
            for key in (
                "stage",
                "tool",
                "plan_step_id",
                "phase_kind",
                "origin_room",
                "destination_room",
                "phase_work_implicated",
            )
            if value.get(key) is not None
        }
        arguments = value.get("arguments")
        if isinstance(arguments, dict):
            compact["arguments"] = {
                str(key): item
                for key, item in list(arguments.items())[:16]
                if isinstance(item, (str, int, float, bool)) or item is None
            }
        return compact or None

    @classmethod
    def _compact_phase(cls, phase: Any) -> dict[str, Any] | None:
        if not isinstance(phase, dict):
            return None
        compact: dict[str, Any] = {
            key: phase.get(key)
            for key in (
                "id",
                "parent_phase_id",
                "ordinal",
                "kind",
                "objective",
                "status",
                "attempt_count",
                "created_at",
                "updated_at",
                "terminal_at",
            )
            if phase.get(key) is not None
        }
        criteria = phase.get("success_criteria")
        if isinstance(criteria, list):
            compact["success_criteria"] = [
                {
                    key: criterion.get(key)
                    for key in (
                        "id",
                        "kind",
                        "metric",
                        "operator",
                        "value",
                        "tools",
                        "path",
                        "item",
                        "room_id",
                        "name",
                    )
                    if criterion.get(key) is not None
                }
                for criterion in criteria[:12]
                if isinstance(criterion, dict)
            ]
        context = cls._compact_phase_context(phase.get("context"))
        if context:
            compact["context"] = context
        failure = phase.get("last_failure")
        if isinstance(failure, dict):
            compact["last_failure"] = {
                "reason": str(failure.get("reason") or "")[:500],
                "recorded_at": failure.get("recorded_at"),
            }
            cause = cls._compact_failure_context(failure.get("cause"))
            if cause is not None:
                compact["last_failure"]["cause"] = cause
        return compact

    @classmethod
    def _compact_external_blocker(cls, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        compact = {
            key: value.get(key)
            for key in (
                "status",
                "kind",
                "fingerprint",
                "repeat_count",
                "candidate_count",
                "candidate_set_fingerprint",
                "disposition_fingerprint",
                "tactic_count",
                "progress",
                "recorded_at",
                "retry_state_fingerprint",
            )
            if value.get(key) is not None
        }
        rejections = []
        for rejected in value.get("rejected", []):
            projected = cls._compact_research_rejection(rejected)
            if projected is not None:
                rejections.append(projected)
        if rejections:
            compact["rejected"] = rejections[:32]
        guidance = " ".join(str(value.get("guidance") or "").split())
        if guidance:
            compact["guidance"] = guidance[:500]
        return compact

    @classmethod
    def _tactic_ledger(
        cls, history: list[dict[str, Any]], external_blocker: Any
    ) -> dict[str, Any]:
        successful: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        unique_rejections: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        research_fingerprints: dict[str, dict[str, Any]] = {}
        for phase in history:
            context = phase.get("context") if isinstance(phase.get("context"), dict) else {}
            tactic = {
                "phase_id": phase.get("id"),
                "kind": phase.get("kind"),
                "target": context.get("target"),
                "room": context.get("room"),
                "use_safe_spots": context.get("use_safe_spots"),
                "attempt_count": phase.get("attempt_count"),
            }
            tactic = {key: value for key, value in tactic.items() if value is not None}
            if phase.get("status") == "succeeded" and phase.get("kind") == "farm":
                successful.append(tactic)
            elif phase.get("status") == "failed" and phase.get("kind") == "farm":
                failure = phase.get("last_failure")
                if isinstance(failure, dict) and failure.get("reason"):
                    tactic["reason"] = str(failure.get("reason"))[:500]
                failed.append(tactic)

            validation = context.get("recipe_validation")
            if not isinstance(validation, dict):
                continue
            fingerprint = str(validation.get("fingerprint") or "")
            if fingerprint:
                research_fingerprints[fingerprint] = {
                    "fingerprint": fingerprint,
                    "status": validation.get("status"),
                    "candidate_count": validation.get("candidate_count"),
                }
            for rejected in validation.get("rejected", []):
                projected = cls._compact_research_rejection(rejected)
                if projected is None:
                    continue
                key = (
                    str(projected.get("room") or ""),
                    str(projected.get("target") or "").casefold(),
                    str(projected.get("use_safe_spots")),
                    str(projected.get("reason") or ""),
                )
                unique_rejections[key] = projected

        blocker = cls._compact_external_blocker(external_blocker)
        if blocker is not None:
            for rejected in blocker.get("rejected", []):
                key = (
                    str(rejected.get("room") or ""),
                    str(rejected.get("target") or "").casefold(),
                    str(rejected.get("use_safe_spots")),
                    str(rejected.get("reason") or ""),
                )
                unique_rejections[key] = rejected
        return {
            "successful_farms": successful[-8:],
            "failed_farms": failed[-12:],
            "unique_rejected_candidates": list(unique_rejections.values())[:32],
            "research_candidate_sets": list(research_fingerprints.values())[-8:],
            "external_blocker": blocker,
        }

    @staticmethod
    def _compact_attempt(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        compact = {
            key: value.get(key)
            for key in (
                "id",
                "phase_id",
                "semantic_action",
                "signature",
                "status",
                "created_at",
                "terminal_at",
            )
            if value.get(key) is not None
        }
        result = value.get("result")
        if isinstance(result, dict):
            compact_result = {
                key: result.get(key)
                for key in ("ok", "error", "reason", "for_level", "verification")
                if result.get(key) is not None
            }
            prey = result.get("prey")
            if isinstance(prey, list):
                compact_result["prey"] = [
                    {
                        key: item.get(key)
                        for key in (
                            "creature",
                            "level",
                            "best_room",
                            "rooms",
                            "chance",
                            "cap",
                            "risk",
                        )
                        if item.get(key) is not None
                    }
                    for item in prey[:12]
                    if isinstance(item, dict)
                ]
            if compact_result:
                compact["result"] = compact_result
        verification = value.get("verification")
        if isinstance(verification, dict):
            compact_verification = {
                key: verification.get(key)
                for key in (
                    "no_progress",
                    "known_no_progress",
                    "durably_deferred",
                    "transient_movement_failure",
                    "reason",
                )
                if verification.get(key) is not None
            }
            if compact_verification:
                compact["verification"] = compact_verification
        return compact

    @classmethod
    def _compact_run(cls, run: dict[str, Any]) -> dict[str, Any]:
        compact = {
            key: run.get(key)
            for key in ("id", "goal_id", "status", "strategy_summary", "created_at", "updated_at")
            if run.get(key) is not None
        }
        checkpoint = run.get("progress_checkpoint")
        if isinstance(checkpoint, dict):
            completion = checkpoint.get("completion")
            compact["progress_checkpoint"] = {
                "phase_id": checkpoint.get("phase_id"),
                "phase_kind": checkpoint.get("phase_kind"),
                "recorded_at": checkpoint.get("recorded_at"),
                "completion": (
                    {
                        key: completion.get(key)
                        for key in ("percent_estimate", "summary", "all_met")
                        if completion.get(key) is not None
                    }
                    if isinstance(completion, dict)
                    else None
                ),
            }
        blocker = cls._compact_external_blocker(run.get("external_blocker"))
        if blocker is not None:
            compact["external_blocker"] = blocker
        memory = run.get("working_memory")
        if isinstance(memory, dict):
            compact["working_memory"] = {
                str(key): item
                for key, item in list(memory.items())[:20]
                if isinstance(item, (str, int, float, bool)) or item is None
            }
        return compact

    def manager_context(
        self,
        run: dict[str, Any],
        phase: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Return decision-relevant campaign memory without raw phase payloads."""

        history = self.storage.campaign_phases(run["id"])
        attempt_phase = phase or (history[-1] if history else None)
        attempts = (
            self.storage.phase_attempts(attempt_phase["id"], limit=8)
            if attempt_phase
            else []
        )
        return {
            "run": self._compact_run(run),
            "active_phase": self._compact_phase(phase),
            "phase_capabilities": {
                kind: sorted(tool_names)
                for kind, tool_names in sorted(PHASE_TOOL_NAMES.items())
            },
            "recent_phase_summaries": [
                projected
                for value in history[-8:]
                if (projected := self._compact_phase(value)) is not None
            ],
            "recent_phase_attempts": [
                projected
                for value in attempts
                if (projected := self._compact_attempt(value)) is not None
            ],
            "tactic_ledger": self._tactic_ledger(history, run.get("external_blocker")),
            "action_breaker_limit": self.ACTION_FAILURE_LIMIT,
            "instructions": (
                "Preserve the strategic goal across routine failures. Supporting preparation is an internal "
                "child phase, never a public supervisor goal. Select a materially different tactic after a breaker."
            ),
        }

    def tactical_context(
        self,
        run: dict[str, Any],
        phase: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Return only the active phase and compact facts needed to execute it."""

        history = self.storage.campaign_phases(run["id"])
        attempt_phase = phase or (history[-1] if history else None)
        attempts = (
            self.storage.phase_attempts(attempt_phase["id"], limit=8)
            if attempt_phase
            else []
        )
        return {
            "run": self._compact_run(run),
            "active_phase": self._compact_phase(phase),
            "recent_phase_summaries": [
                projected
                for value in history[-3:]
                if (projected := self._compact_phase(value)) is not None
            ],
            "recent_phase_attempts": [
                projected
                for value in attempts
                if (projected := self._compact_attempt(value)) is not None
            ],
            "action_breaker_limit": self.ACTION_FAILURE_LIMIT,
            "instructions": (
                "Execute only the active bounded phase. Historical phases are summaries, not current state."
            ),
        }

    def context(
        self,
        run: dict[str, Any],
        phase: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Compatibility alias for callers that need tactical execution context."""

        return self.tactical_context(run, phase)
