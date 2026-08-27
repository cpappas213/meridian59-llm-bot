from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from meridian_bot.broker import (
    APPROVED_BROKER_TOOLS,
    BrokerClient,
    BrokerError,
    CONTROLLER_ONLY_TOOLS,
    FORBIDDEN_CHARACTER_LIFECYCLE_TOOLS,
    HarnessIncompatible,
    Tool,
    ToolCallError,
    guard_controller_tool_call,
)

from .helpers import config


class BrokerTests(unittest.TestCase):
    def test_reroll_is_filtered_from_controller_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            tool_names = {
                "join",
                "leave",
                "look",
                "status",
                "inventory",
                "autopilot",
                "wait_for_event",
                "reroll",
                "restart_character",
            }

            def fake_rpc(
                method: str,
                params: dict[str, object] | None = None,
                timeout: float = 30,
            ) -> dict[str, object]:
                self.assertEqual("tools/list", method)
                return {
                    "tools": [
                        {
                            "name": name,
                            "description": name,
                            "inputSchema": {
                                "type": "object",
                                "properties": {},
                            },
                        }
                        for name in sorted(tool_names)
                    ]
                }

            broker.rpc = fake_rpc  # type: ignore[method-assign]

            capabilities = broker.capabilities(refresh=True)
            planner_names = {tool["name"] for tool in broker.planner_tools()}

            self.assertEqual(frozenset({"reroll"}), FORBIDDEN_CHARACTER_LIFECYCLE_TOOLS)
            self.assertNotIn("reroll", APPROVED_BROKER_TOOLS)
            self.assertNotIn("reroll", capabilities)
            self.assertNotIn("reroll", planner_names)
            self.assertNotIn("restart_character", capabilities)
            self.assertNotIn("restart_character", planner_names)

    def test_every_reroll_action_is_rejected_before_any_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            broker._manifest = {
                "reroll": Tool(
                    "reroll",
                    "Destructive character replacement.",
                    {
                        "type": "object",
                        "properties": {
                            "action": {"enum": ["plan", "verify", "reroll"]},
                            "confirm": {"type": "boolean"},
                        },
                        "required": ["action"],
                    },
                )
            }
            requests: list[object] = []
            broker._json_request = (  # type: ignore[method-assign]
                lambda *_args, **_kwargs: requests.append((_args, _kwargs))
            )

            for arguments in (
                {"action": "plan"},
                {"action": "verify"},
                {"action": "reroll", "confirm": True},
            ):
                with self.subTest(arguments=arguments):
                    with self.assertRaisesRegex(ToolCallError, "permanently disabled"):
                        broker.call_tool("reroll", arguments)
                    with self.assertRaisesRegex(ToolCallError, "permanently disabled"):
                        broker.rpc(
                            "tools/call",
                            {"name": "reroll", "arguments": arguments},
                        )

            self.assertEqual([], requests)

    def test_controller_cannot_forget_a_character_login(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            broker._manifest = {
                "leave": Tool(
                    "leave",
                    "Log out.",
                    {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            "forget": {"type": "boolean"},
                        },
                        "required": ["agent"],
                    },
                )
            }

            requests: list[dict[str, object]] = []

            def fake_json_request(
                _url: str,
                payload: dict[str, object],
                _timeout: float,
            ) -> dict[str, object]:
                requests.append(payload)
                return {"result": {"content": [{"text": "{}"}]}}

            broker._json_request = fake_json_request  # type: ignore[method-assign]

            for forget in (True, 1, 0, "false", None, []):
                with self.subTest(forget=forget):
                    with self.assertRaisesRegex(ToolCallError, "literal boolean false"):
                        broker.call_tool(
                            "leave",
                            {"agent": "primary", "forget": forget},
                            mutation=True,
                        )
                    with self.assertRaisesRegex(ToolCallError, "literal boolean false"):
                        broker.rpc(
                            "tools/call",
                            {
                                "name": "leave",
                                "arguments": {"agent": "primary", "forget": forget},
                            },
                        )

            self.assertEqual([], requests)
            broker.call_tool("leave", {"agent": "primary"}, mutation=True)
            wire_arguments = requests[-1]["params"]["arguments"]  # type: ignore[index]
            self.assertIs(wire_arguments["forget"], False)  # type: ignore[index]

    def test_unreviewed_upstream_tool_is_rejected_before_any_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            requests: list[object] = []
            broker._json_request = (  # type: ignore[method-assign]
                lambda *_args, **_kwargs: requests.append((_args, _kwargs))
            )

            for invoke in (
                lambda: broker.call_tool(
                    "restart_character", {"confirm": True}, mutation=True
                ),
                lambda: broker.rpc(
                    "tools/call",
                    {
                        "name": "restart_character",
                        "arguments": {"confirm": True},
                    },
                ),
            ):
                with self.assertRaisesRegex(ToolCallError, "fail-closed"):
                    invoke()

            self.assertEqual([], requests)

    def test_lifecycle_action_added_under_approved_tool_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            broker._manifest = {
                "act": Tool(
                    "act",
                    "Generic ordinary-client action.",
                    {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            "verb": {
                                "type": "string",
                                "enum": [
                                    "get",
                                    "suicide",
                                    "delete_character",
                                    "perform_suicide",
                                    "force-reroll",
                                    "restartCharacter",
                                ],
                            },
                            "options": {
                                "type": "object",
                                "properties": {
                                    "onDeath": {
                                        "type": "string",
                                        "enum": ["reset"],
                                    }
                                },
                            },
                        },
                    },
                )
            }
            requests: list[object] = []
            broker._json_request = (  # type: ignore[method-assign]
                lambda *_args, **_kwargs: requests.append((_args, _kwargs))
            )

            planner = broker._manifest["act"].planner_view()
            self.assertEqual(
                ["get"], planner["input_schema"]["properties"]["verb"]["enum"]
            )
            self.assertNotIn("options", planner["input_schema"]["properties"])
            for arguments in (
                {"agent": "primary", "verb": "suicide"},
                {"agent": "primary", "verb": "delete_character"},
                {"agent": "primary", "verb": "perform_suicide"},
                {
                    "agent": "primary",
                    "options": {"operation": "force-reroll"},
                },
                {"agent": "primary", "restartCharacter": True},
                {
                    "agent": "primary",
                    "options": {"on_death": "reroll"},
                },
                {"agent": "primary", "command": "auto_reroll"},
                {"agent": "primary", "suicide_on_failure": True},
                {
                    "agent": "primary",
                    "character_lifecycle": {"operation": "reset"},
                },
                {
                    "agent": "primary",
                    "action": "delete",
                    "target": "character",
                },
                {
                    "agent": "primary",
                    "character": {"operation": "reset"},
                },
                {
                    "agent": "primary",
                    "characterLifecycle": {"operation": "reset"},
                },
                {
                    "agent": "primary",
                    "onDeath": "reset",
                },
            ):
                with self.subTest(arguments=arguments):
                    with self.assertRaisesRegex(
                        ToolCallError, "character-lifecycle directives"
                    ):
                        broker.call_tool("act", arguments, mutation=True)
                    with self.assertRaisesRegex(
                        ToolCallError, "character-lifecycle directives"
                    ):
                        broker.rpc(
                            "tools/call",
                            {"name": "act", "arguments": arguments},
                        )

            self.assertEqual([], requests)
            self.assertEqual(
                {"agent": "primary", "spell": "Forget"},
                guard_controller_tool_call(
                    "cast", {"agent": "primary", "spell": "Forget"}
                ),
            )
            opaque_join = {
                "agent": "reset",
                "account": "new",
                "password": "auto-reroll",
                "character": "Reset",
                "host": "suicide",
            }
            self.assertEqual(
                opaque_join,
                guard_controller_tool_call("join", opaque_join),
            )

    def test_managed_launch_command_includes_separate_read_only_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))

            command = broker._launch_command(Path(temporary) / "m59-broker.mjs", 8901)

            self.assertEqual(
                [
                    broker.config.harness.node_executable,
                    str(Path(temporary) / "m59-broker.mjs"),
                    "--http",
                    "8901",
                    "--dashboard",
                    "8902",
                    "--in-process",
                ],
                command,
            )

    def test_travel_hides_and_disables_upstream_errand_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            broker._manifest = {
                "travel": Tool(
                    "travel",
                    "Travel to a room.",
                    {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            "to": {"type": ["string", "number"]},
                            "run_errands": {"type": "boolean"},
                        },
                        "required": ["agent", "to"],
                    },
                )
            }
            sent: dict[str, object] = {}

            def fake_rpc(
                method: str,
                params: dict[str, object] | None = None,
                timeout: float = 30,
            ) -> dict[str, object]:
                sent.update(params or {})
                return {"content": [{"text": "{}"}]}

            broker.rpc = fake_rpc  # type: ignore[method-assign]

            view = broker._manifest["travel"].planner_view()
            result = broker.call_tool(
                "travel",
                {"agent": "primary", "to": 42, "run_errands": True},
                mutation=True,
            )

            self.assertNotIn("run_errands", view["input_schema"]["properties"])
            self.assertEqual({}, result)
            self.assertEqual(
                {"agent": "primary", "to": 42, "run_errands": False},
                sent["arguments"],
            )

    def test_experimental_upstream_controls_are_not_planner_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            broker._manifest = {
                "jump": Tool(
                    "jump",
                    "Attempt a declared ledge jump.",
                    {"type": "object", "properties": {}},
                ),
                "autopilot": Tool(
                    "autopilot",
                    "Configure the keeper.",
                    {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["start", "stop"],
                            },
                            "mode": {
                                "type": "string",
                                "enum": ["survive", "farm", "idle", "tick"],
                            },
                            "travel_deaths_allowed": {"type": "number"},
                        },
                    },
                ),
            }
            sent: dict[str, object] = {}

            def fake_rpc(
                method: str,
                params: dict[str, object] | None = None,
                timeout: float = 30,
            ) -> dict[str, object]:
                sent.update(params or {})
                return {"content": [{"text": "{}"}]}

            broker.rpc = fake_rpc  # type: ignore[method-assign]

            visible = {tool["name"]: tool for tool in broker.planner_tools()}
            autopilot_schema = visible["autopilot"]["input_schema"]["properties"]

            self.assertNotIn("jump", visible)
            self.assertNotIn("travel_deaths_allowed", autopilot_schema)
            self.assertEqual(
                ["survive", "farm", "idle"],
                autopilot_schema["mode"]["enum"],
            )
            broker.call_tool(
                "autopilot",
                {
                    "action": "start",
                    "mode": "farm",
                    "travel_deaths_allowed": 3,
                },
            )
            self.assertEqual(0, sent["arguments"]["travel_deaths_allowed"])
            with self.assertRaisesRegex(ValueError, "unsupported experimental"):
                broker.call_tool("autopilot", {"mode": "tick"})

    def test_attach_refuses_broker_missing_configured_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            broker.verify_revision = lambda: None  # type: ignore[method-assign]
            broker.health = lambda timeout=3: {  # type: ignore[method-assign]
                "ok": True,
                "root": str(broker.config.harness.root),
            }

            def missing_dashboard(timeout: float = 3) -> dict[str, object]:
                raise BrokerError("connection refused")

            broker.dashboard_health = missing_dashboard  # type: ignore[method-assign]

            with self.assertRaisesRegex(
                HarnessIncompatible, "missing the configured read-only dashboard"
            ):
                broker.ensure_started()

    def test_attach_reports_verified_dashboard_health(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            broker.verify_revision = lambda: None  # type: ignore[method-assign]
            broker.health = lambda timeout=3: {  # type: ignore[method-assign]
                "ok": True,
                "root": str(broker.config.harness.root),
            }
            broker.dashboard_health = lambda timeout=3: {  # type: ignore[method-assign]
                "ok": True,
                "view": "dashboard",
                "readonly": True,
            }

            health = broker.ensure_started()

            self.assertTrue(health["dashboard"]["readonly"])

    def test_ensure_joined_verifies_health_session_before_startup_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            broker.health = lambda timeout=3: {  # type: ignore[method-assign]
                "ok": True,
                "sessions": ["primary"],
            }
            calls: list[tuple[str, dict[str, object]]] = []

            def fake_call(name: str, arguments: dict[str, object], **_kwargs: object) -> dict[str, object]:
                calls.append((name, dict(arguments)))
                return {"character": "TestHero"}

            broker.call_tool = fake_call  # type: ignore[method-assign]

            result = broker.ensure_joined()

            self.assertEqual({"already_joined": True}, result)
            self.assertEqual(
                [("status", {"agent": "primary", "brief": True})],
                calls,
            )

    def test_ensure_joined_rejoins_health_session_that_is_not_in_game(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            broker.config.secrets.update(
                {
                    "M59_ACCOUNT_USERNAME": "account",
                    "M59_ACCOUNT_PASSWORD": "password",
                }
            )
            broker.health = lambda timeout=3: {  # type: ignore[method-assign]
                "ok": True,
                "sessions": ["primary"],
            }
            calls: list[tuple[str, dict[str, object]]] = []

            def fake_call(name: str, arguments: dict[str, object], **_kwargs: object) -> dict[str, object]:
                calls.append((name, dict(arguments)))
                if name == "status":
                    raise ToolCallError('agent "primary" is not in game — call join first')
                return {"joined": True}

            broker.call_tool = fake_call  # type: ignore[method-assign]

            result = broker.ensure_joined()

            self.assertEqual({"joined": True}, result)
            self.assertEqual(["status", "join"], [name for name, _ in calls])

    def test_observe_includes_compact_live_minimap_without_vector_walls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            broker._manifest = {
                name: Tool(
                    name,
                    name,
                    {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            **({"minimap": {"type": "boolean"}} if name == "look" else {}),
                            **({"brief": {"type": "boolean"}} if name == "status" else {}),
                        },
                        "required": ["agent"],
                    },
                )
                for name in ("look", "status", "inventory")
            }
            calls: list[tuple[str, dict[str, object]]] = []

            def fake_call(name: str, arguments: dict[str, object], **_kwargs: object) -> dict[str, object]:
                calls.append((name, arguments))
                if name == "look":
                    return {
                        "objects": [{"id": 9, "name": "baby spider", "col": 4, "row": 7}],
                        "minimap": {
                            "text": "  12345\n7 ...a@",
                            "legend": {"a": "baby spider (id 9)", "@": "you"},
                            "size": {"rows": 9, "cols": 5},
                            "wall_summary": {"total": 100},
                            "truncated": False,
                            "walls": [[0, 1], [1, 2]],
                            "key": {"unused": True},
                        },
                    }
                return {}

            broker.call_tool = fake_call  # type: ignore[method-assign]

            observation = broker.observe()

            self.assertIn(("look", {"agent": "primary", "minimap": True}), calls)
            minimap = observation["look"]["minimap"]
            self.assertEqual("  12345\n7 ...a@", minimap["text"])
            self.assertEqual("baby spider (id 9)", minimap["legend"]["a"])
            self.assertNotIn("walls", minimap)
            self.assertNotIn("key", minimap)
            self.assertIn("keeper uses the full geometry", minimap["note"])

    def test_observe_reads_server_verified_equipment_without_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            broker._manifest = {
                name: Tool(
                    name,
                    name,
                    {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            **({"brief": {"type": "boolean"}} if name == "status" else {}),
                            **({"refresh": {"type": "boolean"}} if name == "equipment" else {}),
                        },
                        "required": ["agent"],
                    },
                )
                for name in ("look", "status", "inventory", "equipment")
            }
            calls: list[tuple[str, dict[str, object]]] = []

            def fake_call(name: str, arguments: dict[str, object], **_kwargs: object) -> dict[str, object]:
                calls.append((name, arguments))
                if name == "equipment":
                    return {"known": True, "equipped": [{"id": 7, "name": "mace"}]}
                return {}

            broker.call_tool = fake_call  # type: ignore[method-assign]

            observation = broker.observe()

            self.assertTrue(observation["equipment"]["known"])
            self.assertIn(("equipment", {"agent": "primary", "refresh": False}), calls)

    def test_observe_reads_cached_abilities_for_progress_milestones(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            broker._manifest = {
                name: Tool(
                    name,
                    name,
                    {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            **({"brief": {"type": "boolean"}} if name == "status" else {}),
                            **({"known_only": {"type": "boolean"}} if name == "abilities" else {}),
                        },
                        "required": ["agent"],
                    },
                )
                for name in ("look", "status", "inventory", "abilities")
            }
            calls: list[tuple[str, dict[str, object]]] = []

            def fake_call(name: str, arguments: dict[str, object], **_kwargs: object) -> dict[str, object]:
                calls.append((name, arguments))
                if name == "abilities":
                    return {"skills": [], "spells": [{"name": "Blink", "ability": 5}]}
                return {}

            broker.call_tool = fake_call  # type: ignore[method-assign]
            observation = broker.observe()

            self.assertEqual(5, observation["abilities"]["spells"][0]["ability"])
            self.assertIn(("abilities", {"agent": "primary", "known_only": True}), calls)

    def test_observe_uses_only_cached_tactical_reads_during_keeper_retreat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            broker._manifest = {
                name: Tool(
                    name,
                    name,
                    {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            **(
                                {
                                    "cached": {"type": "boolean"},
                                    "minimap": {"type": "boolean"},
                                }
                                if name == "look"
                                else {}
                            ),
                            **({"brief": {"type": "boolean"}} if name == "status" else {}),
                            **({"action": {"type": "string"}} if name == "autopilot" else {}),
                        },
                        "required": ["agent"],
                    },
                )
                for name in ("look", "status", "inventory", "spells", "autopilot")
            }
            broker._observation_cache = {
                "look": {
                    "room": {"num": 562, "name": "The Ocean"},
                    "vitals": {"health": {"current": 30, "max": 40}},
                },
                "status": {"vitals": {"health": {"current": 30, "max": 40}}},
                "inventory": {"items": [{"id": 7, "name": "mace"}]},
                "spells": {"known": True, "spells": []},
            }
            calls: list[tuple[str, dict[str, object]]] = []

            def fake_call(
                name: str, arguments: dict[str, object], **_kwargs: object
            ) -> dict[str, object]:
                calls.append((name, dict(arguments)))
                if name == "autopilot":
                    return {
                        "running": True,
                        "mode": "survive",
                        "activity": "retreating to Brownestone",
                        "policy": {"restBelow": 0.9},
                    }
                if name == "look" and arguments.get("cached") is True:
                    return {
                        "room": {"num": 563, "name": "Brownestone"},
                        "vitals": {"health": {"current": 5, "max": 40}},
                        "minimap": {"text": "@", "walls": [[0, 0]]},
                    }
                raise AssertionError(f"unexpected bulk read during retreat: {name}")

            broker.call_tool = fake_call  # type: ignore[method-assign]

            observation = broker.observe()

            self.assertEqual(
                [
                    ("autopilot", {"agent": "primary", "action": "status"}),
                    (
                        "look",
                        {"agent": "primary", "cached": True, "minimap": True},
                    ),
                ],
                calls,
            )
            self.assertEqual("tactical_cache", observation["freshness"]["mode"])
            self.assertEqual(5, observation["status"]["vitals"]["health"]["current"])
            self.assertEqual(563, observation["status"]["where"]["num"])
            self.assertEqual("mace", observation["inventory"]["items"][0]["name"])
            self.assertNotIn("walls", observation["look"]["minimap"])

    def test_planner_view_hides_controller_owned_agent_argument(self) -> None:
        schema = {
            "type": "object",
            "properties": {"agent": {"type": "string"}, "verb": {"type": "string"}},
            "required": ["agent", "verb"],
        }
        tool = Tool("act", "Perform an action.", schema)

        view = tool.planner_view()

        self.assertEqual({"verb": {"type": "string"}}, view["input_schema"]["properties"])
        self.assertEqual(["verb"], view["input_schema"]["required"])
        self.assertIn("never supply an agent id", view["description"])
        self.assertIn("agent", schema["properties"])
        self.assertEqual(["agent", "verb"], schema["required"])

    def test_percent_encoded_windows_root_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="m59 bot ") as temporary:
            root = Path(temporary)
            broker = BrokerClient(config(root))
            broker._check_root({"root": str(root).replace(" ", "%20")})

    def test_different_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            with self.assertRaises(HarnessIncompatible):
                broker._check_root({"root": str(Path(temporary) / "other")})

    def test_upstream_control_surfaces_are_hidden_but_evidence_tools_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = BrokerClient(config(Path(temporary)))
            names = {
                "pilot",
                "spread",
                "quartermaster",
                "attack_intent",
                "move_intent",
                "context_intent",
                "cancel_action",
                "prey",
                "post_mortem",
                "cancel_movement",
            }
            broker._manifest = {
                name: Tool(name, f"Upstream {name} tool.", {"type": "object", "properties": {}})
                for name in names
            }

            visible = {tool["name"] for tool in broker.planner_tools()}

            controls = {
                "pilot",
                "spread",
                "quartermaster",
                "attack_intent",
                "move_intent",
                "context_intent",
                "cancel_action",
            }
            self.assertTrue(controls.issubset(CONTROLLER_ONLY_TOOLS))
            self.assertTrue(controls.isdisjoint(visible))
            self.assertTrue({"prey", "post_mortem", "cancel_movement"}.issubset(visible))

    def test_managed_launch_uses_native_paths_for_upstream_runtime_stores(self) -> None:
        with tempfile.TemporaryDirectory(prefix="m59 bot ") as temporary:
            root = Path(temporary)
            value = config(root)
            harness_root = root / "harness with spaces"
            value = replace(value, harness=replace(value.harness, root=harness_root))
            broker = BrokerClient(value)

            env = broker._launch_environment()

            substrate = harness_root / "substrate"
            expected = {
                "M59_BAD_EXITS": substrate / "m59-badexits.json",
                "M59_ABILITY_DIR": substrate / "abilities",
                "M59_BANK_DIR": substrate / "banks",
                "M59_DESC_DIR": substrate / "descriptions",
                "M59_HITS_DIR": substrate / "hits",
                "M59_POSTMORTEM_DIR": substrate / "postmortems",
                "M59_TRANSIT_DIR": substrate / "transits",
                "M59_UPTIME_FILE": substrate / "keeper-uptime.jsonl",
                "M59_ACTIVE_FILE": substrate / "keeper-active.json",
            }
            self.assertEqual(
                {name: str(path) for name, path in expected.items()},
                {name: env[name] for name in expected},
            )
            self.assertFalse(any("%20" in env[name] for name in expected))


if __name__ == "__main__":
    unittest.main()
