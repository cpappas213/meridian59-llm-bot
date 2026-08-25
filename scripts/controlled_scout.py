"""Move the keeper to a candidate room, then hand it back to survival mode.

This is an operator recovery/scouting aid.  It deliberately uses ordinary
keeper travel, never reconnects for an entry-grace advantage, and never leaves
farm mode running after arrival or after a safety/timeout interrupt.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from meridian_bot.broker import BrokerClient
from meridian_bot.config import BotConfig


def health_value(status: dict[str, Any]) -> tuple[int | None, int | None]:
    health = status.get("vitals", {}).get("health", {})
    if not isinstance(health, dict):
        return None, None
    current = health.get("current", health.get("value"))
    maximum = health.get("max")
    return (
        int(current) if isinstance(current, (int, float)) else None,
        int(maximum) if isinstance(maximum, (int, float)) else None,
    )


def survive(broker: BrokerClient, agent: str) -> dict[str, Any]:
    return broker.call_tool(
        "autopilot",
        {
            "agent": agent,
            "action": "start",
            "automated_pleas": False,
            "mode": "survive",
            "rest_below": 0.7,
            "flee_below": 0.7,
            "max_carry": 35,
            "drop_junk": True,
            "bank_above": 25,
            "fight_above_vigor": 80,
            "use_safe_spots": True,
            "hold_resume_above": 0.9,
            "break_out_via_logoff": False,
        },
        timeout=30,
        mutation=True,
    )


def stand(broker: BrokerClient, agent: str) -> dict[str, Any]:
    """Clear Meridian's silent REST/NO_MOVE state before asking for travel."""
    return broker.call_tool(
        "rest",
        {"agent": agent, "stand": True},
        timeout=10,
        mutation=True,
    )


def stop_and_wait(
    broker: BrokerClient, agent: str, *, timeout: float = 30
) -> dict[str, Any]:
    """Let the current keeper pass finish before replacing its job."""
    status = broker.call_tool(
        "autopilot", {"agent": agent, "action": "status"}, timeout=10
    )
    if not status.get("running"):
        return status
    broker.call_tool(
        "autopilot",
        {"agent": agent, "action": "stop"},
        timeout=10,
        mutation=True,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = broker.call_tool(
            "autopilot", {"agent": agent, "action": "status"}, timeout=10
        )
        if not status.get("running"):
            return status
        time.sleep(1)
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--room", type=int, required=True)
    parser.add_argument("--prey", required=True)
    parser.add_argument("--timeout", type=int, default=55)
    parser.add_argument("--minimum-health-fraction", type=float, default=0.75)
    args = parser.parse_args()

    config = BotConfig.load(args.config)
    broker = BrokerClient(config)
    agent = config.game.agent
    trail: list[dict[str, Any]] = []
    last_room: int | None = None
    reason = "timeout"
    stable_arrival_polls = 0

    # Do not mutate the policy of an old loop while it is still walking.  The
    # upstream keeper applies mode changes immediately but cannot interrupt the
    # in-flight travel pass, which can otherwise make two scouting jobs overlap.
    stop_and_wait(broker, agent)
    stand(broker, agent)
    broker.call_tool(
        "autopilot",
        {
            "agent": agent,
            "action": "start",
            "automated_pleas": False,
            "mode": "farm",
            "hunt": args.prey,
            "assigned_room": args.room,
            "rest_below": 0.7,
            "flee_below": max(0.75, args.minimum_health_fraction),
            "max_carry": 35,
            "drop_junk": True,
            "bank_above": 25,
            "fight_above_vigor": 80,
            "use_safe_spots": True,
            "hold_resume_above": 0.95,
            "pull_within": 6,
            "break_out_via_logoff": False,
        },
        timeout=30,
        mutation=True,
    )

    deadline = time.monotonic() + max(5, args.timeout)
    try:
        while time.monotonic() < deadline:
            look = broker.call_tool("look", {"agent": agent}, timeout=10)
            status = broker.call_tool(
                "status", {"agent": agent, "brief": True}, timeout=10
            )
            keeper = broker.call_tool(
                "autopilot", {"agent": agent, "action": "status"}, timeout=10
            )
            room = look.get("room", {}) if isinstance(look, dict) else {}
            room_id = room.get("num", room.get("id")) if isinstance(room, dict) else None
            current, maximum = health_value(status if isinstance(status, dict) else {})
            if room_id != last_room:
                trail.append(
                    {
                        "room": room_id,
                        "name": room.get("name") if isinstance(room, dict) else None,
                        "health": current,
                        "max_health": maximum,
                    }
                )
                last_room = room_id
            settled = not status.get("busy") and keeper.get("activity") != "travelling"
            stable_arrival_polls = (
                stable_arrival_polls + 1
                if room_id == args.room and settled
                else 0
            )
            if stable_arrival_polls >= 2:
                reason = "arrived"
                break
            if (
                current is not None
                and maximum
                and current / maximum <= args.minimum_health_fraction
            ):
                reason = "health_threshold"
                break
            time.sleep(1)
    finally:
        stop_and_wait(broker, agent)
        survival = survive(broker, agent)

    final_look = broker.call_tool("look", {"agent": agent}, timeout=10)
    final_status = broker.call_tool(
        "status", {"agent": agent, "brief": True}, timeout=10
    )
    current, maximum = health_value(final_status)
    room = final_look.get("room", {})
    print(
        json.dumps(
            {
                "reason": reason,
                "trail": trail,
                "room": room,
                "position": final_status.get("position"),
                "health": {"current": current, "max": maximum},
                "objects": [
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "distance": item.get("distance"),
                        "is_player": item.get("is_player"),
                        "can": item.get("can"),
                    }
                    for item in final_look.get("objects", [])
                    if isinstance(item, dict)
                ],
                "survival": {
                    "running": survival.get("running"),
                    "mode": survival.get("mode"),
                    "activity": survival.get("activity"),
                },
            },
            indent=2,
        )
    )
    return 0 if reason == "arrived" else 2


if __name__ == "__main__":
    raise SystemExit(main())
