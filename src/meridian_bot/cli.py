from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from .api import ApiServers
from .broker import BrokerClient, BrokerError
from .config import BotConfig
from .controller import BotController
from .knowledge import KnowledgeBase
from .model import VllmClient
from .mcp import serve as serve_mcp
from .knowledge_mcp import serve as serve_knowledge_mcp
from .persona import PERSONA_FIELDS
from .singleton import InstanceLock
from .tui import ControllerApi, run_tui
from .utils import uuid7


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="m59-bot", description="Durable LLM controller for Meridian 59")
    value.add_argument("--config", default="config/bot.toml", help="controller TOML file")
    sub = value.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the controller, APIs, and game loop")
    run.add_argument("--no-connect", action="store_true", help="start APIs without broker/game startup (diagnostics only)")
    sub.add_parser("doctor", help="validate configuration and inspect local dependencies")
    sub.add_parser("once", help="run startup and one planning turn")
    persona = sub.add_parser(
        "setup-persona",
        help="interactively configure the first-run character persona",
    )
    persona.add_argument(
        "--input",
        metavar="JSON_FILE",
        help="read the persona object from a JSON file; use - for standard input",
    )
    persona.add_argument(
        "--update-existing",
        action="store_true",
        help="write a new persona version instead of preserving an existing one",
    )
    persona.add_argument(
        "--reuse-current",
        action="store_true",
        help="reuse the current persona (requires --update-existing)",
    )
    persona.add_argument(
        "--replace-existing-character",
        action="store_true",
        help="explicitly allow replacement of an established differently named character",
    )
    tui = sub.add_parser(
        "tui",
        help="monitor the running bot and manage its durable goal queue",
    )
    tui.add_argument(
        "--refresh-seconds",
        type=float,
        default=2.0,
        help="dashboard polling interval (default: 2 seconds)",
    )
    sub.add_parser("mcp", help="serve the six-tool bot-control MCP facade over stdio")
    sub.add_parser("knowledge-mcp", help="serve the read-only Meridian knowledge MCP facade over stdio")
    return value


