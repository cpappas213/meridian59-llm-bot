from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Callable

from .config import LearningConfig
from .contracts import ARMOR_NAME_MARKERS, WEAPON_NAME_MARKERS
from .storage import Storage
from .utils import canonical_json, deep_get, json_hash, timestamp, uuid7


FAILURE_CLASSES = {
    "insufficient_combat_power",
    "missing_capability",
    "route_unavailable",
    "world_unavailable",
    "invalid_reference",
    "ineffective_tactic",
    "dependency_failure",
}

COMBAT_TOOLS = {"pvp_engage", "pvp_seek", "fight", "attack", "approach", "autopilot"}
ROUTE_TOOLS = {"travel", "map", "exits", "walk_to", "go_through", "escape_underworld", "leave_raza"}
RECOVERABLE_PREPARATION_TOOLS = {
    *ROUTE_TOOLS,
    # `act` is the broker's one-shot inventory/equipment/world interaction
    # surface. A refused drop, get, use, or activation disproves that exact
    # action in the current state; it does not prove a strategic campaign such
    # as raising max HP is impossible.
    "act",
    "bank",
    "shop",
    "merchants",
    "sell",
    "sell_all",
    # A cast is a capability/preparation attempt unless its concrete result
    # contains combat or survival evidence.  Treating every cast as combat made
    # an ambiguous Create Weapon inventory receipt look like proof that the
    # character needed more HP or equipment, then locked the very tactic that
    # could supply that equipment.
    "cast",
    "equip_best",
    "rest",
    "look",
    "inventory",
    "prey",
    "hunting_grounds",
    "knowledge_search",
}
HOME_EVENT_KINDS = {"goal.home_reached"}
COMBAT_MEMORY_KEY = "combat_outcomes_v1"
WEAPON_WORDS = WEAPON_NAME_MARKERS
ARMOR_WORDS = ARMOR_NAME_MARKERS
HEALING_SUPPLY_WORDS = ("flask", "healing potion")
INVENTORY_CAPACITY_REFUSAL_MARKERS = (
    "carry too much",
    "carrying too much",
    "cannot carry",
    "can't carry",
    "unable to carry",
    "too heavy",
    "too bulky",
    "inventory is full",
    "inventory full",
)
FARM_RECOVERY_REASON_MARKERS = (
    "health reached the keeper flee threshold",
    "health crossed the keeper flee threshold",
    "the keeper had to withdraw",
    "repeated retreat episodes reached",
    "repeated critical-health interrupts",
)

FARM_ROOM_HAZARD_MARKERS = (
    "live overlevel hostile",
    "live over-level hostile",
    "overlevel hostile",
    "over-level hostile",
    "source room overlevel",
    "source-room overlevel",
    "spawn table hazard",
    "room population hazard",
)
FARM_DEATH_EVIDENCE_MARKERS = (
    "death",
    "died",
    "killed the character",
    "failed lethally",
    "lethal",
)
FARM_SAFE_SPOT_EVIDENCE_MARKERS = (
    "safe spot",
    "safe-spot",
    "wall tactic",
    "held wall",
)


def normalize_farm_target(value: Any) -> str:
    """Return the stable prey spelling used by farm-tactic identities."""

    return " ".join(str(value or "").casefold().split())


def farm_target_spellings_match(left: Any, right: Any) -> bool:
    """Compare ordinary creature spellings without collapsing distinct prey."""

    def variants(value: Any) -> set[str]:
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
        normalized = " ".join(re.findall(r"[a-z0-9]+", text.casefold()))
        if not normalized:
            return set()
        values = {normalized}
        words = normalized.split()
        last = words[-1]
        singular: str | None = None
        if last.endswith("ies") and len(last) > 3:
            singular = last[:-3] + "y"
        elif last.endswith("es") and last[:-2].endswith(
            ("s", "x", "z", "ch", "sh")
        ):
            singular = last[:-2]
        elif last.endswith("s") and len(last) > 1:
            singular = last[:-1]
        if singular:
            values.add(" ".join([*words[:-1], singular]))
        return values

    return bool(variants(left) & variants(right))


def farm_tactic_identity(
    room: Any, target: Any, use_safe_spots: Any
) -> dict[str, Any]:
    """Describe only controllable farm inputs, never their observed outcome."""

    strategy = use_safe_spots if isinstance(use_safe_spots, bool) else None
    return {
        "room": str(room) if room is not None else None,
        "target": normalize_farm_target(target),
        "use_safe_spots": strategy,
    }


def farm_tactic_key(room: Any, target: Any, use_safe_spots: Any) -> str:
    """Return a compact stable key for one room/prey/positioning tactic."""

    return "farm:" + json_hash(
        farm_tactic_identity(room, target, use_safe_spots)
    )[:24]


def farm_quarantine_evidence_class(record: dict[str, Any]) -> str:
    """Classify durable farm evidence without timestamp/count fingerprint churn."""

    explicit = str(record.get("evidence_class") or "").strip().casefold()
    if explicit:
        return explicit
    reasons = " ".join(
        str(item).casefold()
        for item in record.get("reasons", [])
        if item is not None
    )
    deltas = record.get("deltas") if isinstance(record.get("deltas"), dict) else {}
    death_delta = False
    for key in ("deaths", "deaths_in_safe_spot", "deaths_in_proven_safe_spot"):
        try:
            death_delta = death_delta or int(deltas.get(key, 0) or 0) > 0
        except (TypeError, ValueError):
            continue
    if death_delta or any(marker in reasons for marker in FARM_DEATH_EVIDENCE_MARKERS):
        return "death"
    if record.get("live_overlevel_hostiles") or any(
        marker in reasons for marker in FARM_ROOM_HAZARD_MARKERS
    ):
        return "room_hazard"
    if any(marker in reasons for marker in FARM_SAFE_SPOT_EVIDENCE_MARKERS):
        return "safe_spot_failure"
    if record.get("use_safe_spots") is False:
        return "open_field_failure"
    return "survivability_failure"


def farm_quarantine_scope(record: dict[str, Any]) -> str:
    """Return the causal scope enforced for a retained farm failure."""

    explicit = str(record.get("evidence_scope") or "").strip().casefold()
    if explicit in {"room", "room_and_prey", "exact_tactic"}:
        return explicit
    evidence_class = farm_quarantine_evidence_class(record)
    if evidence_class == "room_hazard":
        return "room"
    if evidence_class == "death":
        # A lethal result is not evidence that removing the wall constraint is
        # safe.  Retain it for this room/prey pairing until a capability or
        # other explicit enabling change invalidates the quarantine.
        return "room_and_prey"
    if evidence_class == "safe_spot_failure":
        return "exact_tactic"
    if isinstance(record.get("use_safe_spots"), bool):
        return "exact_tactic"
    return "room_and_prey"


def farm_quarantine_evidence_identity(record: dict[str, Any]) -> dict[str, Any]:
    """Return stable decision evidence for research/disposition fingerprints."""

    return {
        "class": farm_quarantine_evidence_class(record),
        "scope": farm_quarantine_scope(record),
    }


