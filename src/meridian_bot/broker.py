from __future__ import annotations

import copy
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import unquote
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import BotConfig
from .utils import parse_json_object, redact, uuid7


class BrokerError(RuntimeError):
    code = "BROKER_UNAVAILABLE"


class HarnessIncompatible(BrokerError):
    code = "HARNESS_INCOMPATIBLE"


class ToolCallError(BrokerError):
    code = "TOOL_CALL_FAILED"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]

    def planner_view(self) -> dict[str, Any]:
        schema = copy.deepcopy(self.schema)
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if isinstance(properties, dict):
            properties.pop("agent", None)
        required = schema.get("required") if isinstance(schema, dict) else None
        if isinstance(required, list):
            schema["required"] = [name for name in required if name != "agent"]
        return {
            "name": self.name,
            "description": self.description + " The controller selects the only configured character; never supply an agent id.",
            "input_schema": schema,
        }

    def accepts(self, argument: str) -> bool:
        properties = self.schema.get("properties", {}) if isinstance(self.schema, dict) else {}
        return isinstance(properties, dict) and argument in properties

    def validate(self, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        schema = self.schema
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        missing = [name for name in required if name not in arguments]
        if missing:
            raise ValueError(f"missing {self.name} argument(s): {', '.join(missing)}")
        unknown = set(arguments) - set(properties)
        if properties and unknown:
            raise ValueError(f"unknown {self.name} argument(s): {', '.join(sorted(unknown))}")
        for name, value in arguments.items():
            spec = properties.get(name, {})
            enum = spec.get("enum") if isinstance(spec, dict) else None
            if enum and value not in enum:
                raise ValueError(f"{self.name}.{name} must be one of {enum}")


# Session lifecycle, credential-bearing, debugging, and conversation tools are
# controller-owned. The planner sees every other ordinary-player capability.
CONTROLLER_ONLY_TOOLS = {
    "join",
    "leave",
    "fleet",
    "recording",
    "converse",
    "inbox",
    # Upstream fleet/operator controls are not character tactics. Exposing them
    # would let the game planner claim a human-piloted session or coordinate
    # other accounts, both outside this controller's single-character scope.
    "pilot",
    "spread",
    "quartermaster",
    # The upstream RTS gateway owns these asynchronous intent/token APIs. The
    # single-character planner uses ordinary synchronous broker tools and must
    # not compete with a separate controller for packet authority.
    "attack_intent",
    "move_intent",
    "context_intent",
    "cancel_action",
}


class BrokerClient:
    def __init__(self, config: BotConfig):
        self.config = config
        self.url = config.harness.control_url + "/"
        self._id = 0
        self._id_lock = threading.Lock()
        self._mutate_lock = threading.Lock()
        self._manifest: dict[str, Tool] | None = None
        self._process: subprocess.Popen[str] | None = None
        self._log_handle: Any = None

    def _next_id(self) -> int:
        with self._id_lock:
            self._id += 1
            return self._id

    def _json_request(self, url: str, payload: dict[str, Any] | None, timeout: float) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            headers={"content-type": "application/json", "accept": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise BrokerError(f"broker request failed: {exc}") from exc

    def health(self, timeout: float = 3) -> dict[str, Any]:
        value = self._json_request(self.config.harness.control_url + "/health", None, timeout)
        if not isinstance(value, dict) or not value.get("ok"):
            raise BrokerError("broker health response was not healthy")
        return redact(value)

    def dashboard_health(self, timeout: float = 3) -> dict[str, Any]:
        value = self._json_request(
            f"http://127.0.0.1:{self.config.harness.dashboard_port}/health",
            None,
            timeout,
        )
        if (
            not isinstance(value, dict)
            or not value.get("ok")
            or value.get("view") != "dashboard"
            or value.get("readonly") is not True
        ):
            raise BrokerError("broker dashboard health response was not healthy and read-only")
        return redact(value)

    def _launch_command(self, script: Path, control_port: int) -> list[str]:
        return [
            self.config.harness.node_executable,
            str(script),
            "--http",
            str(control_port),
            "--dashboard",
            str(self.config.harness.dashboard_port),
        ]

    def _launch_environment(self) -> dict[str, str]:
        """Build native harness paths without relying on URL pathname defaults."""

        env = os.environ.copy()
        substrate = self.config.harness.root / "substrate"
        env.update(
            {
                "M59_HOST": self.config.game.host,
                "M59_PORT": str(self.config.game.port),
                "M59_STATE_FILE": str(self.config.harness.state_file),
                "M59_BIND": "127.0.0.1",
                "M59_RSC_DIR": str(self.config.harness.state_file.parent / "rsc"),
                # Some upstream modules still derive defaults from
                # URL.pathname. Those paths remain percent-encoded when a
                # Windows checkout contains spaces, so provide every
                # broker-imported filesystem store in native Path form.
                "M59_MAP": str(substrate / "m59-map.json"),
                "M59_MAP_FILE": str(substrate / "m59-map.json"),
                "M59_CODE_EXITS": str(substrate / "m59-codeexits.json"),
                "M59_BAD_EXITS": str(substrate / "m59-badexits.json"),
                "M59_SPAWN_FILE": str(substrate / "m59-spawns.json"),
                "M59_SAFESPOT_FILE": str(substrate / "m59-safespots.json"),
                "M59_MERCHANTS": str(substrate / "m59-merchants.json"),
                "M59_SPELLS": str(substrate / "m59-spells.json"),
                "M59_ABILITY_DIR": str(substrate / "abilities"),
                "M59_BANK_DIR": str(substrate / "banks"),
                "M59_DESC_DIR": str(substrate / "descriptions"),
                "M59_HITS_DIR": str(substrate / "hits"),
                "M59_POSTMORTEM_DIR": str(substrate / "postmortems"),
                "M59_TRANSIT_DIR": str(substrate / "transits"),
                "M59_UPTIME_FILE": str(substrate / "keeper-uptime.jsonl"),
                "M59_ACTIVE_FILE": str(substrate / "keeper-active.json"),
                "M59_LEDGER_DIR": str(self.config.harness.state_file.parent / "history"),
                "M59_RECORD_DIR": str(self.config.harness.state_file.parent / "recordings"),
            }
        )
        return env

    def rpc(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30) -> Any:
        payload = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            payload["params"] = params
        response = self._json_request(self.url, payload, timeout)
        if not isinstance(response, dict):
            raise BrokerError("broker returned a non-object JSON-RPC response")
        if response.get("error"):
            raise BrokerError(str(response["error"].get("message", "JSON-RPC error")))
        return response.get("result")

    def capabilities(self, *, refresh: bool = False) -> dict[str, Tool]:
        if self._manifest is not None and not refresh:
            return self._manifest
        result = self.rpc("tools/list", timeout=10)
        listed = result.get("tools", []) if isinstance(result, dict) else []
        manifest: dict[str, Tool] = {}
        for raw in listed:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            tool = Tool(
                name=str(raw["name"]),
                description=str(raw.get("description", "")),
                schema=raw.get("inputSchema") if isinstance(raw.get("inputSchema"), dict) else {},
            )
            manifest[tool.name] = tool
        required = {
            "join",
            "leave",
            "look",
            "status",
            "inventory",
            "autopilot",
            "wait_for_event",
        }
        missing = required - set(manifest)
        if missing:
            raise HarnessIncompatible(f"broker is missing required tools: {', '.join(sorted(missing))}")
        self._manifest = manifest
        return manifest

    def planner_tools(self) -> list[dict[str, Any]]:
        return [
            tool.planner_view()
            for name, tool in sorted(self.capabilities().items())
            if name not in CONTROLLER_ONLY_TOOLS
        ]

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float = 180,
        mutation: bool = False,
    ) -> Any:
        tool = self.capabilities().get(name)
        if tool is None:
            raise HarnessIncompatible(f"unknown broker tool: {name}")
        tool.validate(arguments)

        def invoke() -> Any:
            result = self.rpc(
                "tools/call",
                {"name": name, "arguments": arguments},
                timeout=timeout,
            )
            if not isinstance(result, dict):
                raise BrokerError("tool result was not an object")
            blocks = result.get("content", [])
            text = blocks[0].get("text", "") if blocks and isinstance(blocks[0], dict) else ""
            if result.get("isError"):
                raise ToolCallError(text or f"{name} failed")
            if not text:
                return {}
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                try:
                    return parse_json_object(text)
                except (ValueError, json.JSONDecodeError):
                    return {"text": text}

        if mutation:
            with self._mutate_lock:
                return invoke()
        return invoke()

    def observe(self) -> dict[str, Any]:
        agent = self.config.game.agent
        look_arguments: dict[str, Any] = {"agent": agent}
        # The broker's text minimap contains the same live creature, player, exit,
        # and position markers a human sees in the client. Request it when the
        # installed harness supports it so the planner gets room geometry without
        # spending a separate turn on discovery.
        look_tool = self.capabilities().get("look")
        if look_tool and look_tool.accepts("minimap"):
            look_arguments["minimap"] = True
        look = self.call_tool("look", look_arguments, timeout=20)
        if isinstance(look, dict) and isinstance(look.get("minimap"), dict):
            minimap = look["minimap"]
            # Raw vector walls can be hundreds of KB. The readable picture is a
            # few KB and already carries the tactical symbols and coordinates.
            look["minimap"] = {
                key: copy.deepcopy(minimap[key])
                for key in ("text", "legend", "size", "wall_summary", "truncated")
                if key in minimap
            }
            look["minimap"]["note"] = (
                "Compact live tactical picture; raw vector walls are intentionally omitted "
                "from LLM context. The keeper uses the full geometry for movement and safe-spot tests."
            )
        status = self.call_tool("status", {"agent": agent, "brief": True}, timeout=20)
        inventory = self.call_tool("inventory", {"agent": agent}, timeout=20)
        observation = {
            "id": uuid7(),
            "observed_at": time.time(),
            "look": look,
            "status": status,
            "inventory": inventory,
        }
        # Spell knowledge is planner context, not an instruction to provision.
        # In particular, exposing Create Food here must never cause the controller
        # to insert food, reagent, funding, or eating work on the planner's behalf.
        if "spells" in self.capabilities():
            try:
                observation["spells"] = self.call_tool(
                    "spells", {"agent": agent}, timeout=10
                )
            except (BrokerError, ValueError):
                observation["spells"] = {"known": False, "spells": []}
        # Newer harness builds expose the server's equipment list directly. The
        # inventory request above already refreshes that list, so refresh=False
        # reads the verified result without spending another game-server request.
        # Keep the adapter compatible with older harness revisions where this
        # optional tool does not exist.
        if "equipment" in self.capabilities():
            try:
                observation["equipment"] = self.call_tool(
                    "equipment", {"agent": agent, "refresh": False}, timeout=10
                )
            except (BrokerError, ValueError):
                observation["equipment"] = {"known": False}
        # Ability values are maintained by broker-side server pushes, so this is
        # a cheap cache read rather than four fresh game requests every turn. It
        # lets the controller emit sparse learned-skill and five-point milestones
        # instead of asking the journal LLM to infer progression from combat.
        if "abilities" in self.capabilities():
            try:
                observation["abilities"] = self.call_tool(
                    "abilities",
                    {"agent": agent, "known_only": True},
                    timeout=10,
                )
            except (BrokerError, ValueError):
                observation["abilities"] = {
                    "skills": [],
                    "spells": [],
                    "freshness": {"known": False},
                }
        return observation

    def ensure_started(self, startup_timeout: float = 20) -> dict[str, Any]:
        self.verify_revision()
        try:
            health = self.health()
            self._check_root(health)
        except HarnessIncompatible:
            raise
        except BrokerError:
            if self.config.harness.lifecycle != "controller_managed":
                raise
        else:
            # A healthy control socket does not prove that the separately bound,
            # read-only fleet dashboard was launched.  Attaching silently to a
            # broker started with only ``--http`` strands LAN dashboard users on
            # a stale page, while starting a second broker would race the live
            # character.  Refuse the ambiguous attachment and say exactly how
            # the existing process must be restarted.
            try:
                health["dashboard"] = self.dashboard_health()
            except BrokerError as exc:
                raise HarnessIncompatible(
                    "healthy broker is missing the configured read-only dashboard at "
                    f"127.0.0.1:{self.config.harness.dashboard_port}; restart that broker with "
                    f"--dashboard {self.config.harness.dashboard_port} instead of starting a duplicate"
                ) from exc
            return health
        if self._process and self._process.poll() is None:
            raise BrokerError("managed broker is running but not healthy")
        script = self.config.harness.root / "tools" / "m59-broker.mjs"
        if not script.is_file():
            raise HarnessIncompatible(f"broker script missing under configured harness root: {script}")
        port = int(self.config.harness.control_url.rsplit(":", 1)[-1])
        env = self._launch_environment()
        log_path = self.config.deployment.log_dir / "harness-broker.log"
        if self._log_handle is not None:
            self._log_handle.close()
        log_handle = log_path.open("a", encoding="utf-8")
        self._log_handle = log_handle
        self._process = subprocess.Popen(
            self._launch_command(script, port),
            cwd=self.config.harness.root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise BrokerError(f"managed broker exited with code {self._process.returncode}")
            try:
                health = self.health()
                self._check_root(health)
                health["dashboard"] = self.dashboard_health(timeout=1)
                return health
            except HarnessIncompatible:
                raise
            except BrokerError:
                time.sleep(0.25)
        raise BrokerError("managed broker did not become healthy")

    def verify_revision(self) -> None:
        expected = self.config.harness.expected_revision.strip()
        if not expected:
            return
        try:
            result = subprocess.run(
                ["git", "-C", str(self.config.harness.root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HarnessIncompatible(f"could not verify pinned harness revision: {exc}") from exc
        actual = result.stdout.strip()
        if actual != expected:
            raise HarnessIncompatible(f"harness revision mismatch: expected {expected}, found {actual}")

    def _check_root(self, health: dict[str, Any]) -> None:
        reported = health.get("root")
        # The harness currently reports URL.pathname, which is percent-encoded
        # when a Windows checkout path contains spaces.
        if reported and Path(unquote(str(reported))).resolve() != self.config.harness.root.resolve():
            raise HarnessIncompatible("healthy broker belongs to a different harness checkout")

    def ensure_joined(self) -> Any:
        health = self.health()
        if self.config.game.agent in health.get("sessions", []):
            # `/health.sessions` is meant to contain only live sessions, but a
            # startup race in older harnesses exposed the Session object while
            # its asynchronous login was still running. Verify with an ordinary
            # read before authorizing startup mutations such as autopilot.
            try:
                self.call_tool(
                    "status",
                    {"agent": self.config.game.agent, "brief": True},
                    timeout=10,
                )
            except ToolCallError as exc:
                if "not in game" not in str(exc).casefold():
                    raise
            else:
                return {"already_joined": True}
        username = self.config.secrets.get("M59_ACCOUNT_USERNAME")
        password = self.config.secrets.get("M59_ACCOUNT_PASSWORD")
        if not username or not password:
            raise BrokerError("account credentials are absent from the private secret environment")
        arguments: dict[str, Any] = {
            "agent": self.config.game.agent,
            "account": username,
            "password": password,
            "host": self.config.game.host,
            "port": self.config.game.port,
        }
        if self.config.game.character:
            arguments["character"] = self.config.game.character
        # Credentials go only to the loopback broker and are never returned/logged by this adapter.
        self.call_tool("join", arguments, timeout=45, mutation=True)
        return {"joined": True}

    def shutdown_owned_process(self) -> None:
        # Deliberately do not invoke harness `leave`: a broker restart can resume
        # its stored session. Process shutdown is only for a broker we spawned.
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