def _prompt_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def prompt_persona(
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> dict[str, object]:
    """Collect a complete operator-authored persona without invoking a model."""

    output_fn("")
    output_fn("Character persona")
    output_fn("Define how the character identifies and speaks. This does not create a gameplay goal.")
    name = ""
    while not name:
        name = input_fn("Desired character name: ").strip()
        if not name:
            output_fn("A character name is required.")
    output_fn("")
    output_fn("Voice and identity concept")
    output_fn("Write one short paragraph: about 2-4 sentences or 40-100 words.")
    output_fn(
        "Include the character's broad archetype or background impression, emotional tone, "
        "social presence, and one meaningful tension or flaw."
    )
    output_fn(
        "This guides the initial build, roleplay-aware planning, and in-game dialogue. "
        "It never creates goals or overrides controller policy."
    )
    output_fn(
        "Keep detailed trait lists, speech rules, values, taboos, and relationship defaults "
        "for the focused questions that follow. Never include credentials or private data."
    )
    output_fn(
        'Example: "An observant roadside mystic who survives through patience and preparation. '
        "She is warm toward earnest travelers, skeptical of swagger, and quietly amused by "
        'danger, though caution makes her slow to trust."'
    )
    character_voice = ""
    while not character_voice:
        character_voice = input_fn(
            "Voice and identity concept (one paragraph): "
        ).strip()
        if not character_voice:
            output_fn("A voice and identity concept is required.")
    traits = _prompt_list(input_fn("Personality traits (comma-separated): "))
    speech_style = _prompt_list(input_fn("Speech-style rules (comma-separated): "))
    values = _prompt_list(input_fn("Values and motivations (comma-separated): "))
    taboos = _prompt_list(input_fn("Conversation taboos (comma-separated): "))
    relationship_defaults = input_fn(
        "Default posture toward strangers, friends, rivals, favors, and betrayals: "
    ).strip()
    while True:
        raw_maximum = input_fn("Maximum characters per in-game reply [360]: ").strip()
        try:
            maximum = int(raw_maximum or "360")
        except ValueError:
            output_fn("Enter a whole number from 1 through 1000.")
            continue
        if 1 <= maximum <= 1000:
            break
        output_fn("Enter a whole number from 1 through 1000.")
    return {
        "name": name,
        "character_voice": character_voice,
        "traits": traits,
        "speech_style": speech_style,
        "values": values,
        "taboos": taboos,
        "relationship_defaults": relationship_defaults,
        "max_reply_characters": maximum,
    }


def _persona_from_json(path: str) -> dict[str, object]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    value = json.loads(raw)
    if isinstance(value, dict) and isinstance(value.get("persona"), dict):
        value = value["persona"]
    if not isinstance(value, dict):
        raise ValueError("persona JSON must contain one object")
    return value


def setup_persona(
    config: BotConfig,
    *,
    input_path: str | None = None,
    update_existing: bool = False,
    reuse_current: bool = False,
    replace_existing_character: bool = False,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Persist first-run persona input before the live controller starts."""

    if reuse_current and not update_existing:
        raise ValueError("--reuse-current requires --update-existing")
    if replace_existing_character and not update_existing:
        raise ValueError(
            "--replace-existing-character requires --update-existing"
        )
    controller = BotController(config)
    try:
        current = controller.persona()
        current_version = int(current.get("version", 0))
        if current_version and not update_existing:
            output_fn(
                json.dumps(
                    {
                        "status": "preserved",
                        "version": current_version,
                        "name": current.get("name"),
                        "onboarding": current.get("onboarding"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if reuse_current:
            if not current_version:
                raise ValueError("no existing persona is available to reuse")
            persona = {
                key: current[key]
                for key in PERSONA_FIELDS
                if key in current
            }
        elif input_path:
            persona = _persona_from_json(input_path)
        else:
            persona = prompt_persona(input_fn=input_fn, output_fn=output_fn)
        result = controller.set_persona(
            {
                "request_id": f"local-persona-setup-{uuid7()}",
                "expected_version": current_version,
                "persona": persona,
                "replace_existing_character": replace_existing_character,
            }
        )
        output_fn(
            json.dumps(
                {
                    "status": "configured",
                    "version": result["version"],
                    "name": result["name"],
                    "onboarding": result["onboarding"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        controller.close()


def configure_logging(config: BotConfig) -> None:
    handler = RotatingFileHandler(config.deployment.log_dir / "controller.log", maxBytes=5_000_000, backupCount=10, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler(sys.stderr)])


def doctor(config: BotConfig) -> int:
    broker = BrokerClient(config)
    healthy = True
    checks: dict[str, object] = {
        "configuration": "valid",
        "database_parent": str(config.database_path.parent),
        "harness_root": "present" if config.harness.root.is_dir() else "missing",
        "harness_revision_expected": config.harness.expected_revision,
        "credentials": "configured" if config.secrets.get("M59_ACCOUNT_USERNAME") and config.secrets.get("M59_ACCOUNT_PASSWORD") else "missing",
        "control_token": "configured" if config.control_token else "missing",
        "obsidian": "enabled" if config.notifications.obsidian_enabled else "disabled",
    }
    if not config.harness.root.is_dir():
        healthy = False
    if not (
        config.secrets.get("M59_ACCOUNT_USERNAME")
        and config.secrets.get("M59_ACCOUNT_PASSWORD")
    ):
        healthy = False
    try:
        checks["broker"] = {"health": broker.health(), "tools": len(broker.capabilities())}
    except Exception as exc:
        healthy = False
        checks["broker"] = {"status": "unavailable", "reason": str(exc)}
    try:
        checks["knowledge"] = KnowledgeBase(config).metadata()
    except Exception as exc:
        healthy = False
        checks["knowledge"] = {"status": "unavailable", "reason": str(exc)}
    try:
        checks["model"] = VllmClient(config).health()
    except Exception as exc:
        healthy = False
        checks["model"] = {"status": "unavailable", "reason": str(exc)}
    checks["overall"] = "healthy" if healthy else "unhealthy"
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if healthy else 1


def run_controller(config: BotConfig, *, no_connect: bool = False, once: bool = False) -> int:
    configure_logging(config)
    controller = BotController(config)
    lock = InstanceLock(config.deployment.run_dir / f"{config.deployment.instance_id}.lock")
    with lock:
        servers: ApiServers | None = None
        try:
            if once:
                if no_connect:
                    raise ValueError("once cannot be combined with no-connect")
                controller.startup(connect_game=True)
                print(json.dumps(controller.turn(), ensure_ascii=False, indent=2))
                return 0
            servers = ApiServers(controller)
            servers.start()
            try:
                controller.startup(connect_game=not no_connect)
            except BrokerError as exc:
                # Keep the local status and MCP control surfaces available
                # while the durable loop retries a temporarily unavailable
                # harness or game server.
                logging.getLogger(__name__).exception("controller startup degraded")
                controller.dependencies["broker"] = "unhealthy"
                controller._degrade("broker", exc)

            def stop(*_: object) -> None:
                controller.safe_stop()

            signal.signal(signal.SIGINT, stop)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, stop)
            controller.run_forever()
        finally:
            if servers:
                servers.stop()
            controller.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "mcp":
        serve_mcp()
        return 0
    if args.command == "knowledge-mcp":
        serve_knowledge_mcp()
        return 0
    config = BotConfig.load(Path(args.config))
    if args.command == "setup-persona":
        return setup_persona(
            config,
            input_path=args.input,
            update_existing=args.update_existing,
            reuse_current=args.reuse_current,
            replace_existing_character=args.replace_existing_character,
        )
    if args.command == "tui":
        return run_tui(
            ControllerApi(config),
            refresh_seconds=max(0.25, args.refresh_seconds),
        )
    if args.command == "doctor":
        return doctor(config)
    if args.command == "once":
        return run_controller(config, once=True)
    if args.command == "run":
        return run_controller(config, no_connect=args.no_connect)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