def farm_quarantine_entries(
    raw: Any, *, room: Any | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """Read both legacy room-keyed and exact-tactic quarantine records."""

    if not isinstance(raw, dict):
        return []
    entries: list[tuple[str, dict[str, Any]]] = []
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        recorded_room = value.get("assigned_room", value.get("room"))
        if room is not None and str(recorded_room) != str(room):
            # Very old records sometimes had only their room dictionary key.
            if not (str(key) == str(room) and not str(key).startswith("farm:")):
                continue
        entries.append((str(key), value))
    return entries


def farm_quarantine_matches(
    record: dict[str, Any],
    *,
    room: Any,
    target: Any,
    use_safe_spots: Any,
    target_matches: Callable[[Any, Any], bool] | None = None,
) -> bool:
    """Whether one retained disposition applies to the proposed farm tactic."""

    recorded_room = record.get("assigned_room", record.get("room"))
    if recorded_room is not None and room is not None and str(recorded_room) != str(room):
        return False
    scope = farm_quarantine_scope(record)
    if scope == "room":
        return True
    recorded_target = normalize_farm_target(record.get("target"))
    requested_target = normalize_farm_target(target)
    target_matches = target_matches or farm_target_spellings_match
    if (
        recorded_target
        and requested_target
        and not target_matches(record.get("target"), target)
    ):
        return False
    if scope == "room_and_prey":
        return True
    recorded_strategy = record.get("use_safe_spots")
    if (
        not isinstance(recorded_strategy, bool)
        and farm_quarantine_evidence_class(record) == "safe_spot_failure"
    ):
        recorded_strategy = True
    if isinstance(recorded_strategy, bool) and isinstance(use_safe_spots, bool):
        return recorded_strategy == use_safe_spots
    return True


def is_inventory_capacity_refusal(reason: Any) -> bool:
    text = str(reason or "").casefold()
    return any(marker in text for marker in INVENTORY_CAPACITY_REFUSAL_MARKERS)


def is_obsolete_farm_recovery_failure(reason: Any) -> bool:
    """Whether old evidence mislabeled a recoverable farm cycle as failure."""

    text = " ".join(str(reason or "").casefold().split())
    if not any(marker in text for marker in FARM_RECOVERY_REASON_MARKERS):
        return False
    return not any(
        marker in text
        for marker in (
            "death",
            "died",
            "overlevel hostile",
            "safe spot failed",
            "disproved the safe spot",
            "could not reach safety",
        )
    )


class GoalDeferredError(ValueError):
    code = "GOAL_DEFERRED"

    def __init__(self, result: dict[str, Any]):
        self.result = result
        lesson = result.get("lesson", {})
        super().__init__(
            str(lesson.get("summary") or "Equivalent goal is deferred until its retry conditions are met")
        )


class GoalLearning:
    """Durable failure lessons and deterministic retry gates above the broker."""

    def __init__(
        self,
        config: LearningConfig,
        storage: Storage,
        corpus_version: Callable[[], str],
    ) -> None:
        self.config = config
        self.storage = storage
        self._corpus_version = corpus_version

    @staticmethod
    def _normal_text(value: Any) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).split())

    @classmethod
    def _is_finish_location(cls, criterion: dict[str, Any]) -> bool:
        kind = criterion.get("kind")
        if kind == "location_reached":
            return True
        if kind == "event_occurred":
            event_kind = str(criterion.get("event_kind", "")).casefold()
            return event_kind in HOME_EVENT_KINDS or event_kind.startswith("goal.returned_to_")
        if kind == "state_equals":
            path = str(criterion.get("path", "")).casefold()
            return "room" in path
        return False

    @staticmethod
    def _is_finish_coordinate(criterion: dict[str, Any]) -> bool:
        if criterion.get("kind") != "state_equals":
            return False
        path = str(criterion.get("path", "")).casefold()
        return path.endswith(("position.col", "position.row"))

    @classmethod
    def _criterion_identity(cls, criterion: dict[str, Any]) -> dict[str, Any]:
        value = {key: item for key, item in criterion.items() if key not in {"id", "after_cursor"}}
        if "criteria" in value and isinstance(value["criteria"], list):
            # Composite ids are local wiring, not semantic goal identity.
            value["criteria"] = ["criterion"] * len(value["criteria"])
        if "criterion_ids" in value and isinstance(value["criterion_ids"], list):
            value["criterion_ids"] = ["criterion"] * len(value["criterion_ids"])
        for key, item in list(value.items()):
            if isinstance(item, str):
                value[key] = cls._normal_text(item)
        return value

    @classmethod
    def goal_family(cls, goal: dict[str, Any]) -> str:
        source_criteria = [
            item
            for item in goal.get("success_criteria", [])
            if isinstance(item, dict)
        ]
        has_finish_location = any(cls._is_finish_location(item) for item in source_criteria)
        criteria = [
            cls._criterion_identity(item)
            for item in source_criteria
            if not cls._is_finish_location(item)
            and not (has_finish_location and cls._is_finish_coordinate(item))
        ]
        criteria.sort(key=canonical_json)
        if criteria:
            identity: dict[str, Any] = {"success_criteria": criteria}
        else:
            identity = {"objective": cls._normal_text(goal.get("objective", ""))}
        return "goal-family:" + json_hash(identity)[:24]

    @staticmethod
    def _inventory_items(observation: dict[str, Any]) -> list[dict[str, Any]]:
        items = deep_get(observation, "inventory.items", [])
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    @classmethod
    def state_profile(cls, observation: dict[str, Any], corpus_version: str) -> dict[str, Any]:
        health = deep_get(observation, "status.vitals.health", deep_get(observation, "look.vitals.health", {}))
        mana = deep_get(observation, "status.vitals.mana", deep_get(observation, "look.vitals.mana", {}))
        vigor = deep_get(observation, "status.vitals.vigor", deep_get(observation, "look.vitals.vigor", {}))
        items = cls._inventory_items(observation)
        equipment = []
        equipment_signal_seen = False
        carried_weapons: list[str] = []
        carried_armor: list[str] = []
        healing_supplies: list[dict[str, Any]] = []
        healing_supply_count = 0
        carried_currency = 0
        for item in items:
            can = [str(value).casefold() for value in item.get("can", [])] if isinstance(item.get("can"), list) else []
            name = str(item.get("name") or "")
            normalized_name = name.casefold()
            if "shilling" in normalized_name:
                raw_amount = next(
                    (item.get(key) for key in ("amount", "quantity", "count") if item.get(key) is not None),
                    1,
                )
                carried_currency += int(raw_amount) if isinstance(raw_amount, (int, float)) and raw_amount > 0 else 1
            if any(word in normalized_name for word in WEAPON_WORDS):
                carried_weapons.append(name)
            if item.get("slot") or any(word in normalized_name for word in ARMOR_WORDS):
                carried_armor.append(name)
            if any(word in normalized_name for word in HEALING_SUPPLY_WORDS):
                raw_amount = next(
                    (item.get(key) for key in ("amount", "quantity", "count") if item.get(key) is not None),
                    1,
                )
                amount = int(raw_amount) if isinstance(raw_amount, (int, float)) and raw_amount > 0 else 1
                healing_supply_count += amount
                healing_supplies.append({"name": name, "id": item.get("id"), "amount": amount})
            if any(key in item for key in ("equipped", "in_use", "worn")) or "unuse" in can:
                equipment_signal_seen = True
            if item.get("equipped") or item.get("in_use") or item.get("worn") or "unuse" in can:
                equipment.append({"name": item.get("name"), "id": item.get("id"), "slot": item.get("slot")})

        # Prefer the harness' server-verified plUsing view when available. It is
        # deliberately separate from the pack: carrying a weapon is not evidence
        # that the character is wielding it. The inventory-level equipped list is
        # a compatible fallback for the same newer broker revision.
        observed_equipment = observation.get("equipment")
        wielded_weapons: list[str] = []
        if isinstance(observed_equipment, dict) and observed_equipment.get("known") is True:
            equipment_signal_seen = True
            verified = observed_equipment.get("equipped", [])
            if isinstance(verified, list):
                equipment = [
                    {
                        "name": item.get("name"),
                        "id": item.get("id"),
                        "slot": item.get("slot"),
                    }
                    if isinstance(item, dict)
                    else {"name": str(item), "id": None, "slot": None}
                    for item in verified
                ]
            verified_wielding = observed_equipment.get("wielding")
            if isinstance(verified_wielding, str) and verified_wielding.strip():
                wielded_weapons = [verified_wielding.strip()]
            elif isinstance(verified_wielding, list):
                wielded_weapons = [
                    str(item).strip()
                    for item in verified_wielding
                    if str(item).strip()
                ]
        elif isinstance(deep_get(observation, "inventory.equipped"), list):
            equipment_signal_seen = True
            equipment = [
                {"name": str(name), "id": None, "slot": None}
                for name in deep_get(observation, "inventory.equipped", [])
            ]
        if not wielded_weapons and equipment_signal_seen:
            wielded_weapons = [
                str(item.get("name") or "").strip()
                for item in equipment
                if isinstance(item, dict)
                and any(
                    word in str(item.get("name") or "").casefold()
                    for word in WEAPON_WORDS
                )
            ]
        attributes = deep_get(observation, "status.attributes", deep_get(observation, "look.attributes", {}))
        skills = deep_get(observation, "status.skills", deep_get(observation, "look.skills", []))
        abilities = deep_get(
            observation,
            "status.spells",
            deep_get(observation, "status.abilities", deep_get(observation, "look.spells", [])),
        )
        room = deep_get(observation, "look.room", {})
        profile = {
            "max_health": health.get("max") if isinstance(health, dict) else None,
            "max_mana": mana.get("max") if isinstance(mana, dict) else None,
            "vigor": vigor.get("current", vigor.get("value")) if isinstance(vigor, dict) else None,
            "room": {
                "id": room.get("num", room.get("id")) if isinstance(room, dict) else None,
                "name": room.get("name") if isinstance(room, dict) else str(room or ""),
            },
            "equipment": sorted(equipment, key=canonical_json),
            "equipment_state": "known" if equipment_signal_seen else "unknown",
            "wielded_weapons": sorted(set(wielded_weapons)),
            "carried_weapons": sorted(set(carried_weapons)),
            "carried_armor": sorted(set(carried_armor)),
            "healing_supplies": sorted(healing_supplies, key=canonical_json),
            "healing_supply_count": healing_supply_count,
            "carried_currency": carried_currency,
            "attributes": attributes if isinstance(attributes, (dict, list)) else {},
            "skills": skills if isinstance(skills, (dict, list)) else [],
            "abilities": abilities if isinstance(abilities, (dict, list)) else [],
            "corpus_version": corpus_version,
        }
        carry = deep_get(observation, "inventory.carry")
        inventory_load_state: Any
        if isinstance(carry, dict):
            # The newer broker derives exact weight/bulk load when every item is
            # known and an explicitly marked lower bound otherwise.  Preserve
            # that distinction in the retry fingerprint.
            inventory_load_state = carry
        else:
            # Older brokers do not expose carry.  Inventory identity is still a
            # deterministic state-change signal and is safer than a timed retry.
            inventory_load_state = sorted(
                [
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "amount": item.get("amount", item.get("quantity", item.get("count", 1))),
                    }
                    for item in items
                ],
                key=canonical_json,
            )
        profile["inventory_load_hash"] = json_hash(inventory_load_state)
        # Server object ids identify one session's item instances, not combat
        # capability.  Reconnects may assign a different id to the same sword;
        # including that id in a durable retry fingerprint falsely unlocked
        # lessons even though the character was no better prepared.
        semantic_equipment = cls._semantic_equipment(profile["equipment"])
        profile["equipment_hash"] = json_hash(semantic_equipment)
        profile["equipment_observation_hash"] = json_hash(
            {"state": profile["equipment_state"], "items": semantic_equipment}
        )
        profile["attributes_hash"] = json_hash(profile["attributes"])
        profile["skills_hash"] = json_hash(profile["skills"])
        profile["abilities_hash"] = json_hash(profile["abilities"])
        profile["healing_supplies_hash"] = json_hash(profile["healing_supplies"])
        profile["capability_hash"] = json_hash(
            {
                key: profile[key]
                for key in (
                    "max_health",
                    "max_mana",
                    "equipment_hash",
                    "attributes_hash",
                    "skills_hash",
                    "abilities_hash",
                    "healing_supply_count",
                )
            }
        )
        return profile

    @staticmethod
    def _semantic_equipment(equipment: Any) -> list[dict[str, str | None]]:
        """Return stable equipment identity without ephemeral server object ids."""

        if not isinstance(equipment, list):
            return []
        values = [
            {
                "name": " ".join(str(item.get("name") or "").casefold().split()),
                "slot": (
                    " ".join(str(item.get("slot") or "").casefold().split())
                    or None
                ),
            }
            for item in equipment
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        return sorted(values, key=canonical_json)

    def profile(self, observation: dict[str, Any]) -> dict[str, Any]:
        return self.state_profile(observation, self._corpus_version())

    @staticmethod
    def _health_value(observation: dict[str, Any]) -> dict[str, Any]:
        value = deep_get(observation, "status.vitals.health", deep_get(observation, "look.vitals.health", {}))
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _underworld(observation: dict[str, Any]) -> bool:
        room = deep_get(observation, "look.room.name", deep_get(observation, "look.room", ""))
        return "underworld" in str(room or "").casefold()

    def record_combat_outcome(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        before: dict[str, Any],
        result: Any = None,
        after: dict[str, Any] | None = None,
        error: str | None = None,
        died: bool | None = None,
    ) -> dict[str, Any]:
        """Persist compact empirical combat evidence without adding a schema migration."""
        if tool not in COMBAT_TOOLS:
            return {}
        result_dict = result if isinstance(result, dict) else {}
        before_health = self._health_value(before)
        after_health = self._health_value(after or {})
        inferred_death = bool(
            result_dict.get("died")
            or (after is not None and not self._underworld(before) and self._underworld(after))
            or (error and "not in game" in error.casefold() and after is not None and self._underworld(after))
        )
        target = (
            arguments.get("target")
            or deep_get(result_dict, "target.name")
            or result_dict.get("target")
            or "unknown"
        )
        room = self.profile(before).get("room")
        entry = {
            "id": uuid7(),
            "occurred_at": timestamp(),
            "tool": tool,
            "target": str(target),
            "room": room,
            "outcome": (
                "died" if (inferred_death if died is None else died)
                else "killed" if result_dict.get("killed") is True
                else "disengaged" if result_dict.get("disengaged") or result_dict.get("outcome") == "disengaged_low_health"
                else "failed" if error
                else str(result_dict.get("outcome") or "survived")
            ),
            "died": inferred_death if died is None else bool(died),
            "killed": bool(result_dict.get("killed")),
            "rounds": result_dict.get("rounds"),
            "health_before": before_health,
            "health_after": after_health,
            "equipment_state": self.profile(before).get("equipment_state"),
            "equipment": self.profile(before).get("equipment", []),
            "equipment_hash": self.profile(before).get("equipment_hash"),
            "equipment_observation_hash": self.profile(before).get("equipment_observation_hash"),
            "error": str(error or "")[:500] or None,
        }
        history = self.storage.get_runtime(COMBAT_MEMORY_KEY, [])
        values = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
        values.append(entry)
        self.storage.set_runtime(COMBAT_MEMORY_KEY, values[-200:])
        return entry

    def combat_summary(self, observation: dict[str, Any] | None = None, *, limit: int = 12) -> dict[str, Any]:
        history = self.storage.get_runtime(COMBAT_MEMORY_KEY, [])
        values = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
        aggregates: dict[str, dict[str, Any]] = {}
        for item in values:
            key = self._normal_text(item.get("target") or "unknown") or "unknown"
            row = aggregates.setdefault(
                key,
                {"target": item.get("target"), "encounters": 0, "kills": 0, "deaths": 0, "disengagements": 0},
            )
            row["encounters"] += 1
            row["kills"] += int(item.get("killed") is True)
            row["deaths"] += int(item.get("died") is True)
            row["disengagements"] += int(item.get("outcome") == "disengaged")
            row["last_outcome"] = item.get("outcome")
            row["last_at"] = item.get("occurred_at")
        return {
            "recent": values[-max(1, min(limit, 30)) :],
            "by_target": sorted(aggregates.values(), key=lambda row: str(row.get("last_at", "")), reverse=True)[:limit],
            "total_deaths": sum(int(item.get("died") is True) for item in values),
        }

    def farm_room_scorecard(self, *, limit: int = 12) -> list[dict[str, Any]]:
        """Aggregate keeper evidence by room, prey, and wall/open-field tactic.

        Raw keeper samples are useful for audit, but poor planner memory: one
        productive target kill can disappear inside several nuisance clears.
        This compact view lets both the planner and supervisor compare realized throughput
        and safety with the source-derived room spawn mix.
        """
        raw_history = self.storage.get_runtime("background_farm_history_v1", [])
        history = (
            [item for item in raw_history if isinstance(item, dict)]
            if isinstance(raw_history, list)
            else []
        )
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for sample in history[-100:]:
            assigned_room = sample.get("assigned_room")
            observed_room = sample.get("room")
            if isinstance(observed_room, dict):
                observed_room = observed_room.get("id", observed_room.get("name"))
            room = assigned_room if assigned_room not in (None, "") else observed_room
            target = self._normal_text(sample.get("target") or "unknown") or "unknown"
            safe_spots = sample.get("use_safe_spots")
            strategy = (
                "safe_spots"
                if safe_spots is True
                else "open_field"
                if safe_spots is False
                else "unspecified"
            )
            key = (str(room), target, strategy)
            row = grouped.setdefault(
                key,
                {
                    "room": room,
                    "target": target,
                    "strategy": strategy,
                    "samples": 0,
                    "samples_at_assigned_room": 0,
                    "kills": 0,
                    "target_kills": 0,
                    "other_kills": 0,
                    "unattributed_kills": 0,
                    "withdrawals": 0,
                    "route_withdrawals": 0,
                    "route_damage_samples": 0,
                    "deaths": 0,
                    "healing_supplies_used": 0,
                    "risk_samples": 0,
                    "recovery_samples": 0,
                    "safe_spot_failure_count": 0,
                    "last_observed_at": None,
                },
            )
            row["samples"] += 1
            if sample.get("at_assigned_room") is True:
                row["samples_at_assigned_room"] += 1
            deltas = sample.get("deltas") if isinstance(sample.get("deltas"), dict) else {}
            kills = int(deltas.get("kills", 0) or 0)
            withdrawals = int(deltas.get("withdrawals", 0) or 0)
            deaths = int(deltas.get("deaths", 0) or 0)
            by_target = (
                sample.get("kills_by_target")
                if isinstance(sample.get("kills_by_target"), dict)
                else {}
            )
            named_target_kills = sum(
                int(count or 0)
                for name, count in by_target.items()
                if self._normal_text(name) == target
            )
            named_other_kills = sum(
                int(count or 0)
                for name, count in by_target.items()
                if self._normal_text(name) != target
            )
            row["kills"] += kills
            row["target_kills"] += named_target_kills
            row["other_kills"] += named_other_kills
            row["unattributed_kills"] += int(sample.get("unattributed_kills", 0) or 0)
            row["withdrawals"] += withdrawals
            row["deaths"] += deaths
            row["healing_supplies_used"] += int(sample.get("healing_supplies_used", 0) or 0)
            if sample.get("at_assigned_room") is False:
                row["route_withdrawals"] += withdrawals
                warnings = sample.get("tactic_warnings", [])
                if isinstance(warnings, list) and any(
                    "route damage" in str(warning).casefold()
                    for warning in warnings
                ):
                    row["route_damage_samples"] += 1
            recovery_reasons = sample.get("recovery_reasons")
            recovery_reasons = (
                recovery_reasons if isinstance(recovery_reasons, list) else []
            )
            if recovery_reasons:
                row["recovery_samples"] += 1
            risk_reasons = sample.get("risk_reasons")
            risk_reasons = risk_reasons if isinstance(risk_reasons, list) else []
            material_risks = [
                reason
                for reason in risk_reasons
                if not (
                    sample.get("at_assigned_room") is True
                    and is_obsolete_farm_recovery_failure(reason)
                )
            ]
            if material_risks:
                row["risk_samples"] += 1
            row["safe_spot_failure_count"] = max(
                row["safe_spot_failure_count"],
                int(sample.get("safe_spot_failure_count", 0) or 0),
            )
            row["last_observed_at"] = sample.get("observed_at") or row["last_observed_at"]

        quarantine_values = self.farm_tactic_quarantines()
        values = list(grouped.values())
        for row in values:
            total_named = row["target_kills"] + row["other_kills"]
            row["target_kill_share"] = (
                round(row["target_kills"] / total_named, 3) if total_named else None
            )
            row["quarantined"] = any(
                str(item.get("room")) == str(row["room"])
                and self._normal_text(item.get("target") or "") == row["target"]
                and (
                    item.get("effective_use_safe_spots") is None
                    or (item.get("effective_use_safe_spots") is True and row["strategy"] == "safe_spots")
                    or (item.get("effective_use_safe_spots") is False and row["strategy"] == "open_field")
                )
                for item in quarantine_values
            )
            row["interpretation"] = (
                "Empirical controller evidence; productive kill/rest/withdraw/resume cycles are recovery telemetry, not room failure. Compare target_kill_share with the static spawn table and keep route failures separate from room combat."
            )
        values.sort(
            key=lambda row: str(row.get("last_observed_at") or ""), reverse=True
        )
        return values[: max(1, min(int(limit), 30))]

    def farm_tactic_quarantines(self) -> list[dict[str, Any]]:
        """Disclose legacy quarantine scope the same way execution enforces it."""
        raw = self.storage.get_runtime("farm_tactic_quarantine_v1", {})
        values = (
            [dict(item) for _key, item in farm_quarantine_entries(raw)]
        )
        for item in values:
            strategy = item.get("use_safe_spots")
            inferred = False
            if not isinstance(strategy, bool):
                reasons = [
                    str(reason).casefold()
                    for reason in item.get("reasons", [])
                    if reason is not None
                ]
                if reasons and all("safe spot" in reason for reason in reasons):
                    strategy = True
                    inferred = True
                else:
                    strategy = None
            item["effective_use_safe_spots"] = strategy
            item["tactic_id"] = item.get("tactic_id") or farm_tactic_key(
                item.get("assigned_room", item.get("room")),
                item.get("target"),
                strategy,
            )
            evidence_identity = farm_quarantine_evidence_identity(item)
            item["evidence_class"] = evidence_identity["class"]
            item["evidence_scope"] = evidence_identity["scope"]
            item["evidence_fingerprint"] = item.get(
                "evidence_fingerprint"
            ) or json_hash(
                {
                    "tactic_id": item["tactic_id"],
                    "disposition": evidence_identity,
                }
            )
            item["quarantine_scope"] = (
                "safe_spots"
                if evidence_identity["scope"] == "exact_tactic" and strategy is True
                else "open_field"
                if evidence_identity["scope"] == "exact_tactic" and strategy is False
                else evidence_identity["scope"]
            )
            if inferred:
                item["scope_note"] = (
                    "Legacy evidence only disproved the safe-spot strategy; it does not quarantine a separately evidenced open-field tactic."
                )
        return values

    def readiness_summary(self, observation: dict[str, Any]) -> dict[str, Any]:
        profile = self.profile(observation)
        health = self._health_value(observation)
        max_health = health.get("max")
        history = self.combat_summary(observation)
        recent_deaths = sum(int(item.get("died") is True) for item in history["recent"])
        if self._underworld(observation):
            recommended = "recover_from_underworld"
        elif recent_deaths and (profile["equipment_state"] == "unknown" or not profile["carried_armor"]):
            recommended = "equipment_skill_or_supplies"
        elif isinstance(max_health, (int, float)) and max_health < 30:
            recommended = "safe_hp_progression_to_30"
        else:
            recommended = "bounded_campaign_progression"
        return {
            "max_health": max_health,
            "pvp_eligible_by_guide": isinstance(max_health, (int, float)) and max_health >= 30,
            "pvp_rule_note": "Official new-player guidance uses 30 max HP; fresh live server evidence overrides it.",
            "equipment_state": profile["equipment_state"],
            "equipped": profile["equipment"],
            "wielded_weapons": profile["wielded_weapons"],
            "carried_weapons": profile["carried_weapons"],
            "carried_armor": profile["carried_armor"],
            "healing_supplies": profile["healing_supplies"],
            "healing_supply_count": profile["healing_supply_count"],
            "recent_combat_deaths": recent_deaths,
            "farm_tactic_quarantines": self.farm_tactic_quarantines()[-10:],
            "recent_farm_evidence": (
                self.storage.get_runtime("background_farm_history_v1", [])[-8:]
                if isinstance(self.storage.get_runtime("background_farm_history_v1", []), list)
                else []
            ),
            "farm_room_scorecard": self.farm_room_scorecard(),
            "recommended_goal_type": recommended,
        }

    @staticmethod
    def tactic_key(tool: str, arguments: dict[str, Any], observation: dict[str, Any]) -> str:
        room = deep_get(observation, "look.room", {})
        location = room.get("num", room.get("name")) if isinstance(room, dict) else room
        return "tactic:" + json_hash({"tool": tool, "arguments": arguments, "room": location})[:24]

    @staticmethod
    def tactic_family_key(tool: str, arguments: dict[str, Any], observation: dict[str, Any]) -> str:
        room = deep_get(observation, "look.room", {})
        location = room.get("num", room.get("name")) if isinstance(room, dict) else room
        if tool in ROUTE_TOOLS:
            target = {
                key: arguments.get(key)
                for key in ("destination", "to", "room", "room_id", "city", "target", "col", "row")
                if arguments.get(key) is not None
            }
            identity = {"kind": "route", "target": target, "room": location}
        else:
            identity = {"tool": tool, "arguments": arguments, "room": location}
        return "tactic-family:" + json_hash(identity)[:24]

    @staticmethod
    def classify(tool: str, reason: str, *, event_kind: str = "") -> tuple[str, str, float]:
        text = f"{reason} {event_kind}".casefold()
        if any(
            marker in text
            for marker in (
                "server answered nothing at all",
                "not a door problem but a lost packet",
                "reply that did not arrive inside",
            )
        ):
            # The broker explicitly distinguishes this from an exit refusal:
            # no authoritative response arrived. Route tools must not make the
            # more generic ROUTE_TOOLS branch turn transport silence into a
            # permanent location/corpus gate.
            return "dependency_failure", "tactic", 0.9
        if tool in {"map", "knowledge_search"} and any(marker in text for marker in ("no match", "not found", "unknown", "invalid")):
            # Operator-authored goal references are validated before planning
            # and explicitly deferred with scope="goal" by the controller.
            # A lookup string that misses during execution was chosen by the
            # tactical planner, so only that exact tactic is invalid.
            return "invalid_reference", "tactic", 0.98
        if any(marker in text for marker in ("not visible", "no player", "no target", "spawn", "offline")):
            return "world_unavailable", "goal", 0.85
        if any(marker in text for marker in ("unknown spell", "unknown skill", "cannot use", "can't use", "missing")):
            return "missing_capability", "goal", 0.82
        # A composite combat tool may fail before combat begins. Preserve the
        # concrete failed stage instead of treating every pvp_seek failure as
        # evidence that the character needs more HP or equipment.
        if tool in ROUTE_TOOLS or (tool == "act" and "\"verb\":\"go\"" in text) or any(
            marker in text
            for marker in (
                "route",
                "exit",
                "arrive",
                "travel",
                "no floor",
                "boundary",
                "failed hop",
                "could not reach",
                "pvp.search.failed",
            )
        ):
            return "route_unavailable", "tactic", 0.92
        if any(marker in text for marker in ("timeout", "connection", "unavailable", "dependency", "broker")):
            return "dependency_failure", "tactic", 0.8
        if tool in COMBAT_TOOLS or "survival" in event_kind or any(marker in text for marker in ("low health", "critical health", "too weak", "flee")):
            return "insufficient_combat_power", "goal", 0.9
        return "ineffective_tactic", "tactic", 0.72

    def _retry_when(self, classification: str, profile: dict[str, Any]) -> dict[str, Any]:
        created_at = timestamp()
        if classification == "invalid_reference":
            return {"mode": "any", "conditions": [{"kind": "corpus_changed", "from": profile["corpus_version"]}]}
        if classification in {"insufficient_combat_power", "missing_capability"}:
            return {
                "mode": "any",
                "conditions": [
                    {"kind": "numeric_increase", "field": "max_health", "from": profile.get("max_health")},
                    {"kind": "numeric_increase", "field": "max_mana", "from": profile.get("max_mana")},
                    {"kind": "component_improved", "field": "equipment", "from": profile["equipment"]},
                    {"kind": "component_improved", "field": "attributes", "from": profile["attributes"]},
                    {"kind": "component_improved", "field": "skills", "from": profile["skills"]},
                    {"kind": "component_improved", "field": "abilities", "from": profile["abilities"]},
                    {"kind": "numeric_increase", "field": "healing_supply_count", "from": profile["healing_supply_count"]},
                ],
            }
        if classification == "route_unavailable":
            return {
                "mode": "any",
                "conditions": [
                    {"kind": "location_changed", "from": profile["room"]},
                    {"kind": "corpus_changed", "from": profile["corpus_version"]},
                ],
            }
        cooldown = self.config.world_retry_cooldown_seconds if classification == "world_unavailable" else self.config.generic_retry_cooldown_seconds
        return {
            "mode": "any",
            "conditions": [
                {"kind": "capability_changed", "from": profile["capability_hash"]},
                {"kind": "cooldown_elapsed", "seconds": cooldown, "since": created_at},
            ],
        }

    @staticmethod
    def _is_farm_survivability_failure(
        failed_tactic: Any, summary: Any = "", event_kind: Any = ""
    ) -> bool:
        if not isinstance(failed_tactic, dict):
            return False
        arguments = failed_tactic.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        if str(failed_tactic.get("tool") or "") != "autopilot" or not (
            str(arguments.get("mode") or "").casefold() == "farm"
            or arguments.get("assigned_room") is not None
        ):
            return False
        text = f"{summary} {event_kind}".casefold()
        return any(
            marker in text
            for marker in (
                "survivability",
                "retreat episodes",
                "safe-spot failure",
                "safe spot failure",
                "healing margin",
                "flee threshold",
                "observed a death",
            )
        )

    @staticmethod
    def _parse_time(value: str) -> float:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _structured_component_improved(cls, before: Any, current: Any) -> bool:
        """Return true only for a monotonic gain in a structured capability.

        Hash inequality is directionless: losing a weapon after death changes the
        equipment hash just as surely as acquiring armor.  Retry gates must only
        unlock on evidence that is at least as capable as the failed state and
        strictly better in one respect.
        """

        if isinstance(before, bool) or isinstance(current, bool):
            return False
        if isinstance(before, (int, float)) and isinstance(current, (int, float)):
            return current > before
        if isinstance(before, dict) and isinstance(current, dict):
            improved = False
            for key, before_value in before.items():
                if key not in current:
                    return False
                current_value = current[key]
                if isinstance(before_value, (dict, list)):
                    if cls._structured_component_regressed(before_value, current_value):
                        return False
                    improved = improved or cls._structured_component_improved(
                        before_value, current_value
                    )
                elif (
                    not isinstance(before_value, bool)
                    and not isinstance(current_value, bool)
                    and isinstance(before_value, (int, float))
                    and isinstance(current_value, (int, float))
                ):
                    if current_value < before_value:
                        return False
                    improved = improved or current_value > before_value
                elif current_value != before_value:
                    return False
            return improved or any(key not in before for key in current)
        if isinstance(before, list) and isinstance(current, list):
            before_values = {canonical_json(value) for value in before}
            current_values = {canonical_json(value) for value in current}
            return before_values < current_values
        return False

    @classmethod
    def _structured_component_regressed(cls, before: Any, current: Any) -> bool:
        """Return true when a structured capability has lost known state."""

        if isinstance(before, bool) or isinstance(current, bool):
            return before != current
        if isinstance(before, (int, float)) and isinstance(current, (int, float)):
            return current < before
        if isinstance(before, dict) and isinstance(current, dict):
            for key, before_value in before.items():
                if key not in current:
                    return True
                current_value = current[key]
                if isinstance(before_value, (dict, list)):
                    if cls._structured_component_regressed(before_value, current_value):
                        return True
                elif (
                    not isinstance(before_value, bool)
                    and not isinstance(current_value, bool)
                    and isinstance(before_value, (int, float))
                    and isinstance(current_value, (int, float))
                    and current_value < before_value
                ):
                    return True
                elif not isinstance(before_value, (int, float)) and current_value != before_value:
                    return True
            return False
        if isinstance(before, list) and isinstance(current, list):
            before_values = {canonical_json(value) for value in before}
            current_values = {canonical_json(value) for value in current}
            return not before_values <= current_values
        return before != current

    @classmethod
    def _component_improved(
        cls, field: str, failed_state: dict[str, Any], profile: dict[str, Any]
    ) -> bool:
        if field in {"equipment", "equipment_hash"}:
            before_values = {
                canonical_json(value)
                for value in cls._semantic_equipment(failed_state.get("equipment"))
            }
            current_values = {
                canonical_json(value)
                for value in cls._semantic_equipment(profile.get("equipment"))
            }
            return before_values < current_values
        source_field = field.removesuffix("_hash")
        if source_field in {"attributes", "skills", "abilities"}:
            return cls._structured_component_improved(
                failed_state.get(source_field), profile.get(source_field)
            )
        return False

    @classmethod
    def _capability_improved(
        cls, failed_state: dict[str, Any], profile: dict[str, Any]
    ) -> bool:
        """Compare durable capability facts instead of versioned aggregate hashes."""

        for field in ("max_health", "max_mana", "healing_supply_count"):
            before, current = failed_state.get(field), profile.get(field)
            if (
                isinstance(before, (int, float))
                and isinstance(current, (int, float))
                and current > before
            ):
                return True
        for field in ("equipment", "attributes", "skills", "abilities"):
            if field in failed_state and cls._component_improved(
                field, failed_state, profile
            ):
                return True
        return False

    def capability_improved(
        self, failed_state: dict[str, Any], observation: dict[str, Any]
    ) -> bool:
        """Compare a prior capability snapshot with a live observation."""

        return self._capability_improved(failed_state, self.profile(observation))

    @classmethod
    def _condition_met(
        cls,
        condition: dict[str, Any],
        profile: dict[str, Any],
        now: float,
        failed_state: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        kind = condition.get("kind")
        if kind == "numeric_increase":
            before, current = condition.get("from"), profile.get(str(condition.get("field")))
            met = isinstance(before, (int, float)) and isinstance(current, (int, float)) and current > before
            return met, f"{condition.get('field')} must increase above {before!r} (now {current!r})"
        if kind == "numeric_at_least":
            current, required = profile.get(str(condition.get("field"))), condition.get("value")
            met = isinstance(current, (int, float)) and isinstance(required, (int, float)) and current >= required
            return met, f"{condition.get('field')} must be at least {required!r} (now {current!r})"
        if kind == "component_changed":
            field = str(condition.get("field"))
            if (
                field in {
                    "equipment_hash",
                    "attributes_hash",
                    "skills_hash",
                    "abilities_hash",
                }
                and isinstance(failed_state, dict)
            ):
                met = cls._component_improved(field, failed_state, profile)
                return met, f"{field.removesuffix('_hash')} must improve without a known loss"
            current = profile.get(field)
            return current != condition.get("from"), f"{condition.get('field')} must change"
        if kind == "component_improved":
            field = str(condition.get("field"))
            met = bool(
                isinstance(failed_state, dict)
                and cls._component_improved(field, failed_state, profile)
            )
            return met, f"{field} must improve without a known loss"
        if kind == "capability_changed":
            if isinstance(failed_state, dict):
                return cls._capability_improved(failed_state, profile), "combat capability or equipment must improve"
            return profile.get("capability_hash") != condition.get("from"), "combat capability or equipment must change"
        if kind == "location_changed":
            return profile.get("room") != condition.get("from"), f"location must change from {condition.get('from')}"
        if kind == "corpus_changed":
            return profile.get("corpus_version") != condition.get("from"), "knowledge corpus must be updated"
        if kind == "cooldown_elapsed":
            remaining = max(0, int(float(condition.get("seconds", 0)) - (now - cls._parse_time(str(condition.get("since", ""))))))
            return remaining <= 0, f"retry cooldown has {remaining} second(s) remaining"
        return False, f"unknown retry condition {kind}"

    def evaluate_retry(self, lesson: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        profile = self.profile(observation)
        predicate = lesson.get("retry_when", {})
        conditions = predicate.get("conditions", []) if isinstance(predicate, dict) else []
        conditions = list(conditions) if isinstance(conditions, list) else []
        failed_state = lesson.get("failed_state", {})
        failed_tactic = failed_state.get("failed_tactic", {}) if isinstance(failed_state, dict) else {}
        if self._is_farm_survivability_failure(
            failed_tactic, lesson.get("summary")
        ) and any(
            isinstance(item, dict) and item.get("kind") == "cooldown_elapsed"
            for item in conditions
        ):
            # Migrate legacy exact-farm safety lessons. A timeout can make a
            # transient tactic worth another try; it cannot make unchanged
            # combat readiness survive a room that repeatedly forced retreat.
            conditions = [
                item
                for item in conditions
                if isinstance(item, dict) and item.get("kind") != "cooldown_elapsed"
            ] or [
                {
                    "kind": "capability_changed",
                    "from": failed_state.get("capability_hash"),
                }
            ]
        # Time alone may justify retrying a transient or merely ineffective
        # tactic. It can never erase a verified death in the exact farm room.
        # Once that stronger evidence exists, require a real capability gain.
        if any(
            isinstance(item, dict) and item.get("kind") == "cooldown_elapsed"
            for item in conditions
        ) and self._farm_tactic_has_later_death(lesson, failed_state, failed_tactic):
            conditions = [
                {
                    "kind": "capability_changed",
                    "from": failed_state.get("capability_hash"),
                }
            ]
        # Lessons created before healing supplies were modeled cannot contain a
        # supply predicate. Treat their missing baseline as zero so a newly
        # observed carried flask is a concrete readiness change, not a prose
        # promise or a way to evade a failed goal.
        condition_fields = {
            str(item.get("field"))
            for item in conditions
            if isinstance(item, dict) and item.get("field") is not None
        }
        if (
            lesson.get("classification") in {"insufficient_combat_power", "missing_capability"}
            and "healing_supply_count" not in condition_fields
        ):
            baseline = failed_state.get("healing_supply_count", 0) if isinstance(failed_state, dict) else 0
            conditions.append(
                {"kind": "numeric_increase", "field": "healing_supply_count", "from": baseline}
            )
        economic_reason = str(lesson.get("summary") or "")
        economic_baseline = failed_state.get("carried_currency", 0) if isinstance(failed_state, dict) else 0
        if isinstance(failed_tactic, dict) and failed_tactic.get("tool") == "shop":
            legacy_actions = self.storage.get_runtime("blocked_actions", [])
            for action in legacy_actions if isinstance(legacy_actions, list) else []:
                if not isinstance(action, dict) or not isinstance(action.get("arguments"), dict):
                    continue
                action_observation = {"look": {"room": action.get("room")}}
                if self.tactic_key("shop", action["arguments"], action_observation) != lesson.get("tactic_key"):
                    continue
                economic_reason += " " + str(action.get("reason") or "")
                economic_baseline = action.get("carried_currency", economic_baseline) or 0
                break
        if (
            isinstance(failed_tactic, dict)
            and failed_tactic.get("tool") == "shop"
            and any(
                marker in economic_reason.casefold()
                for marker in ("enough money", "insufficient fund", "cannot afford")
            )
            and "carried_currency" not in condition_fields
        ):
            # Migrate pre-currency economic lessons.  Their failed state lacks a
            # cash baseline, so zero is the only safe lower bound; once money is
            # visibly carried the stale unaffordable-purchase lesson may unlock.
            conditions.append(
                {"kind": "numeric_increase", "field": "carried_currency", "from": economic_baseline}
            )
        if (
            isinstance(failed_tactic, dict)
            and failed_tactic.get("tool") == "shop"
            and is_inventory_capacity_refusal(economic_reason)
            and "inventory_load_hash" not in condition_fields
        ):
            # Replace the old generic cooldown/capability predicate with the
            # state that actually controls this refusal.  A legacy lesson has
            # no baseline and is allowed one re-evaluation; a fresh refusal is
            # then persisted with the new inventory-load fingerprint.
            conditions = [
                {
                    "kind": "component_changed",
                    "field": "inventory_load_hash",
                    "from": failed_state.get("inventory_load_hash", "legacy-unknown"),
                }
            ]
        results = []
        for condition in conditions:
            if isinstance(condition, dict):
                met, detail = self._condition_met(
                    condition,
                    profile,
                    time.time(),
                    failed_state if isinstance(failed_state, dict) else None,
                )
                results.append({"met": met, "detail": detail, "condition": condition})
        failed_room = failed_state.get("room") if isinstance(failed_state, dict) else None
        failed_tool = str(failed_tactic.get("tool") or "") if isinstance(failed_tactic, dict) else ""
        failed_arguments = (
            failed_tactic.get("arguments", {}) if isinstance(failed_tactic, dict) else {}
        )
        equipment_failure_is_location_independent = failed_tool in {
            "equip_best",
            "wear_best",
        } or (
            failed_tool == "act"
            and isinstance(failed_arguments, dict)
            and failed_arguments.get("verb") == "use"
            and any(
                marker in str(lesson.get("summary") or "").casefold()
                for marker in ("broken", "can't use", "cannot use", "unusable")
            )
        )
        farm_failure_is_destination_bound = (
            failed_tool == "autopilot"
            and isinstance(failed_arguments, dict)
            and (
                str(failed_arguments.get("mode") or "").casefold() == "farm"
                or failed_arguments.get("assigned_room") is not None
            )
        )
        if (
            lesson.get("scope") == "tactic"
            and failed_room
            and not equipment_failure_is_location_independent
            and not farm_failure_is_destination_bound
        ):
            location_changed = profile.get("room") != failed_room
            results.append(
                {
                    "met": location_changed,
                    "detail": f"tactic location must change from {failed_room} (now {profile.get('room')})",
                    "condition": {"kind": "tactic_location_changed", "from": failed_room},
                }
            )
        mode = predicate.get("mode", "any") if isinstance(predicate, dict) else "any"
        met = bool(results) and (all(item["met"] for item in results) if mode == "all" else any(item["met"] for item in results))
        return {"met": met, "mode": mode, "conditions": results}

    def _farm_tactic_has_later_death(
        self,
        lesson: dict[str, Any],
        failed_state: Any,
        failed_tactic: Any,
    ) -> bool:
        """Return whether stronger death evidence supersedes a farm cooldown."""

        if not isinstance(failed_state, dict) or not isinstance(failed_tactic, dict):
            return False
        arguments = failed_tactic.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        if str(failed_tactic.get("tool") or "") != "autopilot" or not (
            str(arguments.get("mode") or "").casefold() == "farm"
            or arguments.get("assigned_room") is not None
        ):
            return False
        room = arguments.get("assigned_room")
        target = self._normal_text(arguments.get("hunt") or arguments.get("target") or "")
        created_at = self._parse_time(str(lesson.get("created_at") or ""))
        history = self.storage.get_runtime(COMBAT_MEMORY_KEY, [])
        for outcome in history if isinstance(history, list) else []:
            if not isinstance(outcome, dict) or outcome.get("died") is not True:
                continue
            occurred_at = self._parse_time(str(outcome.get("occurred_at") or ""))
            if created_at and occurred_at and occurred_at < created_at:
                continue
            outcome_room = outcome.get("room")
            if isinstance(outcome_room, dict):
                outcome_room = outcome_room.get("id", outcome_room.get("num"))
            if room is not None and str(outcome_room) != str(room):
                continue
            outcome_target = self._normal_text(outcome.get("target") or "")
            if target and outcome_target and target != outcome_target:
                continue
            return True
        return False

    def repair_regressive_capability_unlocks(
        self, observation: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Re-defer legacy lessons unlocked by a loss masquerading as progress."""

        repaired: list[dict[str, Any]] = []
        quarantines_raw = self.storage.get_runtime("farm_tactic_quarantine_v1", {})
        quarantines = (
            dict(quarantines_raw) if isinstance(quarantines_raw, dict) else {}
        )
        quarantines_changed = False
        for lesson in self.storage.goal_lessons(statuses=["unlocked"], limit=200):
            failed_state = lesson.get("failed_state")
            failed_state = failed_state if isinstance(failed_state, dict) else {}
            failed_tactic = failed_state.get("failed_tactic")
            failed_tactic = failed_tactic if isinstance(failed_tactic, dict) else {}
            if (
                lesson.get("classification") == "ineffective_tactic"
                and lesson.get("scope") == "tactic"
                and is_obsolete_farm_recovery_failure(lesson.get("summary"))
                and not self._farm_tactic_has_later_death(
                    lesson, failed_state, failed_tactic
                )
            ):
                self.storage.update_goal_lesson(
                    lesson["id"],
                    "resolved",
                    evidence={
                        "repair": (
                            "ordinary farm recovery is not a capability failure "
                            "and cannot re-quarantine the tactic"
                        ),
                        "at": timestamp(),
                    },
                )
                continue
            predicate = lesson.get("retry_when")
            conditions = (
                predicate.get("conditions", []) if isinstance(predicate, dict) else []
            )
            if not any(
                isinstance(condition, dict)
                and condition.get("kind")
                in {"capability_changed", "component_changed", "component_improved"}
                for condition in conditions
            ):
                continue
            if lesson.get("resolution_goal_id") and lesson.get("scope") == "goal":
                continue
            evaluation = self.evaluate_retry(lesson, observation)
            if evaluation.get("met"):
                continue
            updated = self.storage.update_goal_lesson(
                lesson["id"],
                "deferred",
                evidence={
                    "repair": (
                        "a directionless capability fingerprint changed because "
                        "known readiness was lost, not improved"
                    ),
                    "retry_evaluation": evaluation,
                    "at": timestamp(),
                },
            )
            repaired.append(updated)

            identity = self._farm_lesson_identity(updated)
            if identity is None:
                continue
            room, target, safe_spots = identity
            existing = next(
                (
                    record
                    for _key, record in farm_quarantine_entries(
                        quarantines, room=room
                    )
                    if farm_quarantine_matches(
                        record,
                        room=room,
                        target=target,
                        use_safe_spots=safe_spots,
                    )
                ),
                None,
            )
            if isinstance(existing, dict):
                continue
            failed_state = updated.get("failed_state")
            failed_state = failed_state if isinstance(failed_state, dict) else {}
            failed_tactic = failed_state.get("failed_tactic")
            failed_tactic = failed_tactic if isinstance(failed_tactic, dict) else {}
            failed_room = failed_state.get("room")
            failed_room_id = (
                failed_room.get("id", failed_room.get("num"))
                if isinstance(failed_room, dict)
                else None
            )
            at_assigned_room = (
                failed_room_id is None or str(failed_room_id) == str(room)
            )
            tactic_id = farm_tactic_key(room, target, safe_spots)
            record = {
                "room": int(room) if room.isdigit() else room,
                "assigned_room": int(room) if room.isdigit() else room,
                "at_assigned_room": at_assigned_room,
                "target": target,
                "use_safe_spots": safe_spots,
                "quarantined_at": timestamp(),
                "goal_id": updated.get("goal_id"),
                "reasons": [str(updated.get("summary") or "unsafe farm tactic")],
                "quarantined": at_assigned_room,
                "lesson_id": updated.get("id"),
                "guidance": (
                    "Do not reuse this room/prey farm unchanged; choose another "
                    "grounded room until combat readiness verifiably improves."
                ),
            }
            record["tactic_id"] = tactic_id
            evidence_identity = farm_quarantine_evidence_identity(record)
            record["evidence_class"] = evidence_identity["class"]
            record["evidence_scope"] = evidence_identity["scope"]
            record["evidence_fingerprint"] = json_hash(
                {"tactic_id": tactic_id, "disposition": evidence_identity}
            )
            quarantines[tactic_id] = record
            quarantines_changed = True
        if quarantines_changed:
            self.storage.set_runtime("farm_tactic_quarantine_v1", quarantines)
        if repaired:
            self.storage.emit_event(
                "goal_learning.false_unlock_repaired",
                "Repaired capability lessons unlocked by readiness loss",
                severity="notice",
                interesting=True,
                data={
                    "lesson_ids": [item.get("id") for item in repaired],
                    "farm_rooms_requarantined": [
                        self._farm_lesson_identity(item)[0]
                        for item in repaired
                        if self._farm_lesson_identity(item) is not None
                    ],
                },
            )
        return repaired

    @staticmethod
    def _farm_lesson_identity(
        lesson: dict[str, Any],
    ) -> tuple[str, str, bool | None] | None:
        """Return the quarantine identity owned by an exact farm lesson."""

        if lesson.get("scope") != "tactic":
            return None
        failed_state = lesson.get("failed_state")
        failed_state = failed_state if isinstance(failed_state, dict) else {}
        failed_tactic = failed_state.get("failed_tactic")
        failed_tactic = failed_tactic if isinstance(failed_tactic, dict) else {}
        if str(failed_tactic.get("tool") or "") != "autopilot":
            return None
        arguments = failed_tactic.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        room = arguments.get("assigned_room")
        if room is None:
            return None
        target = " ".join(
            str(arguments.get("hunt") or arguments.get("target") or "")
            .casefold()
            .split()
        )
        safe_spots = arguments.get("use_safe_spots")
        return str(room), target, safe_spots if isinstance(safe_spots, bool) else None

    def _release_matching_farm_quarantine(
        self,
        lesson: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any] | None:
        """Keep an unlocked exact tactic lesson and its runtime gate consistent."""

        identity = self._farm_lesson_identity(lesson)
        if identity is None:
            return None
        room, target, safe_spots = identity
        raw = self.storage.get_runtime("farm_tactic_quarantine_v1", {})
        quarantines = dict(raw) if isinstance(raw, dict) else {}
        lesson_goal_id = str(lesson.get("goal_id") or "")
        lesson_release_at = self._parse_time(
            str(
                lesson.get("unlocked_at")
                or lesson.get("resolved_at")
                or lesson.get("updated_at")
                or ""
            )
        )
        matching: list[tuple[str, dict[str, Any]]] = []
        for key, record in farm_quarantine_entries(quarantines, room=room):
            if not farm_quarantine_matches(
                record,
                room=room,
                target=target,
                use_safe_spots=safe_spots,
            ):
                continue
            record_goal_id = str(record.get("goal_id") or "")
            if record_goal_id and lesson_goal_id and record_goal_id != lesson_goal_id:
                continue
            quarantined_at = self._parse_time(
                str(record.get("quarantined_at") or "")
            )
            if quarantined_at and lesson_release_at < quarantined_at:
                # An older unlocked lesson cannot erase newer contradictory
                # safety evidence for the same exact tactic.
                continue
            matching.append((key, record))
        if not matching:
            return None

        released_records = []
        for key, record in matching:
            released_records.append(
                {
                    **record,
                    "released_at": timestamp(),
                    "release_reason": reason,
                    "lesson_id": lesson.get("id"),
                }
            )
            quarantines.pop(key, None)
        released = released_records[-1]
        self.storage.set_runtime("farm_tactic_quarantine_v1", quarantines)
        raw_retreats = self.storage.get_runtime(
            "farm_tactic_retreat_incidents_v1", {}
        )
        retreats = dict(raw_retreats) if isinstance(raw_retreats, dict) else {}
        filtered_retreats = {
            key: value
            for key, value in retreats.items()
            if not (
                isinstance(value, dict)
                and str(value.get("assigned_room")) == room
                and (
                    not target
                    or not str(value.get("target") or "").strip()
                    or str(value.get("target") or "").strip().casefold() == target
                )
                and (
                    not isinstance(safe_spots, bool)
                    or not isinstance(value.get("use_safe_spots"), bool)
                    or value.get("use_safe_spots") == safe_spots
                )
                and (
                    not lesson_goal_id
                    or not str(value.get("goal_id") or "")
                    or str(value.get("goal_id")) == lesson_goal_id
                )
            )
        }
        if len(filtered_retreats) != len(retreats):
            self.storage.set_runtime(
                "farm_tactic_retreat_incidents_v1", filtered_retreats
            )
        suppression = self.storage.get_runtime("safety_suppression_v1")
        if isinstance(suppression, dict) and "quarantined_farm_tactic" in suppression.get(
            "blocker_kinds", []
        ):
            self.storage.set_runtime("safety_suppression_v1", None)
        self.storage.emit_event(
            "background_farm.quarantine_released",
            "Released a farm quarantine whose exact tactic lesson unlocked",
            severity="notice",
            interesting=False,
            goal_id=lesson_goal_id or None,
            data={
                "room": record.get("room", room),
                "target": record.get("target"),
                "use_safe_spots": record.get("use_safe_spots"),
                "lesson_id": lesson.get("id"),
                "reason": reason,
            },
        )
        return released

    def release_unlocked_farm_quarantines(self) -> list[dict[str, Any]]:
        """Repair runtime gates left behind by lessons unlocked before this build."""

        released: list[dict[str, Any]] = []
        for lesson in self.storage.goal_lessons(
            statuses=["unlocked", "resolved"], limit=200
        ):
            record = self._release_matching_farm_quarantine(
                lesson,
                reason=f"matching farm lesson is {lesson.get('status')}",
            )
            if record is not None:
                released.append(record)
        return released

    def refresh_unlocks(self, observation: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.config.enabled:
            return []
        unlocked = []
        for lesson in self.storage.goal_lessons(statuses=["deferred"], limit=200):
            evaluation = self.evaluate_retry(lesson, observation)
            if evaluation["met"]:
                updated = self.storage.update_goal_lesson(
                    lesson["id"], "unlocked", evidence=evaluation
                )
                self._release_matching_farm_quarantine(
                    updated,
                    reason="matching farm lesson retry predicate was satisfied",
                )
                unlocked.append(updated)
        return unlocked

    def _equivalent_goal_lessons(
        self,
        family: str,
        *,
        statuses: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Find lessons by current semantics, including pre-migration family ids."""
        values: list[dict[str, Any]] = []
        for lesson in self.storage.goal_lessons(statuses=statuses, limit=200):
            if lesson.get("goal_family") == family:
                values.append(lesson)
                continue
            original = self.storage.goal(str(lesson.get("goal_id") or ""))
            if original and self.goal_family(original) == family:
                values.append(lesson)
        return values[:limit]

    def submission_review(self, goal: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        family = self.goal_family(goal)
        if not self.config.enabled:
            return {"allowed": True, "goal_family": family}
        lessons = self._equivalent_goal_lessons(
            family,
            statuses=["deferred", "unlocked"],
            limit=50,
        )
        for lesson in lessons:
            original = self.storage.goal(str(lesson.get("goal_id") or ""))
            if (
                lesson.get("scope") == "goal"
                and original
                and original.get("status") in {"queued", "active", "paused"}
            ):
                return {
                    "allowed": False,
                    "code": "GOAL_ALREADY_OPEN",
                    "goal_family": family,
                    "lesson": self.public_lesson(
                        lesson,
                        evaluation=self.evaluate_retry(lesson, observation),
                    ),
                    "suggested_goals": [],
                    "message": (
                        f"The original strategic goal is already {original['status']}; "
                        "supervise or reprioritize it instead of creating a retry."
                    ),
                }
            if lesson["status"] == "deferred" and lesson["scope"] == "goal":
                evaluation = self.evaluate_retry(lesson, observation)
                if evaluation["met"]:
                    lesson = self.storage.update_goal_lesson(lesson["id"], "unlocked", evidence=evaluation)
                else:
                    return {
                        "allowed": False,
                        "code": "GOAL_DEFERRED",
                        "goal_family": family,
                        "lesson": self.public_lesson(lesson, evaluation=evaluation),
                        "suggested_goals": lesson.get("suggested_goals", []),
                        "message": "Do not reissue or paraphrase this goal. Pursue a supporting goal until retry conditions are met.",
                    }
            if lesson["status"] == "unlocked" and lesson["scope"] == "goal":
                retry_goal = self.storage.goal(str(lesson.get("resolution_goal_id") or ""))
                if retry_goal and retry_goal.get("status") in {"queued", "active", "paused"}:
                    evaluation = {"met": True, "mode": "already_started", "conditions": []}
                    return {
                        "allowed": False,
                        "code": "GOAL_DEFERRED",
                        "goal_family": family,
                        "lesson": self.public_lesson(lesson, evaluation=evaluation),
                        "suggested_goals": [],
                        "message": f"A linked retry is already {retry_goal['status']}; supervise it instead of creating a duplicate.",
                    }
                return {
                    "allowed": True,
                    "goal_family": family,
                    "retry_of_goal_id": lesson["goal_id"],
                    "lesson_id": lesson["id"],
                    "retry_note": "Prerequisite change verified; use a revised tactic and let deterministic criteria verify success.",
                }
        return {"allowed": True, "goal_family": family}

    def require_goal_eligible(self, goal: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        review = self.submission_review(goal, observation)
        if review["allowed"]:
            return review
        self.storage.emit_event(
            "goal.reissue_suppressed",
            "Suppressed an equivalent goal whose retry prerequisites are not met",
            severity="warning",
            interesting=True,
            goal_id=review["lesson"].get("goal_id"),
            data=review,
        )
        raise GoalDeferredError(review)

    def check_action(self, tool: str, arguments: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any] | None:
        key = self.tactic_key(tool, arguments, observation)
        for lesson in self.storage.goal_lessons(statuses=["deferred"], limit=200):
            if lesson["scope"] != "tactic" or lesson["tactic_key"] != key:
                continue
            evaluation = self.evaluate_retry(lesson, observation)
            if evaluation["met"]:
                updated = self.storage.update_goal_lesson(
                    lesson["id"], "unlocked", evidence=evaluation
                )
                self._release_matching_farm_quarantine(
                    updated,
                    reason="matching farm lesson retry predicate was satisfied",
                )
                return None
            return {"lesson": self.public_lesson(lesson, evaluation=evaluation), "tactic_key": key}
        return None

    @staticmethod
    def _suggested_goals(classification: str, profile: dict[str, Any]) -> list[str]:
        if classification in {"insufficient_combat_power", "missing_capability"}:
            return [
                f"Raise max HP above {profile.get('max_health')} through safer progression, then satisfy any explicit finish criteria.",
                "Acquire or improve combat equipment, a relevant trained skill, or verified healing supplies, then satisfy any explicit finish criteria.",
            ]
        if classification == "invalid_reference":
            return ["Choose a verified destination or target from knowledge_search; do not invent aliases."]
        if classification == "route_unavailable":
            return ["Reach a connected source-verified safe staging room using verified numeric exits, then satisfy any explicit finish criteria."]
        if classification == "world_unavailable":
            return ["Pursue safe progression while waiting for the required player, NPC, or world condition."]
        return ["Choose a materially different tactic or supporting progression goal before retrying."]

    def defer_goal(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        *,
        tool: str = "",
        arguments: dict[str, Any] | None = None,
        reason: str,
        event_kind: str = "",
        evidence_event_ids: list[str] | None = None,
        classification: str | None = None,
        scope: str | None = None,
        block: bool | None = None,
        retry_when: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        classified, inferred_scope, confidence = self.classify(tool, reason, event_kind=event_kind)
        if tool == "act" and isinstance(arguments, dict) and arguments.get("verb") == "go":
            classified, inferred_scope, confidence = "route_unavailable", "tactic", 0.9
        classification = classification or classified
        scope = scope or inferred_scope
        # Lessons constrain future decisions; they never own the strategic-goal
        # lifecycle.  Earlier builds treated a goal-scoped lesson as authority to
        # transition the active goal to ``blocked``.  That made one combat, route,
        # merchant, or world-state conclusion strand the outcome instead of
        # letting campaign management choose another tactic or supporting phase.
        # Keep ``block`` in the signature for stored callers and older extensions,
        # but deliberately ignore it. Lifecycle handling, such as a recoverable
        # pause for an invalid contract or ambiguous mutation, belongs to the
        # controller rather than to a learned failure classification.
        block = False
        if classification not in FAILURE_CLASSES:
            raise ValueError(f"unknown goal failure classification {classification}")
        profile = self.profile(observation)
        profile["failed_tactic"] = {
            "tool": tool or event_kind or "goal",
            "arguments": arguments or {},
            "room": profile.get("room"),
        }
        if retry_when is None and self._is_farm_survivability_failure(
            profile["failed_tactic"], reason, event_kind
        ):
            # This remains tactic-scoped, so other rooms and prey stay usable.
            # The exact unsafe farm has no time-only unlock: require a measured
            # readiness gain before repeating it unchanged.
            retry_when = self._retry_when("insufficient_combat_power", profile)
        if (
            retry_when is None
            and tool == "shop"
            and any(marker in reason.casefold() for marker in ("enough money", "insufficient fund", "cannot afford"))
        ):
            retry_when = {
                "mode": "any",
                "conditions": [
                    {"kind": "numeric_increase", "field": "carried_currency", "from": profile["carried_currency"]}
                ],
            }
        if (
            retry_when is None
            and tool in {"shop", "cast"}
            and is_inventory_capacity_refusal(reason)
        ):
            retry_when = {
                "mode": "any",
                "conditions": [
                    {
                        "kind": "component_changed",
                        "field": "inventory_load_hash",
                        "from": profile["inventory_load_hash"],
                    }
                ],
            }
        tactic = self.tactic_key(tool or event_kind or "goal", arguments or {}, observation)
        lesson = self.storage.create_goal_lesson(
            {
                "goal_id": goal["id"],
                "goal_family": self.goal_family(goal),
                "tactic_key": tactic,
                "classification": classification,
                "scope": scope,
                "confidence": confidence,
                "summary": reason,
                "failed_state": profile,
                "evidence_event_ids": evidence_event_ids or [],
                "retry_when": retry_when or self._retry_when(classification, profile),
                "suggested_goals": self._suggested_goals(classification, profile),
            }
        )
        return {
            "lesson": lesson,
            "goal": None,
            "goal_blocked": False,
            "strategic_goal_preserved": goal.get("status") == "active",
        }

    def maybe_defer(
        self,
        goal: dict[str, Any],
        observation: dict[str, Any],
        *,
        tool: str,
        arguments: dict[str, Any],
        reason: str,
        event: dict[str, Any],
    ) -> dict[str, Any] | None:
        history = self.storage.goal_events(
            goal["id"],
            kinds=["action.no_progress", "action.failed", "action.succeeded"],
            limit=200,
        )
        cutoff = time.time() - float(self.config.failure_evidence_window_seconds)
        recent = [
            item
            for item in history
            if self._parse_time(str(item.get("occurred_at") or "")) >= cutoff
        ]
        last_progress_at = max(
            [
                self._parse_time(str(item.get("occurred_at") or ""))
                for item in recent
                if item.get("kind") == "action.succeeded"
            ]
            or [cutoff]
        )
        relevant = [
            item
            for item in recent
            if item.get("kind") in {"action.no_progress", "action.failed"}
            and self._parse_time(str(item.get("occurred_at") or "")) > last_progress_at
        ]
        key = self.tactic_key(tool, arguments, observation)
        matching = [
            item
            for item in relevant
            if isinstance(item.get("data", {}).get("room"), (dict, str, int))
            and self.tactic_key(
                str(item.get("data", {}).get("tool", "")),
                item.get("data", {}).get("arguments", {}),
                {"look": {"room": item["data"]["room"]}},
            ) == key
        ]
        if len(matching) >= self.config.repeated_tactic_budget:
            return self.defer_goal(
                goal,
                observation,
                tool=tool,
                arguments=arguments,
                reason=f"Repeated tactic failed to advance the goal: {reason}",
                event_kind=event["kind"],
                evidence_event_ids=[item["id"] for item in matching[-20:]],
                scope="tactic",
            )
        if len(relevant) >= self.config.no_progress_budget:
            tactic_families = {
                self.tactic_family_key(
                    str(item.get("data", {}).get("tool", "")),
                    item.get("data", {}).get("arguments", {}),
                    {"look": {"room": item.get("data", {}).get("room")}},
                )
                for item in relevant
                if isinstance(item.get("data", {}).get("arguments", {}), dict)
                and item.get("data", {}).get("room") is not None
            }
            # Exhaustion means several materially different approaches failed, not
            # the same portal coordinate retried with fine movement or more steps.
            if len(tactic_families) < max(2, self.config.repeated_tactic_budget):
                return self.defer_goal(
                    goal,
                    observation,
                    tool=tool,
                    arguments=arguments,
                    reason=f"Repeated tactic family failed without exhausting distinct alternatives: {reason}",
                    event_kind=event["kind"],
                    evidence_event_ids=[item["id"] for item in relevant[-20:]],
                    scope="tactic",
                )
            # Banking, routing, shopping, equipment, and evidence gathering are
            # preparation tactics. Their failure can disprove an exact tactic,
            # but it cannot prove that an HP/campaign outcome is impossible or
            # that the character must become stronger. Keep those lessons tactic-local
            # even after the aggregate failure budget is exhausted.
            preparation_only = tool in RECOVERABLE_PREPARATION_TOOLS
            return self.defer_goal(
                goal,
                observation,
                tool=tool,
                arguments=arguments,
                reason=(
                    f"Preparation failure budget exhausted without verified goal progress: {reason}"
                    if preparation_only
                    else f"Failure budget exhausted without verified goal progress: {reason}"
                ),
                event_kind=event["kind"],
                evidence_event_ids=[item["id"] for item in relevant[-20:]],
                scope="tactic" if preparation_only else "goal",
            )
        return None

    def repair_preparation_goal_lessons(self) -> list[dict[str, Any]]:
        """Resolve legacy whole-goal gates inferred from preparation failures."""
        repaired: list[dict[str, Any]] = []
        for lesson in self.storage.goal_lessons(
            statuses=["deferred", "unlocked"], limit=200
        ):
            if lesson.get("scope") != "goal":
                continue
            failed_state = lesson.get("failed_state", {})
            failed_tactic = (
                failed_state.get("failed_tactic", {})
                if isinstance(failed_state, dict)
                else {}
            )
            tool = str(failed_tactic.get("tool") or "")
            summary = str(lesson.get("summary") or "").casefold()
            preparation_evidence = tool in RECOVERABLE_PREPARATION_TOOLS or any(
                marker in summary
                for marker in (
                    "deposit needs a positive amount",
                    "carried shillings did not move",
                    "own position unknown",
                )
            )
            if not preparation_evidence or lesson.get("classification") not in {
                "ineffective_tactic",
                "route_unavailable",
                "dependency_failure",
            }:
                continue
            repaired_lesson = self.storage.update_goal_lesson(
                lesson["id"],
                "resolved",
                evidence={
                    "repair": (
                        "preparation failure is tactic-scoped and no longer gates the campaign outcome"
                    ),
                    "failed_tool": tool or None,
                    "at": timestamp(),
                },
            )
            repaired.append(repaired_lesson)
        return repaired

    def backfill(self, observation: dict[str, Any]) -> list[dict[str, Any]]:
        created = []
        existing_goals = {lesson["goal_id"] for lesson in self.storage.goal_lessons(limit=200)}
        for goal in self.storage.goals(["blocked", "failed"]):
            if goal["id"] in existing_goals:
                continue
            blocked_reason = str(goal.get("blocked_reason") or "")
            if blocked_reason == "invalid_game_reference":
                classification, scope = "invalid_reference", "goal"
            elif blocked_reason in {"prerequisite_not_met", "world_unavailable"}:
                classification, scope = (
                    ("world_unavailable", "goal") if blocked_reason == "world_unavailable" else ("insufficient_combat_power", "goal")
                )
            elif blocked_reason == "repeated_non_progress":
                classification, scope = "ineffective_tactic", "tactic"
            else:
                continue
            evidence = self.storage.goal_events(goal["id"], limit=20)
            created.append(
                self.defer_goal(
                    goal,
                    observation,
                    reason=f"Recovered prior blocked goal: {blocked_reason.replace('_', ' ')}",
                    event_kind="goal.backfill",
                    evidence_event_ids=[item["id"] for item in evidence[-10:]],
                    classification=classification,
                    scope=scope,
                    block=False,
                )["lesson"]
            )
        return created

    def record_success(self, goal: dict[str, Any]) -> list[dict[str, Any]]:
        family = self.goal_family(goal)
        resolved = []
        for lesson in self._equivalent_goal_lessons(
            family,
            statuses=["deferred", "unlocked"],
            limit=200,
        ):
            resolved.append(
                self.storage.update_goal_lesson(
                    lesson["id"],
                    "resolved",
                    resolution_goal_id=goal["id"],
                )
            )
        return resolved

    def public_lesson(self, lesson: dict[str, Any], *, evaluation: dict[str, Any] | None = None) -> dict[str, Any]:
        value = {
            key: lesson.get(key)
            for key in (
                "id", "goal_id", "goal_family", "classification", "scope", "status", "summary",
                "retry_when", "suggested_goals", "created_at", "unlocked_at", "resolution_goal_id",
            )
        }
        original = self.storage.goal(str(lesson.get("goal_id") or ""))
        if original:
            value["original_goal"] = {
                key: original.get(key)
                for key in ("id", "title", "objective", "success_criteria", "constraints", "priority", "status")
            }
        failed_state = lesson.get("failed_state", {})
        if isinstance(failed_state, dict) and isinstance(failed_state.get("failed_tactic"), dict):
            value["failed_tactic"] = failed_state["failed_tactic"]
        elif lesson.get("tactic_key"):
            legacy_actions = self.storage.get_runtime("blocked_actions", [])
            for action in legacy_actions if isinstance(legacy_actions, list) else []:
                if not isinstance(action, dict) or not isinstance(action.get("arguments"), dict):
                    continue
                legacy_observation = {"look": {"room": action.get("room")}}
                if self.tactic_key(str(action.get("tool", "")), action["arguments"], legacy_observation) == lesson["tactic_key"]:
                    value["failed_tactic"] = {
                        "tool": action.get("tool"),
                        "arguments": action.get("arguments"),
                        "room": action.get("room"),
                    }
                    break
        if evaluation is not None:
            value["retry_evaluation"] = evaluation
        return value

    def context_for(self, goal: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        family = self.goal_family(goal)
        goal_lessons = self._equivalent_goal_lessons(
            family,
            statuses=["deferred", "unlocked"],
            limit=20,
        )
        tactic_lessons = self.storage.goal_lessons(statuses=["deferred"], limit=50)
        return {
            "goal_family": family,
            "instructions": "Do not repeat a deferred tactic unchanged. If the whole goal is deferred, pursue a suggested supporting goal instead of paraphrasing it.",
            "lessons": [self.public_lesson(item, evaluation=self.evaluate_retry(item, observation)) for item in goal_lessons],
            "deferred_tactics": [self.public_lesson(item, evaluation=self.evaluate_retry(item, observation)) for item in tactic_lessons if item["scope"] == "tactic"][:10],
            "combat_readiness": self.readiness_summary(observation),
            "combat_history": self.combat_summary(observation, limit=8),
        }

    def status_summary(self, observation: dict[str, Any]) -> dict[str, Any]:
        deferred = self.storage.goal_lessons(statuses=["deferred"], limit=100)
        unlocked = self.storage.goal_lessons(statuses=["unlocked"], limit=100)
        return {
            "deferred_count": len(deferred),
            "unlocked_count": len(unlocked),
            "deferred_goals": [
                self.public_lesson(item, evaluation=self.evaluate_retry(item, observation))
                for item in deferred if item["scope"] == "goal"
            ][:10],
            "deferred_tactics": [
                self.public_lesson(item, evaluation=self.evaluate_retry(item, observation))
                for item in deferred if item["scope"] == "tactic"
            ][:10],
            # Unlocked tactic lessons mean the exact tactical quarantine was
            # released by changed state. They are not campaign goals for the supervisor
            # to retry and must not crowd the actionable retry list.
            "eligible_retries": [
                self.public_lesson(item)
                for item in unlocked
                if item.get("scope") == "goal"
                and not item.get("resolution_goal_id")
                and (
                    (original := self.storage.goal(str(item.get("goal_id") or "")))
                    is None
                    or original.get("status")
                    not in {"queued", "active", "paused"}
                )
            ][:10],
            "retries_in_progress": [
                self.public_lesson(item)
                for item in unlocked
                if item.get("scope") == "goal" and item.get("resolution_goal_id")
            ][:10],
            "combat_readiness": self.readiness_summary(observation),
            "combat_history": self.combat_summary(observation, limit=8),
        }
