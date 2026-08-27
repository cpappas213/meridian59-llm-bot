from __future__ import annotations

import json
import os
import re
import select
import shutil
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import BotConfig
from .utils import uuid7


ANSI_RESET = "\x1b[0m"
ANSI_STYLES = {
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
    "bright_cyan": "\x1b[1;36m",
    "bright_white": "\x1b[1;97m",
}


class ControllerApiError(RuntimeError):
    """The local controller API rejected or could not service a TUI request."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.retryable = retryable


class ControllerApi:
    def __init__(self, config: BotConfig, *, timeout: float = 4.0):
        host = "[::1]" if config.controller.control_bind == "::1" else "127.0.0.1"
        self.base_url = f"http://{host}:{config.controller.control_port}"
        self.token = config.control_token
        self.timeout = timeout
        # Goal drafting permits three schema/grounding attempts, and each model
        # request may use the client's one JSON-repair retry.
        self.model_timeout = max(
            timeout, config.model.planner_timeout_seconds * 6 + 5
        )

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={
                "authorization": f"Bearer {self.token}",
                "content-type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout if timeout is None else timeout
            ) as response:
                value = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                error = json.load(exc)
            except (json.JSONDecodeError, UnicodeDecodeError):
                error = {}
            code = error.get("code", f"HTTP_{exc.code}") if isinstance(error, dict) else f"HTTP_{exc.code}"
            message = error.get("message", exc.reason) if isinstance(error, dict) else exc.reason
            details = error.get("details", {}) if isinstance(error, dict) else {}
            raise ControllerApiError(
                f"{code}: {message}",
                code=str(code),
                details=details if isinstance(details, dict) else {},
                retryable=bool(error.get("retryable")) if isinstance(error, dict) else False,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ControllerApiError(f"controller unavailable at {self.base_url}: {reason}") from exc
        if not isinstance(value, dict):
            raise ControllerApiError("controller returned a non-object response")
        return value

    def status(self) -> dict[str, Any]:
        return self.request(
            "GET", "/v1/status?detail=supervision&include_recent_events=0"
        )

    def safe_stop(
        self, *, destination_room_id: int | None = None
    ) -> dict[str, Any]:
        payload = (
            {}
            if destination_room_id is None
            else {"destination_room_id": destination_room_id}
        )
        return self.request("POST", "/v1/runtime/safe-stop", payload)

    def character_status(self) -> dict[str, Any]:
        return self.request("GET", "/v1/character")

    def conversations(self, *, limit: int = 60) -> dict[str, Any]:
        limit = max(1, min(limit, 200))
        value = self.request("GET", f"/v1/conversations?limit={limit}")
        timezone_name = str(value.get("timezone") or "UTC")
        messages = value.get("messages")
        result: list[dict[str, Any]] = []
        for item in messages if isinstance(messages, list) else []:
            if not isinstance(item, dict):
                continue
            message = dict(item)
            message["display_occurred_at"] = _event_display_timestamp(
                message.get("occurred_at"), timezone_name
            )
            result.append(message)
        return {**value, "messages": result, "timezone": timezone_name}

    def goals(self) -> list[dict[str, Any]]:
        value = self.request("GET", "/v1/goals")
        goals = value.get("goals")
        return [item for item in goals if isinstance(item, dict)] if isinstance(goals, list) else []

    def events(self, *, limit: int = 5) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 20))
        value = self.request(
            "GET",
            f"/v1/events?latest=true&interesting_only=false&limit={limit}",
        )
        events = value.get("events")
        timezone_name = str(value.get("timezone") or "UTC")
        if not isinstance(events, list):
            return []
        result: list[dict[str, Any]] = []
        for item in events:
            if not isinstance(item, dict):
                continue
            event = dict(item)
            event["display_occurred_at"] = _event_display_timestamp(
                event.get("occurred_at"), timezone_name
            )
            result.append(event)
        return result

    def submit_goal(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/v1/goals", payload)

    def draft_goal(
        self,
        prompt: str,
        *,
        current_goal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"prompt": prompt}
        if current_goal is not None:
            payload["current_goal"] = current_goal
        return self.request(
            "POST",
            "/v1/goals/draft",
            payload,
            timeout=self.model_timeout,
        )

    def manage_goal(self, goal_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = urllib.parse.quote(goal_id, safe="")
        return self.request("POST", f"/v1/goals/{encoded}/commands", payload)


def _paint(value: Any, style: str, enabled: bool) -> str:
    text = str(value)
    code = ANSI_STYLES.get(style)
    return f"{code}{text}{ANSI_RESET}" if enabled and code else text


def _state_style(value: Any) -> str:
    state = str(value or "").casefold()
    if state in {
        "running",
        "joined",
        "ready",
        "active",
        "succeeded",
        "complete",
        "low",
        "known",
    }:
        return "green"
    if state in {
        "starting",
        "reconciling",
        "degraded",
        "shutdown_requested",
        "draining",
        "requested",
        "pausing",
        "securing",
        "logging_out",
        "queued",
        "paused",
        "awaiting_persona",
        "awaiting_character_identity",
        "awaiting_persona_name_match",
        "elevated",
        "warning",
        "notice",
    }:
        return "yellow"
    if state in {
        "blocked",
        "critical",
        "disconnected",
        "incompatible",
        "stopping",
        "stopped",
        "failed",
        "error",
        "cancelled",
    }:
        return "red"
    return "cyan"


def _vital_style(value: Any) -> str:
    if not isinstance(value, dict):
        return "cyan"
    current = value.get("current", value.get("value"))
    maximum = value.get("max", value.get("maximum", value.get("scale_max")))
    if not isinstance(current, (int, float)) or not isinstance(
        maximum, (int, float)
    ) or maximum <= 0:
        return "cyan"
    fraction = current / maximum
    return "green" if fraction >= 0.7 else "yellow" if fraction >= 0.4 else "red"


def _terminal_colors_enabled() -> bool:
    return "NO_COLOR" not in os.environ and bool(
        getattr(sys.stdout, "isatty", lambda: False)()
    )


def _one_line(value: Any, *, limit: int = 100) -> str:
    text = " ".join(str(value or "-").split())
    return textwrap.shorten(text, width=max(8, limit), placeholder="...")


def _event_display_timestamp(value: Any, timezone_name: str = "UTC") -> str:
    """Render an event in the operator-selected deployment timezone."""

    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return text[:20]
        local = parsed.astimezone(ZoneInfo(timezone_name))
        return local.strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, OverflowError, ZoneInfoNotFoundError):
        return text[:20]


def _meter(vitals: dict[str, Any], name: str) -> str:
    value = vitals.get(name)
    if isinstance(value, dict):
        current = value.get("current", value.get("value"))
        maximum = value.get("max", value.get("maximum", value.get("scale_max")))
        if current is not None and maximum is not None:
            return f"{current}/{maximum}"
        if current is not None:
            return str(current)
    return str(value) if value is not None else "-"


def _human_label(value: Any) -> str:
    return str(value).replace("_", " ").strip().title()


ROOM_PROPERTY_LABELS = {
    "ROOM_GUEST_AREA": "guest area",
    "ROOM_GUILD_PK_ONLY": "guild PvP only",
    "ROOM_HARD_LEARN": "hard learning",
    "ROOM_HOMETOWN": "hometown",
    "ROOM_LAMPS": "lamps",
    "ROOM_NO_COMBAT": "no combat",
    "ROOM_NO_PK": "no PvP",
    "ROOM_OVERRIDE_DEPTH1": "depth override 1",
    "ROOM_OVERRIDE_DEPTH2": "depth override 2",
    "ROOM_SAFE_DEATH": "safe death",
    "ROOM_SAFELOGOFF": "safe logoff",
    "ROOM_SANCTUARY": "sanctuary",
    "ROOM_TRIPLE_HEAL": "triple healing",
}


def _room_property_labels(game: dict[str, Any]) -> list[str]:
    properties = game.get("room_properties")
    if not isinstance(properties, dict):
        return []
    if properties.get("known") is not True:
        return ["properties unknown"]

    labels: list[str] = []
    if properties.get("safe") is True:
        labels.append("safe")
    flags = properties.get("flags")
    flag_values = (
        [str(value) for value in flags]
        if isinstance(flags, list)
        else []
    )
    flag_priority = {
        "ROOM_SANCTUARY": 0,
        "ROOM_NO_COMBAT": 1,
        "ROOM_NO_PK": 2,
        "ROOM_SAFELOGOFF": 3,
        "ROOM_SAFE_DEATH": 4,
        "ROOM_TRIPLE_HEAL": 5,
    }
    for value in sorted(flag_values, key=lambda item: (flag_priority.get(item, 20), item)):
        labels.append(
            ROOM_PROPERTY_LABELS.get(
                value,
                value.removeprefix("ROOM_").replace("_", " ").casefold(),
            )
        )
    terrain = properties.get("terrain")
    for value in terrain if isinstance(terrain, list) else []:
        labels.append(
            str(value).removeprefix("TERRAIN_").replace("_", " ").casefold()
        )
    if properties.get("region"):
        labels.append(f"region: {' '.join(str(properties['region']).split())}")

    unique: list[str] = []
    for label in labels:
        if label and label not in unique:
            unique.append(label)
    return unique or ["no special tags"]


def _format_room_properties(
    game: dict[str, Any], *, max_length: int, color: bool
) -> str:
    labels = _room_property_labels(game)
    if not labels or max_length < 8:
        return ""
    for count in range(len(labels), 0, -1):
        omitted = len(labels) - count
        visible = labels[:count] + ([f"+{omitted}"] if omitted else [])
        candidate = f"[{', '.join(visible)}]"
        if len(candidate) <= max_length:
            properties = game.get("room_properties")
            style = (
                "green"
                if isinstance(properties, dict) and properties.get("safe") is True
                else "dim"
                if labels == ["properties unknown"]
                else "cyan"
            )
            return _paint(candidate, style, color)
    fallback = f"[+{len(labels)} tags]"
    return _paint(fallback, "cyan", color) if len(fallback) <= max_length else ""


def _format_location(game: dict[str, Any], *, width: int, color: bool) -> str:
    room_id = game.get("room_id", "-")
    suffix = f" (room {room_id})"
    available = max(12, width - len("Location ") - len(suffix))
    name_limit = min(42, max(12, available // 2))
    name = _one_line(game.get("location"), limit=name_limit)
    tag_budget = max(0, available - len(name) - 1)
    tags = _format_room_properties(game, max_length=tag_budget, color=color)
    return (
        f"{_paint(name, 'cyan', color)}{suffix}"
        + (f" {tags}" if tags else "")
    )


def _human_number(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _first_record_value(record: dict[str, Any], *names: str) -> Any:
    for name in names:
        if record.get(name) is not None:
            return record[name]
    return None


def _format_vital(name: Any, value: Any) -> str:
    """Turn a harness vital record into a compact operator-facing sentence."""

    label = _human_label(name)
    if not isinstance(value, dict):
        return f"{label}: {_human_number(value) if value is not None else '-'}"
    current = _first_record_value(value, "current", "value")
    maximum = _first_record_value(value, "max", "maximum", "scale_max")
    if current is None:
        primary = "unavailable"
    elif maximum is None:
        primary = _human_number(current)
    else:
        primary = f"{_human_number(current)} / {_human_number(maximum)}"

    details: list[str] = []
    percentage = _first_record_value(value, "pct", "percent", "percentage")
    if percentage is not None:
        details.append(f"{_human_number(percentage)}%")
    if value.get("rested") is not None:
        details.append("rested" if value.get("rested") else "not rested")
    if value.get("rest_threshold") is not None:
        details.append(
            f"rest threshold {_human_number(value['rest_threshold'])}"
        )
    return f"{label}: {primary}" + (f" ({'; '.join(details)})" if details else "")


def _format_attribute(name: Any, value: Any) -> str:
    """Render an attribute record without leaking its Python/JSON representation."""

    label = _human_label(name)
    if not isinstance(value, dict):
        return f"{label}: {_human_number(value) if value is not None else '-'}"
    current = _first_record_value(value, "current", "value", "effective", "base")
    primary = _human_number(current) if current is not None else "unavailable"
    details: list[str] = []
    known_details = (
        ("display_scale", "display scale"),
        ("hard_cap", "hard cap"),
        ("max", "maximum"),
        ("maximum", "maximum"),
        ("modifier", "modifier"),
        ("bonus", "bonus"),
    )
    used = {"current", "value", "effective", "base"}
    for key, detail_label in known_details:
        if key in used or value.get(key) is None:
            continue
        details.append(f"{detail_label} {_human_number(value[key])}")
        used.add(key)
    for key, item in value.items():
        if key in used or item is None or isinstance(item, (dict, list, tuple, set)):
            continue
        details.append(f"{key.replace('_', ' ')} {_human_number(item)}")
    return f"{label}: {primary}" + (f" ({'; '.join(details)})" if details else "")


def _ability_lines(development: dict[str, Any], group: str, *, limit: int = 8) -> list[str]:
    rows = development.get(group)
    rows = rows if isinstance(rows, list) else []
    result = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        ability = row.get("ability")
        result.append(f"{row.get('name', '?')} {ability if ability is not None else '-'}")
    omitted = int(development.get(f"{group}_omitted", 0) or 0)
    if omitted:
        result.append(f"+{omitted} more")
    return result


def render_dashboard(
    status: dict[str, Any],
    goals: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    message: str = "",
    width: int | None = None,
    color: bool = False,
) -> str:
    width = width or shutil.get_terminal_size((110, 32)).columns
    width = max(72, width)
    controller = status.get("controller") if isinstance(status.get("controller"), dict) else {}
    shutdown = (
        controller.get("shutdown")
        if isinstance(controller.get("shutdown"), dict)
        else None
    )
    game = status.get("game") if isinstance(status.get("game"), dict) else {}
    onboarding = status.get("onboarding") if isinstance(status.get("onboarding"), dict) else {}
    campaign = status.get("campaign") if isinstance(status.get("campaign"), dict) else {}
    campaign_execution = (
        campaign.get("execution")
        if isinstance(campaign.get("execution"), dict)
        else {}
    )
    active_phase = (
        campaign_execution.get("active_phase")
        if isinstance(campaign_execution.get("active_phase"), dict)
        else None
    )
    development = campaign.get("development") if isinstance(campaign.get("development"), dict) else {}
    readiness = campaign.get("readiness") if isinstance(campaign.get("readiness"), dict) else {}
    vitals = game.get("vitals") if isinstance(game.get("vitals"), dict) else {}
    finances = game.get("finances") if isinstance(game.get("finances"), dict) else {}
    bank_accounts = (
        finances.get("bank_accounts")
        if isinstance(finances.get("bank_accounts"), list)
        else []
    )
    session_started_at = str(controller.get("since") or "")
    known_bank_balances = [
        account.get("last_known_balance")
        for account in bank_accounts
        if isinstance(account, dict)
        and isinstance(account.get("last_known_balance"), (int, float))
        and not isinstance(account.get("last_known_balance"), bool)
        and (
            not session_started_at
            or (
                account.get("recorded_at") is not None
                and str(account["recorded_at"]) >= session_started_at
            )
        )
    ]
    banked_shillings = sum(known_bank_balances) if known_bank_balances else None
    inventory_value = _first_record_value(
        finances,
        "known_inventory_item_value",
        "source_estimated_inventory_value",
    )
    banked_text = (
        _human_number(banked_shillings)
        if banked_shillings is not None
        else "unknown"
    )
    inventory_value_text = (
        _human_number(inventory_value)
        if inventory_value is not None
        else "unknown"
    )
    if inventory_value is not None and finances.get("valuation_complete") is False:
        inventory_value_text += " (partial estimate)"
    active = next((goal for goal in goals if goal.get("status") == "active"), None)
    displayed_goal = status.get("goal") if isinstance(status.get("goal"), dict) else active
    queue = [goal for goal in goals if goal.get("status") == "queued"]
    paused = [goal for goal in goals if goal.get("status") in {"paused", "blocked"}]
    rule = _paint("-" * width, "blue", color)
    heavy_rule = _paint("=" * width, "blue", color)

    lines = [
        _paint("MERIDIAN 59 BOT CONSOLE".center(width), "bright_cyan", color),
        heavy_rule,
        (
            f"Controller {_paint(controller.get('state', 'unknown'), _state_style(controller.get('state')), color)} | "
            f"Game {_paint(game.get('connection', 'unknown'), _state_style(game.get('connection')), color)} | "
            f"Character {_paint(game.get('character_name') or '-', 'bright_white', color)}"
        ),
        f"Location {_format_location(game, width=width, color=color)}",
        (
            f"{_paint('HP ' + _meter(vitals, 'health'), _vital_style(vitals.get('health')), color)}  "
            f"{_paint('Mana ' + _meter(vitals, 'mana'), 'blue', color)}  "
            f"{_paint('Vigor ' + _meter(vitals, 'vigor'), _vital_style(vitals.get('vigor')), color)}  "
            f"Risk {_paint(game.get('risk', '-'), _state_style(game.get('risk')), color)}  "
            f"Currency {_paint(game.get('carried_currency', '-'), 'yellow', color)}"
        ),
        (
            f"Banked Shillings {_paint(banked_text, 'yellow' if banked_shillings is not None else 'dim', color)} | "
            f"Total Inventory Value {_paint(inventory_value_text, 'yellow' if inventory_value is not None else 'dim', color)}"
        ),
        (
            f"Onboarding {_paint(onboarding.get('status', '-'), _state_style(onboarding.get('status')), color)} | "
            f"Control {controller.get('control_owner', '-')} | "
            f"Observation age {game.get('observation_age_seconds', '-')}s"
        ),
    ]
    if shutdown and shutdown.get("stage"):
        shutdown_stage = str(shutdown.get("stage"))
        shutdown_details: list[str] = []
        paused_goal_ids = shutdown.get("paused_goal_ids")
        if isinstance(paused_goal_ids, list) and paused_goal_ids:
            shutdown_details.append(f"{len(paused_goal_ids)} goal(s) paused")
        safe_room = shutdown.get("safe_room")
        if isinstance(safe_room, dict):
            safe_name = safe_room.get("name") or safe_room.get("canonical_name")
            safe_id = safe_room.get("room_id")
            if safe_name or safe_id is not None:
                shutdown_details.append(
                    "safe room "
                    + str(safe_name or "verified")
                    + (f" ({safe_id})" if safe_id is not None else "")
                )
        if shutdown.get("logged_out") is True:
            shutdown_details.append("logged out")
        if shutdown.get("error"):
            shutdown_details.append(
                "error " + _one_line(shutdown.get("error"), limit=width // 2)
            )
        lines.append(
            (
                f"Safe shutdown {_paint('[' + shutdown_stage + ']', _state_style(shutdown_stage), color)}"
                + (f" | {' | '.join(shutdown_details)}" if shutdown_details else "")
            )
        )
    lines.extend([rule, _paint("CURRENT GOAL", "bright_cyan", color)])
    if displayed_goal:
        lines.extend(
            [
                (
                    f"{_paint('[' + str(displayed_goal.get('status', '-')) + ']', _state_style(displayed_goal.get('status')), color)} "
                    f"{_one_line(displayed_goal.get('title'), limit=width - 28)} "
                    f"(priority {_paint(displayed_goal.get('priority', '-'), 'magenta', color)}, "
                    f"{_paint(str(displayed_goal.get('progress_percent', 0)) + '%', 'green', color)})"
                ),
                f"  {_one_line(displayed_goal.get('objective'), limit=width - 4)}",
            ]
        )
        summary = displayed_goal.get("progress_summary")
        if summary:
            lines.append(f"  Progress: {_one_line(summary, limit=width - 12)}")
        criteria = displayed_goal.get("criteria")
        if isinstance(criteria, list):
            for criterion in criteria[:4]:
                if isinstance(criterion, dict):
                    mark = "x" if criterion.get("met") else " "
                    detail = criterion.get("detail") or criterion.get("kind")
                    lines.append(
                        _paint(
                            f"  [{mark}] {_one_line(detail, limit=width - 8)}",
                            "green" if criterion.get("met") else "dim",
                            color,
                        )
                    )
            if any(
                isinstance(criterion, dict)
                and criterion.get("kind") == "operator_confirmed"
                and criterion.get("met") is not True
                for criterion in criteria
            ):
                lines.append(
                    _paint(
                        "  Manual confirmation pending: press M, select this goal, then F after the other criteria pass.",
                        "yellow",
                        color,
                    )
                )
    else:
        lines.append("No active, paused, or blocked goal. The bot is strategically idle.")

    if displayed_goal and campaign_execution:
        lines.extend([rule, _paint("CURRENT PHASE", "bright_cyan", color)])
        if active_phase is not None:
            ordinal = active_phase.get("ordinal")
            phase_number = f"#{ordinal} " if ordinal is not None else ""
            phase_kind = _human_label(active_phase.get("kind") or "internal work")
            phase_status = str(active_phase.get("status") or "-")
            attempt_count = active_phase.get("attempt_count", 0)
            lines.extend(
                [
                    (
                        f"{_paint(phase_number + phase_kind, 'bright_white', color)} "
                        f"{_paint('[' + phase_status + ']', _state_style(phase_status), color)} | "
                        f"Attempts {_paint(attempt_count, 'magenta', color)} | "
                        f"Campaign {_paint(campaign_execution.get('status', '-'), _state_style(campaign_execution.get('status')), color)}"
                    ),
                    f"  {_one_line(active_phase.get('objective'), limit=width - 4)}",
                ]
            )
            execution_plan = (
                displayed_goal.get("execution_plan")
                if isinstance(displayed_goal.get("execution_plan"), dict)
                else {}
            )
            plan_summary = execution_plan.get("summary")
            if plan_summary:
                plan_status = execution_plan.get("status") or "pending"
                lines.append(
                    f"  Plan [{_paint(plan_status, _state_style(plan_status), color)}]: "
                    f"{_one_line(plan_summary, limit=width - 14)}"
                )
        else:
            lines.append(
                _paint(
                    "No active internal phase; the campaign manager is selecting the next phase.",
                    "yellow",
                    color,
                )
            )

    lines.extend([rule, _paint("GOAL QUEUE", "bright_cyan", color)])
    if queue:
        for index, goal in enumerate(queue[:8], 1):
            lines.append(
                f"{index:>2}. {_paint('P' + str(goal.get('priority', '-')).rjust(3), 'magenta', color)}  "
                f"{_one_line(goal.get('title'), limit=width - 14)}"
            )
    else:
        lines.append("Queue is empty.")
    if paused:
        lines.append(
            "Paused/blocked: "
            + "; ".join(
                f"{goal.get('title', '?')} [{goal.get('status')}]" for goal in paused[:4]
            )
        )

    skill_text = ", ".join(_ability_lines(development, "skills")) or "none observed"
    spell_text = ", ".join(_ability_lines(development, "spells")) or "none observed"
    lines.extend(
        [
            rule,
            _paint("CHARACTER DEVELOPMENT", "bright_cyan", color),
            f"Skills: {_one_line(skill_text, limit=width - 8)}",
            f"Spells: {_one_line(spell_text, limit=width - 8)}",
            (
                f"Equipment {readiness.get('equipment_state', '-')} | "
                f"Healing supplies {readiness.get('healing_supply_count', '-')} | "
                f"Recent deaths {readiness.get('recent_combat_deaths', '-')}"
            ),
            rule,
            _paint("RECENT EVENTS", "bright_cyan", color),
        ]
    )
    if events:
        for event in events[-5:]:
            severity = event.get("severity", "info")
            lines.append(
                f"{event.get('display_occurred_at') or _event_display_timestamp(event.get('occurred_at'))} "
                f"{_paint(f'{str(severity):>7}', _state_style(severity), color)}  "
                f"{_one_line(event.get('summary'), limit=max(20, width - 34))}"
            )
    else:
        lines.append("No interesting events yet.")
    lines.extend(
        [
            heavy_rule,
            (
                f"{_paint('[N]', 'bright_cyan', color)} New goal   "
                f"{_paint('[M]', 'bright_cyan', color)} Manage goal/queue   "
                f"{_paint('[S]', 'bright_cyan', color)} Character status   "
                f"{_paint('[C]', 'bright_cyan', color)} Recent chat"
            ),
            (
                f"{_paint('[R]', 'bright_cyan', color)} Refresh   "
                f"{_paint('[H]', 'bright_cyan', color)} Help   "
                f"{_paint('[X]', 'yellow', color)} Safe shutdown   "
                f"{_paint('[Q]', 'bright_cyan', color)} Detach TUI"
            ),
        ]
    )
    if message:
        message_style = (
            "red"
            if any(
                word in message.casefold()
                for word in ("failed", "error", "unavailable", "unknown")
            )
            else "green"
        )
        lines.append(
            f"Status: {_paint(_one_line(message, limit=width - 8), message_style, color)}"
        )
    return "\n".join(lines)


def _equipment_label(item: Any) -> str:
    if isinstance(item, dict):
        name = str(item.get("name") or item.get("id") or "unknown item")
        slot = item.get("slot")
        return f"{name} ({slot})" if slot else name
    return str(item)


def _ability_value_style(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "dim"
    return "green" if value >= 70 else "yellow" if value >= 30 else "cyan"


def render_character_status(
    detail: dict[str, Any], *, width: int | None = None, color: bool = False
) -> str:
    """Render the complete on-demand character detail returned by the controller."""

    width = max(72, width or shutil.get_terminal_size((110, 32)).columns)
    game = detail.get("game") if isinstance(detail.get("game"), dict) else {}
    abilities = (
        detail.get("abilities") if isinstance(detail.get("abilities"), dict) else {}
    )
    inventory = (
        detail.get("inventory") if isinstance(detail.get("inventory"), dict) else {}
    )
    equipment = (
        detail.get("equipment") if isinstance(detail.get("equipment"), dict) else {}
    )
    rule = _paint("-" * width, "blue", color)
    heavy_rule = _paint("=" * width, "blue", color)
    lines = [
        _paint("DETAILED CHARACTER STATUS".center(width), "bright_cyan", color),
        heavy_rule,
        (
            f"Character {_paint(game.get('character_name') or '-', 'bright_white', color)} | "
            f"Connection {_paint(game.get('connection', 'unknown'), _state_style(game.get('connection')), color)}"
        ),
        f"Location {_format_location(game, width=width, color=color)}",
        (
            f"Observed {game.get('observation_age_seconds', '-')}s ago | "
            f"Risk {_paint(game.get('risk', '-'), _state_style(game.get('risk')), color)} | "
            f"Currency {_paint(game.get('carried_currency', '-'), 'yellow', color)}"
        ),
        rule,
        _paint("VITALS", "bright_cyan", color),
    ]

    vitals = game.get("vitals") if isinstance(game.get("vitals"), dict) else {}
    preferred_vitals = ["health", "mana", "vigor"]
    vital_names = preferred_vitals + [
        str(name) for name in vitals if str(name) not in preferred_vitals
    ]
    if vital_names:
        lines.extend(
            "  "
            + _paint(
                _format_vital(name, vitals.get(name)),
                _vital_style(vitals.get(name)),
                color,
            )
            for name in vital_names
        )
    else:
        lines.append(_paint("  Vitals unavailable.", "dim", color))
    lines.append(_paint("ATTRIBUTES", "bright_cyan", color))
    attributes = game.get("attributes")
    if isinstance(attributes, dict) and attributes:
        lines.extend(
            f"  {_format_attribute(name, value)}"
            for name, value in attributes.items()
        )
    elif isinstance(attributes, list) and attributes:
        lines.extend(f"  {_one_line(value, limit=width - 4)}" for value in attributes)
    else:
        lines.append(_paint("  Attributes unavailable.", "dim", color))

    lines.extend([rule, _paint("EQUIPMENT", "bright_cyan", color)])
    lines.append(
        f"  Verification: {_paint(equipment.get('state', 'unknown'), _state_style(equipment.get('state')), color)}"
    )
    wielded = equipment.get("wielded_weapons")
    wielded = wielded if isinstance(wielded, list) else []
    lines.append(
        "  Wielding: "
        + (
            ", ".join(_paint(_equipment_label(item), "green", color) for item in wielded)
            if wielded
            else _paint("nothing verified", "dim", color)
        )
    )
    equipped = equipment.get("equipped")
    equipped = equipped if isinstance(equipped, list) else []
    if equipped:
        lines.append("  Equipped:")
        lines.extend(
            f"    - {_paint(_equipment_label(item), 'green', color)}"
            for item in equipped
        )
    else:
        lines.append(_paint("  No equipped items were verified.", "dim", color))

    items = inventory.get("items")
    items = items if isinstance(items, list) else []
    lines.extend(
        [rule, _paint(f"INVENTORY ({len(items)} entries)", "bright_cyan", color)]
    )
    if items:
        for item in sorted(
            (item for item in items if isinstance(item, dict)),
            key=lambda item: str(item.get("name") or "").casefold(),
        ):
            quantity = item.get("quantity", 1)
            marker = " [equipped]" if item.get("equipped") else ""
            lines.append(
                f"  {_paint(str(quantity).rjust(5), 'yellow', color)} x "
                f"{_equipment_label(item)}{_paint(marker, 'green', color) if marker else ''}"
            )
    else:
        lines.append(_paint("  Inventory is empty or unavailable.", "dim", color))
    capacity = inventory.get("capacity")
    if isinstance(capacity, dict) and capacity.get("known") is True:
        load_parts = []
        for label, current_key, maximum_key in (
            ("items", "items", None),
            ("weight", "weight", "weight_max"),
            ("bulk", "bulk", "bulk_max"),
        ):
            current = capacity.get(current_key)
            maximum = capacity.get(maximum_key) if maximum_key else None
            if current is not None:
                load_parts.append(
                    f"{label} {current}/{maximum}" if maximum is not None else f"{label} {current}"
                )
        if load_parts:
            lines.append("  Carry capacity: " + ", ".join(load_parts))

    readiness_rows = abilities.get("spell_readiness")
    readiness_rows = readiness_rows if isinstance(readiness_rows, list) else []
    readiness_by_name = {
        str(item.get("name") or "").casefold(): item
        for item in readiness_rows
        if isinstance(item, dict) and item.get("name")
    }

    def append_abilities(group: str, heading: str) -> None:
        rows = abilities.get(group)
        rows = rows if isinstance(rows, list) else []
        lines.extend(
            [rule, _paint(f"{heading} ({len(rows)} known; scale 0-100)", "bright_cyan", color)]
        )
        if not rows:
            lines.append(_paint(f"  No {group} were reported.", "dim", color))
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = _one_line(row.get("name"), limit=32)
            value = row.get("ability")
            value_text = f"{value}/100" if value is not None else "unknown"
            metadata = []
            for label, key in (("school", "school"), ("level", "level"), ("mana", "mana")):
                if row.get(key) is not None:
                    metadata.append(f"{label} {row[key]}")
            if group == "spells":
                ready = readiness_by_name.get(str(row.get("name") or "").casefold())
                if isinstance(ready, dict) and ready.get("castable") is not None:
                    metadata.append("castable" if ready["castable"] else "not castable")
                    blockers = ready.get("blocked_by")
                    if isinstance(blockers, list) and blockers:
                        metadata.append("blocked: " + ", ".join(str(item) for item in blockers))
            suffix = f"  ({'; '.join(metadata)})" if metadata else ""
            lines.append(
                f"  {name:<32} "
                f"{_paint(value_text.rjust(9), _ability_value_style(value), color)}"
                f"{suffix}"
            )

    append_abilities("skills", "SKILLS")
    append_abilities("spells", "SPELLS")
    lines.extend(
        [
            heavy_rule,
            _paint("Press Esc or Enter to return to the live console.", "dim", color),
        ]
    )
    return "\n".join(lines)


def _safe_chat_text(value: Any) -> str:
    """Normalize chat text and strip terminal control characters."""

    without_terminal_sequences = re.sub(
        r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value or "")
    )
    collapsed = " ".join(without_terminal_sequences.split())
    return "".join(character for character in collapsed if character.isprintable())


def render_conversations(
    history: dict[str, Any], *, width: int | None = None, color: bool = False
) -> str:
    """Render recent incoming and outgoing in-game dialogue."""

    width = max(72, width or shutil.get_terminal_size((110, 32)).columns)
    character_name = _safe_chat_text(history.get("character_name")) or "Character"
    timezone_name = _safe_chat_text(history.get("timezone")) or "UTC"
    messages = history.get("messages")
    messages = messages if isinstance(messages, list) else []
    rule = _paint("-" * width, "blue", color)
    heavy_rule = _paint("=" * width, "blue", color)
    lines = [
        _paint("RECENT CHAT".center(width), "bright_cyan", color),
        heavy_rule,
        (
            f"Character {_paint(character_name, 'bright_white', color)} | "
            f"Times shown in {_paint(timezone_name, 'cyan', color)} | "
            f"{len(messages)} message{'s' if len(messages) != 1 else ''}"
        ),
        rule,
    ]
    if not messages:
        lines.append(_paint("No recent in-game chat is stored.", "dim", color))
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "speaker").casefold()
        speaker = _safe_chat_text(message.get("speaker")) or "unknown speaker"
        speaker_kind = _safe_chat_text(message.get("speaker_kind"))
        when = _safe_chat_text(
            message.get("display_occurred_at") or message.get("occurred_at")
        )
        if role == "assistant":
            direction = f"{character_name} -> {speaker}"
            style = "green"
        else:
            direction = f"{speaker} -> {character_name}"
            style = "yellow"
        metadata = f" ({speaker_kind})" if speaker_kind else ""
        lines.append(
            f"{_paint(when or '-', 'dim', color)}  "
            f"{_paint(direction, style, color)}{_paint(metadata, 'dim', color)}"
        )
        content = _safe_chat_text(message.get("content")) or "-"
        wrapped = textwrap.wrap(
            content,
            width=max(20, width - 4),
            break_long_words=True,
            break_on_hyphens=False,
        ) or ["-"]
        lines.extend(f"  {_paint(line, style, color)}" for line in wrapped)
        lines.append("")
    if lines[-1] == "":
        lines.pop()
    lines.extend(
        [
            heavy_rule,
            _paint("Press Esc or Enter to return to the live console.", "dim", color),
        ]
    )
    return "\n".join(lines)


def _read_terminal_line(prompt: str) -> str | None:
    """Read an editable terminal line, returning None immediately on Escape."""

    sys.stdout.write(prompt)
    sys.stdout.flush()
    characters: list[str] = []

    if os.name == "nt":
        import msvcrt

        while True:
            key = msvcrt.getwch()
            if key in {"\x00", "\xe0"}:
                msvcrt.getwch()
                continue
            if key == "\x1b":
                sys.stdout.write("\n")
                sys.stdout.flush()
                return None
            if key in {"\r", "\n"}:
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(characters)
            if key == "\x03":
                raise KeyboardInterrupt
            if key == "\b":
                if characters:
                    characters.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if key.isprintable():
                characters.append(key)
                sys.stdout.write(key)
                sys.stdout.flush()

    import termios
    import tty

    descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        while True:
            key = sys.stdin.read(1)
            if key == "\x1b":
                sys.stdout.write("\n")
                sys.stdout.flush()
                return None
            if key in {"\r", "\n"}:
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(characters)
            if key == "\x03":
                raise KeyboardInterrupt
            if key in {"\x7f", "\b"}:
                if characters:
                    characters.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if key.isprintable():
                characters.append(key)
                sys.stdout.write(key)
                sys.stdout.flush()
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, original)


def _form_input(
    prompt: str, input_fn: Callable[[str], str]
) -> str | None:
    """Use immediate Escape handling interactively while keeping tests injectable."""

    if (
        input_fn is input
        and bool(getattr(sys.stdin, "isatty", lambda: False)())
        and bool(getattr(sys.stdout, "isatty", lambda: False)())
    ):
        return _read_terminal_line(prompt)
    value = input_fn(prompt)
    return None if value == "\x1b" else value


def _prompt_required(
    prompt: str, input_fn: Callable[[str], str]
) -> str | None:
    while True:
        raw = _form_input(prompt, input_fn)
        if raw is None:
            return None
        value = raw.strip()
        if value:
            return value
        print("A value is required.")


def _prompt_integer(
    prompt: str,
    input_fn: Callable[[str], str],
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int | None:
    while True:
        entered = _form_input(prompt, input_fn)
        if entered is None:
            return None
        raw = entered.strip()
        try:
            value = int(raw) if raw else default
        except ValueError:
            print(f"Enter a whole number from {minimum} through {maximum}.")
            continue
        if minimum <= value <= maximum:
            return value
        print(f"Enter a whole number from {minimum} through {maximum}.")


def prompt_new_goal(
    api: ControllerApi,
    *,
    input_fn: Callable[[str], str] = input,
) -> dict[str, Any] | None:
    print("\nCreate a durable goal")
    print("Describe the outcome in plain language. The configured model will build the typed goal.")
    print("Press Esc at any prompt to cancel and return to the live console.")
    prompt = _prompt_required("What should the character accomplish? ", input_fn)
    if prompt is None:
        print("Goal creation cancelled; nothing was submitted.")
        return None
    current_goal: dict[str, Any] | None = None

    while True:
        print("\nAsking the configured model to construct and validate the goal...")
        try:
            response = api.draft_goal(prompt, current_goal=current_goal)
        except ControllerApiError as exc:
            print(f"\nThe draft did not pass validation: {exc}")
            errors = exc.details.get("errors", [])
            if isinstance(errors, list):
                for error in errors:
                    if not isinstance(error, dict):
                        continue
                    print(f"- {error.get('message', error.get('code', 'invalid draft'))}")
                    candidates = error.get("purchase_plan_candidates", [])
                    if isinstance(candidates, list):
                        for candidate in candidates[:5]:
                            if not isinstance(candidate, dict):
                                continue
                            print(
                                "  valid training option: "
                                f"{candidate.get('merchant_class')} in room {candidate.get('room_id')} "
                                f"for at most {candidate.get('maximum_price')}"
                            )
            failed_draft = exc.details.get("canonical_goal")
            if isinstance(failed_draft, dict):
                allowed = {
                    "title",
                    "objective",
                    "success_criteria",
                    "constraints",
                    "priority",
                    "activation",
                }
                current_goal = {
                    key: value for key, value in failed_draft.items() if key in allowed
                }
                print("The invalid structured draft has been retained for repair.")
            while True:
                recovery = _prompt_required(
                    "[R]etry, [M]odify the request, [C]ancel, or [Esc] back: ",
                    input_fn,
                )
                if recovery is None or recovery.casefold() in {"c", "cancel"}:
                    print("Goal creation cancelled; nothing was submitted.")
                    return None
                if recovery.casefold() in {"r", "retry"}:
                    break
                if recovery.casefold() in {"m", "modify"}:
                    revised_prompt = _prompt_required(
                        "Describe what the model should change ([Esc] back): ",
                        input_fn,
                    )
                    if revised_prompt is None:
                        print("Goal creation cancelled; nothing was submitted.")
                        return None
                    prompt = revised_prompt
                    break
                print("Choose R to retry, M to modify, or C to cancel.")
            continue
        draft = response.get("goal")
        if not isinstance(draft, dict):
            raise ControllerApiError("controller returned no structured goal draft")

        model_name = str(response.get("model") or "configured model")
        print(f"\nStructured goal draft from {model_name}")
        print("Nothing has been submitted yet.")
        print("Higher priority numbers run first: 0 is lowest, 100 is highest.")
        print(json.dumps(draft, indent=2, ensure_ascii=False, sort_keys=True))
        warnings = response.get("validation", {}).get("warnings", [])
        if isinstance(warnings, list) and warnings:
            print("\nValidation warnings")
            for warning in warnings:
                if isinstance(warning, dict):
                    print(f"- {warning.get('message', warning.get('code', 'warning'))}")

        while True:
            action = _prompt_required(
                "\n[A]pprove, [M]odify, [C]ancel, or [Esc] back: ",
                input_fn,
            )
            if action is None:
                print("Goal creation cancelled; nothing was submitted.")
                return None
            action = action.casefold()
            if action in {"a", "approve"}:
                return {**draft, "request_id": f"tui-goal-{uuid7()}"}
            if action in {"c", "cancel"}:
                print("Goal creation cancelled; nothing was submitted.")
                return None
            if action in {"m", "modify"}:
                prompt = _prompt_required(
                    "Describe what the model should change ([Esc] back): ", input_fn
                )
                if prompt is None:
                    print("Goal creation cancelled; nothing was submitted.")
                    return None
                current_goal = draft
                break
            print("Choose A to approve, M to modify, or C to cancel.")


def prompt_goal_command(
    goals: list[dict[str, Any]],
    *,
    input_fn: Callable[[str], str] = input,
    color: bool = False,
) -> tuple[str, dict[str, Any]] | None:
    open_goals = [
        goal
        for goal in goals
        if goal.get("status") in {"active", "queued", "paused", "blocked"}
    ]
    if not open_goals:
        print(_paint("There are no open goals to manage.", "dim", color))
        return None
    print()
    print(_paint("GOAL QUEUE MANAGEMENT", "bright_cyan", color))
    print(_paint("-" * 76, "blue", color))
    print(_paint(" #  STATUS       PRI  GOAL", "dim", color))
    for index, goal in enumerate(open_goals, 1):
        status = str(goal.get("status", "-"))
        status_label = f"[{status}]".ljust(12)
        priority_label = f"P{goal.get('priority', '-')}".rjust(4)
        print(
            f"{_paint(f'{index:>2}.', 'bright_white', color)} "
            f"{_paint(status_label, _state_style(status), color)} "
            f"{_paint(priority_label, 'magenta', color)}  "
            f"{_paint(goal.get('title', '?'), 'bright_white', color)}"
        )
    print(_paint("-" * 76, "blue", color))
    print(
        _paint("Tip:", "cyan", color)
        + " higher priority numbers run first; Esc returns without changing anything."
    )
    selected = _prompt_integer(
        f"{_paint('Goal number', 'bright_cyan', color)} ([Esc] back): ",
        input_fn,
        default=1,
        minimum=1,
        maximum=len(open_goals),
    )
    if selected is None:
        return None
    goal = open_goals[selected - 1]
    status = str(goal.get("status"))
    print(
        "\n"
        + _paint("Selected:", "cyan", color)
        + " "
        + _paint(goal.get("title", "?"), "bright_white", color)
        + " "
        + _paint(f"[{status}]", _state_style(status), color)
        + " "
        + _paint(f"P{goal.get('priority', '-')}", "magenta", color)
    )
    choices = ["reprioritize", "cancel"]
    if status in {"active", "queued"}:
        choices.insert(0, "pause")
    if status in {"paused", "blocked"}:
        choices.insert(0, "resume")
    success_criteria = goal.get("success_criteria")
    success_criteria = success_criteria if isinstance(success_criteria, list) else []
    if any(
        isinstance(item, dict) and item.get("kind") == "operator_confirmed"
        for item in success_criteria
    ):
        choices.append("confirm_complete")
        print(
            _paint(
                "This goal has a manual criterion. Confirmation is accepted only after every observable criterion passes.",
                "yellow",
                color,
            )
        )
    action_keys = {
        "pause": "p",
        "resume": "r",
        "reprioritize": "e",
        "cancel": "c",
        "confirm_complete": "f",
    }
    action_labels = {
        "pause": "[P]ause",
        "resume": "[R]esume",
        "reprioritize": "[E]dit priority",
        "cancel": "[C]ancel",
        "confirm_complete": "Con[F]irm manual criterion",
    }
    action_styles = {
        "pause": "yellow",
        "resume": "green",
        "reprioritize": "magenta",
        "cancel": "red",
        "confirm_complete": "green",
    }
    print(
        _paint("Actions:", "cyan", color)
        + " "
        + ", ".join(
            _paint(action_labels[choice], action_styles[choice], color)
            for choice in choices
        )
    )
    raw_action = _prompt_required(
        f"{_paint('Action', 'bright_cyan', color)} ([Esc] back): ", input_fn
    )
    if raw_action is None:
        return None
    raw_action = raw_action.casefold()
    action = next(
        (
            choice
            for choice in choices
            if raw_action in {choice, action_keys[choice]}
        ),
        None,
    )
    if action is None:
        print(_paint("Unknown action.", "red", color))
        return None
    payload: dict[str, Any] = {
        "request_id": f"tui-goal-command-{uuid7()}",
        "expected_version": int(goal.get("version", 0)),
        "action": action,
        "reason": "Operator command from the local goal console.",
    }
    if action == "reprioritize":
        priority = _prompt_integer(
            f"{_paint('New priority', 'bright_cyan', color)} "
            "(0 lowest, 100 highest) [50; Esc to go back]: ",
            input_fn,
            default=50,
            minimum=0,
            maximum=100,
        )
        if priority is None:
            return None
        payload["priority"] = priority
    if action == "cancel":
        confirmation = _form_input(
            "Type CANCEL to confirm permanent cancellation ([Esc] back): ",
            input_fn,
        )
        if confirmation is None:
            return None
        if confirmation.strip() != "CANCEL":
            print(_paint("Cancellation aborted.", "yellow", color))
            return None
        payload["cause"] = "operator_requested"
    if action == "confirm_complete":
        confirmation = _form_input(
            "Type CONFIRM to attest the manual criterion and complete the goal ([Esc] back): ",
            input_fn,
        )
        if confirmation is None:
            return None
        if confirmation.strip() != "CONFIRM":
            print(_paint("Confirmation aborted.", "yellow", color))
            return None
    return str(goal.get("id")), payload


def read_key(timeout: float) -> str | None:
    deadline = time.monotonic() + max(0.0, timeout)
    if os.name == "nt":
        import msvcrt

        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in {"\x00", "\xe0"}:
                    msvcrt.getwch()
                    continue
                return key.casefold()
            time.sleep(0.05)
        return None
    remaining = max(0.0, deadline - time.monotonic())
    ready, _, _ = select.select([sys.stdin], [], [], remaining)
    return sys.stdin.read(1).casefold() if ready else None


def _enable_virtual_terminal() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except (AttributeError, OSError, ValueError):
        return


def _draw(value: str) -> None:
    sys.stdout.write(ANSI_RESET + "\x1b[?25l\x1b[2J\x1b[H" + value)
    sys.stdout.flush()


def _form_screen() -> None:
    sys.stdout.write(ANSI_RESET + "\x1b[?25h\x1b[2J\x1b[H")
    sys.stdout.flush()


def prompt_safe_shutdown(
    *, input_fn: Callable[[str], str] = input, color: bool = False
) -> bool:
    """Require an explicit operator confirmation for coordinated shutdown."""

    print(_paint("COORDINATED SAFE SHUTDOWN", "bright_cyan", color))
    print(
        "This pauses every runnable goal, waits for foreground/keeper ownership, "
        "routes the character to a source-verified safe room, logs out without "
        "forgetting credentials, then stops the controller and owned broker."
    )
    confirmation = _form_input(
        "Type SHUTDOWN to proceed ([Esc] back): ", input_fn
    )
    return bool(
        confirmation is not None
        and confirmation.strip().casefold() == "shutdown"
    )


def run_tui(
    api: ControllerApi,
    *,
    refresh_seconds: float = 2.0,
    key_reader: Callable[[float], str | None] = read_key,
    input_fn: Callable[[str], str] = input,
) -> int:
    _enable_virtual_terminal()
    color = _terminal_colors_enabled()
    status: dict[str, Any] = {}
    goals: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    message = "Connecting to the controller..."
    manual_refresh_requested = False
    try:
        while True:
            try:
                status = api.status()
                goals = api.goals()
                events = api.events()
                if manual_refresh_requested:
                    message = (
                        "Manual refresh complete at "
                        + time.strftime("%H:%M:%S")
                        + "."
                    )
                elif message == "Connecting to the controller..." or message.startswith(
                    "controller unavailable"
                ):
                    message = "Controller connection restored."
            except ControllerApiError as exc:
                message = str(exc)
            manual_refresh_requested = False
            _draw(
                render_dashboard(
                    status, goals, events, message=message, color=color
                )
            )
            key = key_reader(refresh_seconds)
            if key is None:
                continue
            if key == "r":
                manual_refresh_requested = True
                continue
            if key == "\x1b":
                message = "Main console already active."
                continue
            if key == "q":
                return 0
            if key == "x":
                _form_screen()
                try:
                    if not prompt_safe_shutdown(input_fn=input_fn, color=color):
                        message = "Safe shutdown cancelled; the bot is still running."
                    else:
                        result = api.safe_stop()
                        shutdown = (
                            result.get("shutdown")
                            if isinstance(result.get("shutdown"), dict)
                            else {}
                        )
                        stage = shutdown.get("stage") or "requested"
                        message = (
                            f"Safe shutdown accepted [{stage}]. Keep this console "
                            "open to watch progress, or press Q to detach the TUI."
                        )
                except (ControllerApiError, ValueError, EOFError, KeyboardInterrupt) as exc:
                    message = f"Safe shutdown failed: {exc}"
                continue
            if key == "h":
                message = (
                    "N turns a plain-language request into a model-authored goal for your approval; "
                    "M manages goals (use F there to confirm a pending manual criterion); "
                    "S shows complete skills, spells, inventory, and equipment; "
                    "C shows recent incoming and outgoing in-game chat; "
                    "X requests coordinated pause, safe return, logout, and process shutdown; "
                    "Q only detaches this TUI and leaves the bot running; "
                    "Esc cancels any subpage and returns here."
                )
                continue
            if key == "c":
                _form_screen()
                try:
                    history = api.conversations()
                    print(render_conversations(history, color=color))
                    _form_input("", input_fn)
                    message = "Returned from recent chat."
                except (ControllerApiError, ValueError, EOFError, KeyboardInterrupt) as exc:
                    message = f"Recent chat failed: {exc}"
                continue
            if key == "s":
                _form_screen()
                try:
                    detail = api.character_status()
                    print(render_character_status(detail, color=color))
                    _form_input("", input_fn)
                    message = "Returned from detailed character status."
                except (ControllerApiError, ValueError, EOFError, KeyboardInterrupt) as exc:
                    message = f"Character status failed: {exc}"
                continue
            if key == "n":
                _form_screen()
                try:
                    payload = prompt_new_goal(api, input_fn=input_fn)
                    if payload is None:
                        message = "Goal creation cancelled; nothing was submitted."
                    else:
                        result = api.submit_goal(payload)
                        goal = result.get("goal") if isinstance(result.get("goal"), dict) else {}
                        message = f"Goal stored: {goal.get('title', goal.get('id', 'new goal'))}"
                except (ControllerApiError, ValueError, EOFError, KeyboardInterrupt) as exc:
                    message = f"Goal submission failed: {exc}"
                continue
            if key == "m":
                _form_screen()
                try:
                    command = prompt_goal_command(
                        goals, input_fn=input_fn, color=color
                    )
                    if command is None:
                        message = "No goal change was made."
                    else:
                        goal_id, payload = command
                        result = api.manage_goal(goal_id, payload)
                        changed = result.get("goal") if isinstance(result.get("goal"), dict) else {}
                        message = f"Goal is now {changed.get('status', 'updated')}: {changed.get('title', goal_id)}"
                except (ControllerApiError, ValueError, EOFError, KeyboardInterrupt) as exc:
                    message = f"Goal command failed: {exc}"
                continue
            message = f"Unknown key {key!r}; press H for help."
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write(f"{ANSI_RESET}\x1b[?25h\n")
        sys.stdout.flush()
