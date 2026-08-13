from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import CRITERION_KINDS, ability_name_key, parse_ability_metric
from .storage import Storage
from .utils import deep_get


# ``inventory_contains`` normally matches a concrete item name.  Internal
# campaign phases may deliberately request the semantic category ``food``;
# these are the ordinary edible item names backed by the same source-derived
# nutrition catalogue used by combat provisioning.  Reagents such as herbs
# and elderberries are intentionally absent: they are not edible supplies
# until Create Food converts them.
EDIBLE_FOOD_NAME_MARKERS = (
    "inky cap",
    "chocolate mint",
    "wheel of cheese",
    "cheese",
    "turkey leg",
    "mug of",
    "meat pie",
    "stew",
    "loaf of bread",
    "waterskin",
    "slice of pork",
    "bowl of soup",
    "spideye",
    "bunch of grapes",
    "apple",
    "edible mushroom",
    "drumstick",
    "goblet",
)


@dataclass(frozen=True)
class CriterionResult:
    id: str
    kind: str
    met: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "met": self.met, "detail": self.detail}


class CriteriaEvaluator:
    SUPPORTED = set(CRITERION_KINDS)

    def __init__(self, storage: Storage):
        self.storage = storage

    def validate(self, criteria: list[dict[str, Any]]) -> None:
        ids: set[str] = set()
        for index, criterion in enumerate(criteria):
            criterion_id = str(criterion.get("id") or f"criterion_{index + 1}")
            if criterion_id in ids:
                raise ValueError(f"duplicate criterion id: {criterion_id}")
            ids.add(criterion_id)
            kind = criterion.get("kind")
            if kind not in self.SUPPORTED:
                raise ValueError(f"unsupported criterion kind: {kind}")

    def evaluate(self, goal: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        criteria = goal.get("success_criteria", [])
        self.validate(criteria)
        simple: dict[str, CriterionResult] = {}
        for index, item in enumerate(criteria):
            criterion_id = str(item.get("id") or f"criterion_{index + 1}")
            if item["kind"] not in {"composite_all", "composite_any"}:
                simple[criterion_id] = self._simple(criterion_id, item, goal, observation)
        for index, item in enumerate(criteria):
            criterion_id = str(item.get("id") or f"criterion_{index + 1}")
            refs = item.get("criteria", item.get("criterion_ids", []))
            if item["kind"] == "composite_all":
                met = bool(refs) and all(simple.get(str(ref), CriterionResult("", "", False, "missing reference")).met for ref in refs)
                simple[criterion_id] = CriterionResult(criterion_id, item["kind"], met, f"all references met={met}")
            elif item["kind"] == "composite_any":
                met = bool(refs) and any(simple.get(str(ref), CriterionResult("", "", False, "missing reference")).met for ref in refs)
                simple[criterion_id] = CriterionResult(criterion_id, item["kind"], met, f"any reference met={met}")
        results = [simple[str(item.get("id") or f"criterion_{index + 1}")] for index, item in enumerate(criteria)]
        percent = round(100 * sum(result.met for result in results) / len(results)) if results else 0
        all_met = bool(results) and all(result.met for result in results)
        return {
            "percent_estimate": percent,
            "summary": f"{sum(result.met for result in results)} of {len(results)} criteria verified",
            "evidence_event_ids": [],
            "criteria": [result.as_dict() for result in results],
            "all_met": all_met,
        }

    @staticmethod
    def inventory_count(items: Any, requested_item: Any) -> int:
        """Count a concrete inventory name or a supported semantic category."""

        if not isinstance(items, list):
            return 0
        needle = " ".join(str(requested_item or "").split()).casefold()
        semantic_food = needle in {"food", "edible food"}
        count = 0
        for entry in items:
            if not isinstance(entry, dict):
                continue
            name = " ".join(str(entry.get("name") or "").split()).casefold()
            matches = (
                any(marker in name for marker in EDIBLE_FOOD_NAME_MARKERS)
                if semantic_food
                else bool(needle) and needle in name
            )
            if not matches:
                continue
            try:
                count += max(1, int(entry.get("amount", 1) or 1))
            except (TypeError, ValueError):
                count += 1
        return count

    def _simple(self, criterion_id: str, item: dict[str, Any], goal: dict[str, Any], observation: dict[str, Any]) -> CriterionResult:
        kind = item["kind"]
        if kind == "operator_confirmed":
            page = self.storage.events(
                after_cursor=int(self.storage.goal_event_anchor(str(goal.get("id", ""))) or 0),
                limit=1,
                kinds=["goal.operator_confirmed"],
                goal_id=str(goal.get("id", "")) or None,
            )
            met = bool(page.get("events"))
            return CriterionResult(
                criterion_id,
                kind,
                met,
                "explicit operator confirmation recorded"
                if met
                else "awaiting explicit operator confirmation",
            )
        if kind == "state_equals":
            actual = self._state_value(
                observation, str(item.get("path", item.get("metric", "")))
            )
            expected = item.get("value")
            return CriterionResult(criterion_id, kind, actual == expected, f"observed {actual!r}; expected {expected!r}")
        if kind in {"numeric_threshold", "numeric_delta"}:
            actual = self._numeric_metric(observation, str(item.get("metric", "")))
            baseline = item.get("baseline", 0) if kind == "numeric_delta" else 0
            measured = actual - baseline if isinstance(actual, (int, float)) and isinstance(baseline, (int, float)) else None
            op = str(item.get("operator", ">="))
            target = item.get("value")
            met = self._compare(measured, op, target)
            return CriterionResult(criterion_id, kind, met, f"observed {measured!r} {op} {target!r}")
        if kind == "inventory_contains":
            items = deep_get(observation, "inventory.items", [])
            wanted = int(item.get("count", 1))
            count = self.inventory_count(items, item.get("item"))
            return CriterionResult(criterion_id, kind, count >= wanted, f"verified count {count}; required {wanted}")
        if kind == "location_reached":
            location = str(item.get("location", item.get("room", ""))).lower()
            room = str(deep_get(observation, "look.room.name", deep_get(observation, "look.room", ""))).lower()
            room_id = deep_get(observation, "look.room.num", deep_get(observation, "look.room_id"))
            named_match = bool(location) and location in room
            exact_id_match = item.get("room_id") is not None and str(item.get("room_id")) == str(room_id)
            met = named_match or exact_id_match
            return CriterionResult(criterion_id, kind, met, f"observed room {room or room_id!r}")
        if kind == "event_occurred":
            requested_cursor = int(item.get("after_cursor", 0))
            goal_anchor = self.storage.goal_event_anchor(str(goal.get("id", "")))
            # A future/fabricated cursor would otherwise make a valid goal
            # permanently blind to events that happen before that number.  A
            # goal may look back before submission when explicitly requested,
            # but it may never anchor later than its own durable submission.
            effective_cursor = min(requested_cursor, goal_anchor) if goal_anchor is not None else requested_cursor
            page = self.storage.events(
                after_cursor=effective_cursor,
                limit=200,
                kinds=[str(item.get("event_kind"))],
                goal_id=str(goal.get("id", "")) or None,
            )
            met = bool(page["events"])
            anchor_note = (
                f"; invalid future cursor {requested_cursor} clamped to goal anchor {effective_cursor}"
                if effective_cursor != requested_cursor
                else ""
            )
            return CriterionResult(
                criterion_id,
                kind,
                met,
                f"matching goal-scoped durable events: {len(page['events'])}{anchor_note}",
            )
        return CriterionResult(criterion_id, kind, False, "unsupported verifier")

    @staticmethod
    def _state_value(observation: dict[str, Any], path: str) -> Any:
        """Resolve stable state paths, including broker compatibility aliases."""

        if path != "inventory.full":
            return deep_get(observation, path)

        inventory = observation.get("inventory")
        if not isinstance(inventory, dict):
            return None
        if "full" in inventory:
            return inventory["full"]

        # Current broker snapshots expose capacity as remaining weight and bulk
        # rather than the older synthetic ``inventory.full`` flag used by
        # campaign criteria. Keep that criterion stable for persisted phases and
        # derive the flag only when the carry snapshot is explicitly known.
        carry = inventory.get("carry")
        if not isinstance(carry, dict) or carry.get("known") is not True:
            return None
        room_for = carry.get("room_for")
        if isinstance(room_for, dict):
            weight = room_for.get("weight")
            bulk = room_for.get("bulk")
            if all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in (weight, bulk)
            ):
                return bool(weight <= 0 or bulk <= 0)

        load = carry.get("load")
        maximum = carry.get("max")
        if isinstance(load, dict) and isinstance(maximum, dict):
            weight = load.get("weight")
            bulk = load.get("bulk")
            weight_max = maximum.get("weight")
            bulk_max = maximum.get("bulk")
            if all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in (weight, bulk, weight_max, bulk_max)
            ):
                return bool(weight >= weight_max or bulk >= bulk_max)
        return None

    @staticmethod
    def _numeric_metric(observation: dict[str, Any], metric: str) -> Any:
        """Resolve ordinary dot paths plus stable named skill/spell values."""
        if metric == "carried_currency":
            items = deep_get(observation, "inventory.items", [])
            total = 0
            for item in items if isinstance(items, list) else []:
                if (
                    not isinstance(item, dict)
                    or "shilling" not in str(item.get("name") or "").casefold()
                ):
                    continue
                amount = item.get(
                    "amount", item.get("quantity", item.get("count", 1))
                )
                total += (
                    int(amount)
                    if isinstance(amount, (int, float))
                    and not isinstance(amount, bool)
                    and amount > 0
                    else 1
                )
            return total
        aliases = {
            "max_health": (
                "status.vitals.health.max",
                "look.vitals.health.max",
            ),
            "current_health": (
                "status.vitals.health.value",
                "status.vitals.health.current",
                "look.vitals.health.value",
                "look.vitals.health.current",
            ),
            "vigor": (
                "status.vitals.vigor.value",
                "status.vitals.vigor.current",
                "look.vitals.vigor.value",
                "look.vitals.vigor.current",
            ),
        }
        if metric in aliases:
            for path in aliases[metric]:
                value = deep_get(observation, path)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return value
            return None
        parsed = parse_ability_metric(metric)
        if parsed is None:
            return deep_get(observation, metric)
        ability_kind, requested_name = parsed
        abilities = observation.get("abilities")
        if not isinstance(abilities, dict):
            return None
        freshness = abilities.get("freshness")
        if isinstance(freshness, dict):
            known = freshness.get("known")
            if known is False:
                return None
            if isinstance(known, dict) and known.get(f"{ability_kind}s") is False:
                return None
        rows = abilities.get(f"{ability_kind}s")
        wanted = ability_name_key(requested_name)
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or ability_name_key(row.get("name")) != wanted:
                continue
            value = row.get("ability")
            return value if isinstance(value, (int, float)) else None
        return None

    @staticmethod
    def _compare(actual: Any, operator: str, expected: Any) -> bool:
        if not isinstance(actual, (int, float)) or not isinstance(expected, (int, float)):
            return False
        return {">=": actual >= expected, ">": actual > expected, "<=": actual <= expected, "<": actual < expected, "==": actual == expected}.get(operator, False)
