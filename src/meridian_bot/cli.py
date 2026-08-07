from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .api import ApiServers
from .broker import BrokerClient, BrokerError
from .config import BotConfig
from .controller import BotController
from .knowledge import KnowledgeBase
from .model import VllmClient
from .mcp import serve as serve_mcp
from .knowledge_mcp import serve as serve_knowledge_mcp
from .singleton import InstanceLock


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="m59-bot", description="Durable LLM controller for Meridian 59")
    value.add_argument("--config", default="config/bot.toml", help="controller TOML file")
    sub = value.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the controller, APIs, and game loop")
    run.add_argument("--no-connect", action="store_true", help="start APIs without broker/game startup (diagnostics only)")
    sub.add_parser("doctor", help="validate configuration and inspect local dependencies")
    sub.add_parser("once", help="run startup and one planning turn")
    sub.add_parser("mcp", help="serve the six-tool bot-control MCP facade over stdio")
    sub.add_parser("knowledge-mcp", help="serve the read-only Meridian knowledge MCP facade over stdio")
    return value


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
    if args.command == "doctor":
        return doctor(config)
    if args.command == "once":
        return run_controller(config, once=True)
    if args.command == "run":
        return run_controller(config, no_connect=args.no_connect)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
