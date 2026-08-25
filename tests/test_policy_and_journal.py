from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from meridian_bot.config import BotConfig
from meridian_bot.notifications import NotificationDispatcher
from meridian_bot.model import (
    CAMPAIGN_MANAGER_PROMPT_TOKEN_BUDGET,
    GREETER_SYSTEM,
    JOURNAL_ASSESSOR_SYSTEM,
    ModelError,
    PLANNER_SYSTEM,
    RESPONDER_SYSTEM,
    TACTICAL_EXECUTE_PROMPT_TOKEN_BUDGET,
    TACTICAL_PLAN_PROMPT_TOKEN_BUDGET,
    VllmClient,
)
from meridian_bot.obsidian import ObsidianJournal
from meridian_bot.policy import PolicyEngine
from meridian_bot.controller import BotController
from meridian_bot.simulator import SimulatedBroker
from meridian_bot.storage import Storage

from .helpers import config


class PolicyAndJournalTests(unittest.TestCase):
    def test_model_auth_modes_build_provider_specific_headers(self) -> None:
        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"data":[{"id":"test-model"}]}'

        with tempfile.TemporaryDirectory() as temporary:
            base = replace(
                config(Path(temporary)),
                secrets={"M59_LLM_API_KEY": "test-key"},
            )
            expected = {
                "none": {},
                "auto": {"authorization": "Bearer test-key"},
                "bearer": {"authorization": "Bearer test-key"},
                "anthropic": {
                    "x-api-key": "test-key",
                    "anthropic-version": "2023-06-01",
                },
            }
            for mode, auth_headers in expected.items():
                with self.subTest(mode=mode):
                    client = VllmClient(
                        replace(base, model=replace(base.model, auth_mode=mode))
                    )
                    with patch(
                        "meridian_bot.model.urllib.request.urlopen",
                        return_value=Response(),
                    ) as request:
                        client.health()
                    sent = {
                        key.lower(): value
                        for key, value in request.call_args.args[0].header_items()
                    }
                    for key, value in auth_headers.items():
                        self.assertEqual(value, sent[key])
                    if mode == "none":
                        self.assertNotIn("authorization", sent)
                        self.assertNotIn("x-api-key", sent)
                    elif mode in {"auto", "bearer"}:
                        self.assertNotIn("x-api-key", sent)
                    else:
                        self.assertNotIn("authorization", sent)

    def test_explicit_model_auth_requires_a_key_before_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = config(Path(temporary))
            client = VllmClient(
                replace(base, model=replace(base.model, auth_mode="anthropic"))
            )
            with patch("meridian_bot.model.urllib.request.urlopen") as request:
                with self.assertRaisesRegex(ModelError, "requires M59_LLM_API_KEY"):
                    client.health()
            request.assert_not_called()

    def test_unknown_model_auth_mode_is_rejected_by_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bot.toml"
            path.write_text(
                "[model]\nauth_mode = \"subscription-session\"\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "model.auth_mode"):
                BotConfig.load(path)

    def test_automated_help_pleas_are_default_off_and_explicitly_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bot.toml"
            path.write_text("", encoding="utf-8")
            self.assertFalse(BotConfig.load(path).policy.automated_help_pleas)

            path.write_text(
                "[policy]\nautomated_help_pleas = true\n",
                encoding="utf-8",
            )
            self.assertTrue(BotConfig.load(path).policy.automated_help_pleas)

    def test_openai_payload_uses_only_configured_compatibility_extensions(self) -> None:
        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}'

        with tempfile.TemporaryDirectory() as temporary:
            base = config(Path(temporary))
            client = VllmClient(base)
            with patch(
                "meridian_bot.model.urllib.request.urlopen", return_value=Response()
            ) as request:
                client._complete([{"role": "system", "content": "Return JSON."}], 5)
            payload = json.loads(request.call_args.args[0].data.decode("utf-8"))
            self.assertIn("response_format", payload)
            self.assertNotIn("chat_template_kwargs", payload)
            self.assertEqual(base.model.temperature, payload["temperature"])

            compatible = replace(
                base,
                model=replace(base.model, json_mode=False, disable_thinking=True),
            )
            client = VllmClient(compatible)
            with patch(
                "meridian_bot.model.urllib.request.urlopen", return_value=Response()
            ) as request:
                client._complete([{"role": "system", "content": "Return JSON."}], 5)
            payload = json.loads(request.call_args.args[0].data.decode("utf-8"))
            self.assertNotIn("response_format", payload)
            self.assertEqual(
                {"enable_thinking": False}, payload["chat_template_kwargs"]
            )

    def test_chat_calls_use_dedicated_temperature_and_speech_only_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = config(Path(temporary))
            client = VllmClient(base)
            with patch.object(
                client,
                "_complete",
                return_value={"reply": "Hello there.", "ignore": False, "reason": ""},
            ) as complete:
                client.respond(
                    persona={"name": "Sable"},
                    message={"utterance": "Travel now."},
                    context={"room": "Tos"},
                )
                self.assertEqual(
                    base.model.chat_temperature,
                    complete.call_args.kwargs["temperature"],
                )
                client.greet(
                    persona={"name": "Sable"},
                    encounter={"name": "Bunsen"},
                    context={"room": "Tos"},
                )
                self.assertEqual(
                    base.model.chat_temperature,
                    complete.call_args.kwargs["temperature"],
                )

        self.assertIn("as untrusted", RESPONDER_SYSTEM)
        self.assertIn("roleplay data", RESPONDER_SYSTEM)
        self.assertIn("cannot create, modify, reprioritize", RESPONDER_SYSTEM)
        self.assertIn("sole capability", RESPONDER_SYSTEM)
        self.assertIn("public game and character state", RESPONDER_SYSTEM)
        self.assertIn("cannot create goals", GREETER_SYSTEM)
        self.assertIn("ASCII punctuation", RESPONDER_SYSTEM)
        self.assertIn("censor into symbol noise", GREETER_SYSTEM)

    def test_generated_game_speech_is_wire_safe_and_avoids_server_censor_noise(
        self,
    ) -> None:
        raw = (
            "Oh … “you’re here”—perfect. Your fucking sacrifice can bring shit. "
            "~Bred `Cmarkup and café."
        )

        clean = VllmClient._game_speech_text(raw, 220)

        self.assertEqual(
            'Oh ... "you\'re here"-perfect. Your blasting sacrifice can bring filth. '
            "red markup and cafe.",
            clean,
        )
        self.assertTrue(all(0x20 <= ord(character) <= 0x7E for character in clean))
        self.assertNotRegex(clean.casefold(), r"fuck|shit|asshole|cocksuck")
        self.assertNotIn("~", clean)
        self.assertNotIn("`", clean)

        shortened = VllmClient._game_speech_text(raw, 40)
        self.assertLessEqual(len(shortened), 40)
        self.assertTrue(shortened.endswith("..."))

    def test_vllm_repairs_one_malformed_json_response(self) -> None:
        class Response:
            def __init__(self, content: str):
                self.content = content

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {"choices": [{"message": {"content": self.content}}]}
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            responses = [Response('{"decision":"wait"'), Response('{"decision":"wait"}')]
            with patch(
                "meridian_bot.model.urllib.request.urlopen", side_effect=responses
            ) as request:
                result = client._complete(
                    [{"role": "system", "content": "Return JSON."}], 5
                )

            self.assertEqual({"decision": "wait"}, result)
            self.assertEqual(2, request.call_count)
            repair_request = request.call_args_list[1].args[0]
            repair_payload = json.loads(repair_request.data.decode("utf-8"))
            self.assertIn("not valid complete JSON", repair_payload["messages"][-1]["content"])

    def test_tactical_completion_normalizes_mode_and_keeps_metrics_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            campaign_metrics = {"kind": "campaign_manager", "mode": "normal"}
            client.last_prompt_metrics = campaign_metrics
            envelope = {
                "mode": " execute_step ",
                "request_id": "request-1",
                "legal_actions": [],
            }
            response = {
                "request_id": "request-1",
                "action_token": "action-1",
                "arguments": {},
                "rationale": "Use the selected action.",
                "expected_observation": {},
            }
            with patch.object(client, "_complete", return_value=response) as complete:
                result = client.tactical_complete(
                    mode=" execute_step ", envelope=envelope
                )

            self.assertEqual(response, result)
            sent = json.loads(complete.call_args.args[0][1]["content"])
            self.assertEqual("EXECUTE_STEP", sent["mode"])
            self.assertEqual(" execute_step ", envelope["mode"])
            self.assertFalse(complete.call_args.kwargs["allow_json_repair"])
            self.assertGreaterEqual(complete.call_args.kwargs["max_tokens"], 4096)
            self.assertIs(campaign_metrics, client.last_prompt_metrics)
            self.assertEqual(
                "EXECUTE_STEP", client.last_tactical_prompt_metrics["mode"]
            )
            self.assertEqual(
                6000, client.last_tactical_prompt_metrics["token_budget"]
            )
            self.assertIsNone(
                client.last_tactical_prompt_metrics["context_window_limit_tokens"]
            )
            self.assertFalse(
                client.last_tactical_prompt_metrics[
                    "context_window_reserve_enforced"
                ]
            )

    def test_tactical_completion_rejects_conflicting_or_unknown_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            with patch.object(client, "_complete") as complete:
                with self.assertRaisesRegex(ModelError, "mode mismatch"):
                    client.tactical_complete(
                        mode="PLAN_CREATE",
                        envelope={"mode": "EXECUTE_STEP"},
                    )
                with self.assertRaisesRegex(ModelError, "unsupported tactical"):
                    client.tactical_complete(mode="invented", envelope={})
            complete.assert_not_called()

    def test_tactical_completion_disables_unbudgeted_json_transcript_repair(self) -> None:
        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {"choices": [{"message": {"content": '{"request_id":'}}]}
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            with patch(
                "meridian_bot.model.urllib.request.urlopen", return_value=Response()
            ) as request:
                with self.assertRaisesRegex(ModelError, "repair disabled"):
                    client.tactical_complete(
                        mode="EXECUTE_STEP",
                        envelope={
                            "mode": "EXECUTE_STEP",
                            "request_id": "request-1",
                            "legal_actions": [],
                        },
                    )
            self.assertEqual(1, request.call_count)

    def test_tactical_plan_incident_shape_fits_after_provenance_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            goal_contract = {
                "id": "goal-bank-items",
                "version": 7,
                "title": "Bank carried items",
                "objective": "Deposit the selected carried items in a verified bank.",
                "success_criteria": [
                    {"id": "banked", "kind": "inventory_absent", "item": "mace"}
                ],
                "constraints": {"preserve": ["food", "reagents"]},
            }
            phase_contract = {
                "id": "phase-bank-items",
                "kind": "general",
                "objective": "Reach a bank and deposit the selected items.",
                "success_criteria": deepcopy(goal_contract["success_criteria"]),
                "abandon_predicates": [],
                "budget": {"max_actions": 24, "max_minutes": 45},
                "context": {"research_exhaustion_support": True},
            }
            tools = [
                {
                    "name": f"tool_{index:02d}",
                    "description": (
                        f"Exact planning semantics for tool {index:02d}. "
                        + (chr(65 + index % 26) * 420)
                    ),
                }
                for index in range(17)
            ]
            candidates = []
            for index in range(12):
                source_ref = f"safe-room-{index}-" + ("P" * 1180)
                candidates.append(
                    {
                        "candidate_id": f"safe:{100 + index}",
                        "room_id": 100 + index,
                        "name": f"Safe room {index}",
                        "flags": ["ROOM_NO_COMBAT"],
                        "distance": index,
                        "basis": "source_connection_graph",
                        "source_ref": source_ref,
                        "evidence": {
                            "source_tier": "source-derived",
                            "source_ref": source_ref,
                            "corpus_version": "fixture-v1",
                        },
                    }
                )
            ranked_facts = [
                {
                    "rank": index,
                    "fact": f"grounded-fact-{index}-" + ("F" * 980),
                    "source_ref": f"grounding-source-{index}",
                }
                for index in range(19)
            ]
            envelope = {
                "protocol_version": "tactical/v2",
                "mode": "PLAN_CREATE",
                "request_id": "incident-plan-request",
                "state_token": "state-v2-incident",
                "goal_contract": goal_contract,
                "phase_contract": phase_contract,
                "strategy_options": [],
                "available_tools": tools,
                "plan_constraints": {
                    "max_model_steps": 9,
                    "allowed_tools": [tool["name"] for tool in tools],
                    "must_cover": ["banked"],
                    "required_rule_codes": ["BANK_LOCATION_PREREQUISITE"],
                    "safe_ending_candidates": candidates,
                },
                "relevant_facts": {
                    "live_state": {"room_id": 52, "vigor": 100},
                    "grounding": {"ranked_facts": ranked_facts},
                    "source_ref": "aggregate-grounding-provenance",
                },
                "relevant_failures": [],
                "planning_persona": {"name": "MANIAC"},
                "rule_cards": [],
            }
            original = deepcopy(envelope)

            with patch.object(
                client, "_complete", return_value={"accepted": True}
            ) as complete:
                result = client.tactical_complete(
                    mode="PLAN_CREATE", envelope=envelope
                )

            self.assertEqual({"accepted": True}, result)
            sent = json.loads(complete.call_args.args[0][1]["content"])
            metrics = client.last_tactical_prompt_metrics
            self.assertIsNotNone(metrics)
            assert metrics is not None
            self.assertGreater(
                metrics["original_estimated_tokens"],
                TACTICAL_PLAN_PROMPT_TOKEN_BUDGET,
            )
            self.assertLessEqual(
                metrics["estimated_tokens"], TACTICAL_PLAN_PROMPT_TOKEN_BUDGET
            )
            self.assertTrue(metrics["supporting_context_compacted"])
            self.assertEqual("routing-and-provenance", metrics["compaction_profile"])
            self.assertEqual(goal_contract, sent["goal_contract"])
            self.assertEqual(phase_contract, sent["phase_contract"])
            self.assertEqual(tools, sent["available_tools"])
            self.assertEqual(
                [
                    (candidate["candidate_id"], candidate["room_id"])
                    for candidate in candidates
                ],
                [
                    (candidate["candidate_id"], candidate["room_id"])
                    for candidate in sent["plan_constraints"][
                        "safe_ending_candidates"
                    ]
                ],
            )
            serialized = complete.call_args.args[0][1]["content"]
            self.assertNotIn("source_ref", serialized)
            self.assertNotIn('"evidence"', serialized)
            self.assertEqual(19, len(sent["relevant_facts"]["grounding"]["ranked_facts"]))
            self.assertEqual(original, envelope)

    def test_tactical_plan_compacts_ranked_supporting_lists_but_keeps_core_exact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            goal_contract = {
                "id": "goal-ranked-facts",
                "objective": "Use the highest-ranked grounded option.",
                "success_criteria": [{"id": "done", "kind": "location_reached", "room_id": 54}],
                "constraints": {},
            }
            phase_contract = {
                "id": "phase-ranked-facts",
                "kind": "general",
                "objective": "Choose from ranked supporting facts.",
                "success_criteria": deepcopy(goal_contract["success_criteria"]),
                "abandon_predicates": [],
                "budget": {"max_actions": 12, "max_minutes": 30},
            }
            tools = [
                {
                    "name": "travel",
                    "description": "Travel to an exact grounded numeric room destination.",
                }
            ]
            candidates = [
                {
                    "candidate_id": "safe:100",
                    "room_id": 100,
                    "name": "Safe staging",
                    "flags": ["ROOM_NO_COMBAT"],
                }
            ]
            ranked = [
                {"rank": index, "fact": f"ranked-{index}-" + ("R" * 900)}
                for index in range(70)
            ]
            live_inventory = [
                {"id": index, "name": f"carried item {index}"}
                for index in range(18)
            ]
            live_hostiles = [
                {"id": index, "name": f"hostile {index}", "level": index + 1}
                for index in range(15)
            ]
            envelope = {
                "protocol_version": "tactical/v2",
                "mode": "PLAN_CREATE",
                "request_id": "ranked-plan-request",
                "state_token": "state-v2-ranked",
                "goal_contract": goal_contract,
                "phase_contract": phase_contract,
                "available_tools": tools,
                "plan_constraints": {
                    "max_model_steps": 9,
                    "allowed_tools": ["travel"],
                    "must_cover": ["done"],
                    "required_rule_codes": [],
                    "safe_ending_candidates": candidates,
                },
                "relevant_facts": {
                    "live_state": {"inventory": live_inventory},
                    "live_overlevel_hostiles": live_hostiles,
                    "ranked_options": ranked,
                },
                "relevant_failures": [],
                "planning_persona": {"name": "MANIAC"},
                "rule_cards": [],
            }

            with patch.object(
                client, "_complete", return_value={"accepted": True}
            ) as complete:
                client.tactical_complete(mode="PLAN_CREATE", envelope=envelope)

            sent = json.loads(complete.call_args.args[0][1]["content"])
            metrics = client.last_tactical_prompt_metrics
            self.assertIsNotNone(metrics)
            assert metrics is not None
            self.assertLessEqual(
                metrics["estimated_tokens"], TACTICAL_PLAN_PROMPT_TOKEN_BUDGET
            )
            self.assertEqual("ranked-context-12", metrics["compaction_profile"])
            self.assertEqual(goal_contract, sent["goal_contract"])
            self.assertEqual(phase_contract, sent["phase_contract"])
            self.assertEqual(tools, sent["available_tools"])
            self.assertEqual(candidates, sent["plan_constraints"]["safe_ending_candidates"])
            retained = sent["relevant_facts"]["ranked_options"]
            self.assertEqual(ranked[:12], retained[:12])
            self.assertEqual({"omitted_ranked_items": 58}, retained[12])
            self.assertEqual(
                live_inventory, sent["relevant_facts"]["live_state"]["inventory"]
            )
            self.assertEqual(
                live_hostiles, sent["relevant_facts"]["live_overlevel_hostiles"]
            )

    def test_tactical_plan_rejects_irreducible_oversized_goal_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            oversized_goal = {
                "id": "goal-irreducible",
                "objective": "G" * 60_000,
                "success_criteria": [],
                "constraints": {},
            }
            envelope = {
                "protocol_version": "tactical/v2",
                "mode": "PLAN_CREATE",
                "request_id": "irreducible-plan-request",
                "state_token": "state-v2-irreducible",
                "goal_contract": oversized_goal,
                "phase_contract": None,
                "available_tools": [],
                "plan_constraints": {
                    "max_model_steps": 9,
                    "allowed_tools": [],
                    "must_cover": [],
                    "required_rule_codes": [],
                    "safe_ending_candidates": [
                        {"candidate_id": "safe:100", "room_id": 100}
                    ],
                },
                "relevant_facts": {},
                "relevant_failures": [],
                "rule_cards": [],
            }

            with patch.object(client, "_complete") as complete:
                with self.assertRaisesRegex(ModelError, "required context exceeds"):
                    client.tactical_complete(mode="PLAN_CREATE", envelope=envelope)

            complete.assert_not_called()
            self.assertEqual(60_000, len(envelope["goal_contract"]["objective"]))
            metrics = client.last_tactical_prompt_metrics
            self.assertIsNotNone(metrics)
            assert metrics is not None
            self.assertTrue(metrics["over_budget"])
            self.assertGreater(
                metrics["estimated_tokens"], TACTICAL_PLAN_PROMPT_TOKEN_BUDGET
            )

    def test_tactical_action_compaction_preserves_nested_legal_action_schema(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            active_step = {
                "id": "drop-selected-item",
                "outcome": "Drop the selected inventory item.",
                "tool": "act",
                "verification": "The selected item is absent from inventory.",
            }
            legal_actions = [
                {
                    "action_token": "action-v2-drop-selected-item",
                    "step_id": "drop-selected-item",
                    "tool": "act",
                    "locked_arguments": {"verb": "drop"},
                    "free_argument_schema": {
                        "type": "object",
                        "properties": {
                            "target": {
                                "type": ["integer", "string"],
                                "description": "Exact inventory object id or visible name.",
                                "examples": [17, "rusty sword"],
                            }
                        },
                        "required": ["target"],
                        "additionalProperties": False,
                        "examples": [{"target": 17}, {"target": "rusty sword"}],
                    },
                    "expected_observation": {"inventory_absent": "rusty sword"},
                }
            ]
            recent_history = [
                f"historical-observation-{index}-" + ("H" * 700)
                for index in range(64)
            ]
            envelope = {
                "protocol_version": "tactical/v2",
                "mode": "EXECUTE_STEP",
                "request_id": "execute-schema-request",
                "state_token": "state-v2-execute-schema",
                "active_step": active_step,
                "legal_actions": legal_actions,
                "relevant_live_state": {"room_id": 52},
                "recent_history": recent_history,
                "rule_cards": [],
            }

            with patch.object(
                client, "_complete", return_value={"accepted": True}
            ) as complete:
                client.tactical_complete(mode="EXECUTE_STEP", envelope=envelope)

            sent = json.loads(complete.call_args.args[0][1]["content"])
            metrics = client.last_tactical_prompt_metrics
            self.assertIsNotNone(metrics)
            assert metrics is not None
            self.assertGreater(
                metrics["original_estimated_tokens"],
                TACTICAL_EXECUTE_PROMPT_TOKEN_BUDGET,
            )
            self.assertLessEqual(
                metrics["estimated_tokens"], TACTICAL_EXECUTE_PROMPT_TOKEN_BUDGET
            )
            self.assertTrue(metrics["optional_context_compacted"])
            self.assertEqual(active_step, sent["active_step"])
            self.assertEqual(legal_actions, sent["legal_actions"])
            self.assertEqual(recent_history[-12:], sent["recent_history"])

    def test_character_onboarding_reserves_room_for_reasoning_then_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            with patch.object(
                client,
                "_complete",
                return_value={
                    "stats": "caster",
                    "loadout": "selfSufficient",
                    "rationale": "Matches the persona.",
                },
            ) as complete:
                result = client.plan_character(
                    persona={"name": "Sable"},
                    current_character={"name": "User123"},
                )

            self.assertEqual("caster", result["stats"])
            self.assertGreaterEqual(complete.call_args.kwargs["max_tokens"], 4096)

    def test_vllm_retries_reasoning_only_length_with_larger_budget(self) -> None:
        class Response:
            def __init__(self, content: str | None, reasoning: str | None = None):
                self.content = content
                self.reasoning = reasoning

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "choices": [
                            {
                                "finish_reason": "length" if self.content is None else "stop",
                                "message": {
                                    "content": self.content,
                                    "reasoning": self.reasoning,
                                },
                            }
                        ]
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            with patch(
                "meridian_bot.model.urllib.request.urlopen",
                side_effect=[
                    Response(None, "Still thinking..."),
                    Response('{"decision":"wait"}'),
                ],
            ) as request:
                result = client._complete(
                    [{"role": "system", "content": "Return JSON."}],
                    5,
                    max_tokens=300,
                )

            self.assertEqual({"decision": "wait"}, result)
            self.assertEqual(2, request.call_count)
            retry_payload = json.loads(
                request.call_args_list[1].args[0].data.decode("utf-8")
            )
            self.assertGreaterEqual(retry_payload["max_tokens"], 4096)
            self.assertIn(
                "exhausted its completion budget",
                retry_payload["messages"][-1]["content"],
            )

    def test_vllm_reports_reasoning_only_response_after_bounded_retry(self) -> None:
        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "choices": [
                            {
                                "finish_reason": "length",
                                "message": {
                                    "content": None,
                                    "reasoning": "Still thinking...",
                                },
                            }
                        ]
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            with patch(
                "meridian_bot.model.urllib.request.urlopen",
                side_effect=[Response(), Response()],
            ) as request:
                with self.assertRaisesRegex(
                    ModelError, "after retrying with 4096 completion tokens"
                ):
                    client._complete(
                        [{"role": "system", "content": "Return JSON."}], 5
                    )
            self.assertEqual(2, request.call_count)

    def test_goal_drafter_receives_operator_revision_and_current_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            current = {
                "title": "Reach Tos Inn",
                "objective": "Travel to Tos Inn.",
                "success_criteria": [
                    {
                        "id": "at_inn",
                        "kind": "location_reached",
                        "room_id": 52,
                    }
                ],
                "constraints": {},
                "priority": 50,
                "activation": "queue",
            }
            revised = {**current, "priority": 90}
            with patch.object(client, "_complete", return_value=revised) as complete:
                result = client.draft_goal(
                    prompt="Make this priority 90.",
                    current_goal=current,
                    validation_feedback=[
                        {"code": "EXAMPLE", "message": "Repair this."}
                    ],
                    verified_character_state={"character": "Sable"},
                    grounding_hints=[
                        {
                            "kind": "location",
                            "canonical_name": "Tos Inn",
                            "room_id": 52,
                        }
                    ],
                )

            self.assertEqual(revised, result)
            messages = complete.call_args.args[0]
            context = json.loads(messages[1]["content"])
            self.assertEqual("Make this priority 90.", context["operator_prompt"])
            self.assertEqual(current, context["current_goal"])
            self.assertEqual("EXAMPLE", context["validation_feedback"][0]["code"])
            self.assertIn("operator_confirmed", messages[0]["content"])
            self.assertIn("event_kind raza.left", messages[0]["content"])
            self.assertGreaterEqual(complete.call_args.kwargs["max_tokens"], 4096)

    def test_campaign_manager_and_planner_reserve_reasoning_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            with patch.object(
                client, "_complete", return_value={"decision": "start_phase"}
            ) as complete:
                client.manage_campaign(
                    goal={},
                    observation={},
                    campaign_context={
                        "phase_capabilities": {
                            "research_progression": ["hunting_grounds"]
                        }
                    },
                    grounded_knowledge=None,
                    learned_failures=None,
                    financial_context=None,
                )
            self.assertGreaterEqual(complete.call_args.kwargs["max_tokens"], 4096)
            messages = complete.call_args.args[0]
            sent = json.loads(messages[1]["content"])
            self.assertEqual(
                ["hunting_grounds"],
                sent["campaign"]["phase_capabilities"]["research_progression"],
            )
            self.assertIn("fact namespaces, not callable tools", messages[0]["content"])

    def test_campaign_manager_enforces_prompt_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            duplicate = {
                "room": 583,
                "target": "slime",
                "reason": "quarantined_farm_phase",
                "details": ["repeated safe-spot failure " + ("x" * 2000)],
            }
            campaign = {
                "run": {"id": "run-1", "status": "active"},
                "active_phase": None,
                "tactic_ledger": {
                    "unique_rejected_candidates": [duplicate for _ in range(2000)]
                },
                "recent_phase_summaries": [
                    {"id": f"phase-{index}", "objective": "y" * 4000}
                    for index in range(100)
                ],
                "research_retry": {"allowed": False},
                "verified_no_progress_tactics": [
                    {
                        "tool": "travel",
                        "room": 104,
                        "arguments": {"to": 106},
                        "reason": "every square for that exit refused",
                    }
                ],
            }
            with patch.object(
                client, "_complete", return_value={"decision": "start_phase"}
            ) as complete:
                client.manage_campaign(
                    goal={"title": "Reach 100 HP"},
                    observation={"status": {"vitals": {"health": {"max": 34}}}},
                    campaign_context=campaign,
                    grounded_knowledge=None,
                    learned_failures={
                        "room_evidence": [
                            {
                                "classification": "productive_if_level_eligible",
                                "room": 544,
                                "target": "groundworm larva",
                            }
                        ]
                    },
                    financial_context=None,
                )

            self.assertIsNotNone(client.last_prompt_metrics)
            self.assertLessEqual(
                client.last_prompt_metrics["estimated_tokens"],
                CAMPAIGN_MANAGER_PROMPT_TOKEN_BUDGET,
            )
            self.assertTrue(client.last_prompt_metrics["compacted"])
            sent = json.loads(complete.call_args.args[0][1]["content"])
            self.assertEqual(False, sent["campaign"]["research_retry"]["allowed"])
            self.assertEqual(
                106,
                sent["campaign"]["verified_no_progress_tactics"][0]["arguments"][
                    "to"
                ],
            )
            self.assertEqual(
                544, sent["learned_failures"]["room_evidence"][0]["room"]
            )

    def test_campaign_manager_timeout_retries_with_minimal_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            with patch.object(
                client,
                "_complete",
                side_effect=[
                    ModelError("model request failed: timed out"),
                    {"decision": "start_phase"},
                ],
            ) as complete:
                result = client.manage_campaign(
                    goal={"title": "Reach 100 HP"},
                    observation={"status": {"vitals": {"health": {"max": 34}}}},
                    campaign_context={
                        "run": {"id": "run-1", "status": "active"},
                        "research_retry": {"allowed": False},
                    },
                    grounded_knowledge={"rules": ["grounded"]},
                    learned_failures=None,
                    financial_context=None,
                )

            self.assertEqual("start_phase", result["decision"])
            self.assertEqual(2, complete.call_count)
            recovery = json.loads(complete.call_args_list[1].args[0][1]["content"])
            self.assertTrue(recovery["timeout_recovery"]["prior_request_timed_out"])
            self.assertEqual("timeout_recovery", client.last_prompt_metrics["mode"])

    def test_campaign_manager_and_planner_receive_full_planning_persona(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            persona = {
                "version": 4,
                "name": "Sable",
                "character_voice": "A guarded pilgrim who prefers quiet sanctuaries.",
                "traits": ["wary", "curious"],
                "speech_style": ["concise"],
                "values": ["self-reliance"],
                "taboos": ["boasting"],
                "relationship_defaults": "Polite but slow to trust.",
                "max_reply_characters": 360,
            }

            with patch.object(
                client, "_complete", return_value={"decision": "start_phase"}
            ) as complete:
                client.manage_campaign(
                    goal={},
                    observation={},
                    campaign_context={},
                    grounded_knowledge=None,
                    learned_failures=None,
                    financial_context=None,
                    persona=persona,
                )
            manager_context = json.loads(complete.call_args.args[0][1]["content"])
            self.assertEqual(persona, manager_context["planning_persona"])

            with patch.object(
                client, "_complete", return_value={"decision": "wait"}
            ) as complete:
                client.plan(
                    goal={},
                    observation={},
                    tools=[],
                    persona=persona,
                    recent_events=[],
                    pending_proposals=[],
                    planner_feedback=None,
                    policy_summary={},
                )
            planner_context = json.loads(complete.call_args.args[0][1]["content"])
            self.assertEqual(persona, planner_context["planning_persona"])
            self.assertIn("safe_ending.rationale", PLANNER_SYSTEM)

            with patch.object(
                client, "_complete", return_value={"decision": "wait"}
            ) as complete:
                client.plan(
                    goal={},
                    observation={},
                    tools=[],
                    persona={},
                    recent_events=[],
                    pending_proposals=[],
                    planner_feedback=None,
                    policy_summary={},
                )
            self.assertGreaterEqual(complete.call_args.kwargs["max_tokens"], 4096)

    def test_planner_receives_explicit_financial_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = VllmClient(config(Path(temporary)))
            captured: list[dict[str, object]] = []

            def complete(
                messages: list[dict[str, str]],
                _: int,
                **__: object,
            ) -> dict[str, object]:
                captured.append(json.loads(messages[-1]["content"]))
                return {"decision": "wait"}

            client._complete = complete  # type: ignore[method-assign]
            finances = {
                "carried_shillings": 278,
                "known_inventory_item_value": 200,
                "known_total_carried_value": 478,
                "valuation_complete": False,
            }
            client.plan(
                goal={},
                observation={},
                tools=[],
                persona={},
                recent_events=[],
                pending_proposals=[],
                planner_feedback=None,
                policy_summary={},
                financial_context=finances,
            )

            self.assertEqual(finances, captured[0]["financial_context"])

    def test_journal_assessor_is_prompted_to_write_only_the_new_milestone(self) -> None:
        self.assertIn("strict milestone filter", JOURNAL_ASSESSOR_SYSTEM)
        self.assertIn("They are the only new", JOURNAL_ASSESSOR_SYSTEM)
        self.assertIn("developments to assess", JOURNAL_ASSESSOR_SYSTEM)
        self.assertIn("Never recap older goals", JOURNAL_ASSESSOR_SYSTEM)

    def test_journal_compaction_preserves_combat_and_collapses_routine_noise(self) -> None:
        events = [
            {"id": f"routine-{index}", "kind": "action.succeeded", "summary": "look succeeded", "occurred_at": f"2026-08-03T00:00:{index:02d}Z"}
            for index in range(6)
        ]
        events.append(
            {"id": "death", "kind": "character.died", "summary": "TestHero died", "occurred_at": "2026-08-03T00:01:00Z"}
        )
        compacted = VllmClient._compact_journal_events(events)
        routine = next(item for item in compacted if item["kind"] == "action.succeeded")
        self.assertEqual(6, routine["aggregated_count"])
        self.assertEqual("death", next(item for item in compacted if item["kind"] == "character.died")["id"])

    def test_live_journal_context_includes_combat_assessment_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                broker = SimulatedBroker()
                controller.broker = broker
                controller.last_observation = broker.observe()
                context = controller._journal_assessment_context()
                self.assertEqual("unknown", context["combat_readiness"]["equipment_state"])
                self.assertIn("combat_history", context)
                self.assertIn("abilities", context)
                self.assertNotIn("surrounding_events", context)
            finally:
                controller.storage.close()

    def test_drop_is_autonomous_caution_and_leave_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            policy = PolicyEngine(cfg.policy)
            observation = {"inventory": {"items": [{"id": 9, "name": "Ancient artifact"}]}}
            drop = policy.evaluate("act", {"agent": "primary", "verb": "drop", "target": 9}, observation, {"id": "goal"}, known_tools={"act", "leave"})
            leave = policy.evaluate("leave", {"agent": "primary"}, observation, {"id": "goal"}, known_tools={"act", "leave"})
            self.assertEqual("allow_with_caution", drop.decision)
            self.assertEqual("item_drop", drop.action_class)
            self.assertTrue(drop.notify)
            self.assertEqual("deny", leave.decision)

    def test_bank_transfer_is_logged_as_autonomous_property_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            policy = PolicyEngine(cfg.policy)
            decision = policy.evaluate(
                "bank",
                {"agent": "primary", "action": "deposit", "amount": 41},
                {"inventory": {"items": [{"id": 9, "name": "shilling", "amount": 41}]}},
                {"id": "goal"},
                known_tools={"bank"},
            )
            self.assertEqual("allow_with_caution", decision.decision)
            self.assertEqual("protected_property_transaction", decision.action_class)
            self.assertEqual("bank_deposit", decision.facts["transaction"])
            self.assertEqual(41, decision.facts["amount"])
            self.assertTrue(decision.notify)

    def test_obsidian_retry_is_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary), obsidian=True)
            event = {
                "id": "event-1",
                "occurred_at": "2026-08-04T01:30:00Z",
                "kind": "consequence.executed",
                "severity": "notice",
                "summary": "Dropped a protected item",
                "goal_id": "goal-1",
            }
            assessment = {
                "headline": "TestHero deliberately shed valuable weight",
                "assessment": "TestHero dropped a protected item while carrying out their active plan.",
                "significance": "The inventory loss may change what equipment is available for the next fight.",
                "next_watch": "Confirm whether the item was intentionally abandoned or recovered.",
                "severity": "warning",
            }
            journal = ObsidianJournal(cfg.notifications, "Etc/GMT+7")
            journal.deliver_assessment(assessment, [event], model_name="test-model")
            journal.deliver_assessment(assessment, [event], model_name="test-model")
            shard = cfg.notifications.obsidian_vault_path / "01 Projects/Meridian 59 Bot/Journal/2026-08-03.md"
            content = shard.read_text(encoding="utf-8")
            self.assertEqual(1, content.count("m59-event:event-1"))
            self.assertIn("2026-08-03 6:30:00 PM -07", content)
            self.assertNotIn("2026-08-04T01:30:00Z", content)
            self.assertIn("TestHero deliberately shed valuable weight", content)
            self.assertNotIn("consequence.executed", content)

    def test_executive_index_shows_current_campaign_and_clean_journal_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary), obsidian=True)
            event = {
                "id": "hp-22",
                "occurred_at": "2026-08-04T01:30:00Z",
                "kind": "progress.hp_gained",
                "severity": "notice",
                "summary": "TestHero's maximum HP increased from 21 to 22",
                "goal_id": "goal-1",
                "data": {"before": 21, "after": 22},
            }
            assessment = {
                "headline": "TestHero reached 22 max HP",
                "assessment": "The farming plan produced one verified max-HP gain.",
                "significance": "This is durable character progression.",
                "next_watch": "Watch for the next safe prey tier.",
                "severity": "notice",
            }
            context = {
                "character": "TestHero",
                "location": "Tos Inn",
                "vitals": {"health": {"current": 22, "max": 22}},
                "risk": "safe",
                "controller": {"dependencies": {"broker": "healthy", "model": "healthy"}},
                "active_goal": {
                    "title": "Raise max HP to 25",
                    "status": "active",
                    "completion": {"percent_estimate": 25, "summary": "22 of 25 max HP"},
                },
            }

            journal = ObsidianJournal(cfg.notifications, "Etc/GMT+7")
            journal.deliver_assessment(
                assessment, [event], model_name="test-model", context=context
            )

            index = cfg.notifications.obsidian_vault_path / "01 Projects/Meridian 59 Bot/Meridian 59 Bot.md"
            content = index.read_text(encoding="utf-8")
            self.assertIn("## Current campaign", content)
            self.assertIn("**Health:** 22/22 HP", content)
            self.assertIn("Raise max HP to 25 (active) — 25%", content)
            self.assertIn("## Latest milestone", content)
            self.assertIn("- [[Journal/2026-08-03|2026-08-03]]\n", content)

            index.write_text("broken index", encoding="utf-8")
            journal.refresh_executive_summary(context)
            repaired = index.read_text(encoding="utf-8")
            self.assertIn("TestHero reached 22 max HP", repaired)
            self.assertIn("**Health:** 22/22 HP", repaired)

    def test_obsidian_suppresses_non_milestones_before_calling_the_llm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary), obsidian=True)
            storage = Storage(cfg.database_path)
            event = storage.emit_event(
                "dependency.controller.unhealthy",
                "controller: unknown argument",
                severity="warning",
                interesting=True,
            )
            seen: list[list[str]] = []

            def assessor(*, events: list[dict[str, object]], context: dict[str, object]) -> dict[str, object]:
                seen.append([str(item["id"]) for item in events])
                return {
                    "significant": False,
                    "headline": "Routine input validation",
                    "assessment": "This was an isolated malformed request with no effect on TestHero.",
                    "significance": "",
                    "next_watch": "",
                    "severity": "notice",
                }

            dispatcher = NotificationDispatcher(cfg, storage, assessor=assessor)
            result = dispatcher.dispatch_pending()
            self.assertEqual([], seen)
            self.assertEqual(1, result["suppressed"])
            self.assertEqual("suppressed", storage.delivery_status("obsidian", event["id"]))
            journal = cfg.notifications.obsidian_vault_path / "01 Projects/Meridian 59 Bot/Journal"
            self.assertFalse(journal.exists())
            storage.close()

    def test_escalated_planner_stall_is_assessed_once_and_shown_in_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary), obsidian=True)
            storage = Storage(cfg.database_path)
            event = storage.emit_event(
                "planner.stalled",
                "Planner repeated the same blocked travel action three times",
                severity="warning",
                interesting=True,
                goal_id="goal-1",
                data={
                    "same_blocker_count": 3,
                    "blocked_for_seconds": 45,
                    "blocker_kinds": ["bank_before_hazard_travel"],
                },
            )
            context = {
                "character": "TestHero",
                "location": "Frisconar's Mysticals",
                "controller": {"dependencies": {"broker": "healthy"}},
                "liveness": {
                    "state": "stalled",
                    "safety_suppression": {
                        "same_blocker_count": 3,
                        "first_blocked_at": "2026-08-04T10:00:00Z",
                        "blocker_kinds": ["bank_before_hazard_travel"],
                    },
                },
            }

            def assessor(**_: object) -> dict[str, object]:
                return {
                    "significant": True,
                    "headline": "TestHero's current plan stalled",
                    "assessment": "The same bank-travel blocker repeated three times.",
                    "significance": "The active phase is not advancing.",
                    "next_watch": "Watch for a corrected action or paused goal.",
                    "severity": "warning",
                }

            dispatcher = NotificationDispatcher(
                cfg, storage, assessor=assessor, context_provider=lambda: context
            )
            result = dispatcher.dispatch_pending()

            self.assertEqual(1, result["sent"])
            self.assertEqual("delivered", storage.delivery_status("obsidian", event["id"]))
            index = (
                cfg.notifications.obsidian_vault_path
                / "01 Projects/Meridian 59 Bot/Meridian 59 Bot.md"
            )
            self.assertIn("**Liveness:** stalled", index.read_text(encoding="utf-8"))
            storage.close()

    def test_obsidian_deduplicates_goal_milestones_and_assesses_the_richest_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary), obsidian=True)
            storage = Storage(cfg.database_path)
            sparse = storage.emit_event(
                "goal.active", "Goal active", interesting=True, goal_id="goal-1"
            )
            rich = storage.emit_event(
                "goal.active",
                "Goal active with evaluated state",
                interesting=True,
                goal_id="goal-1",
                data={"completion": {"percent_estimate": 10}, "title": "Reach 25 HP"},
            )
            seen: list[list[str]] = []

            def assessor(*, events: list[dict[str, object]], context: dict[str, object]) -> dict[str, object]:
                seen.append([str(item["id"]) for item in events])
                return {
                    "significant": True,
                    "headline": "New goal activated",
                    "assessment": "TestHero began working toward 25 max HP.",
                    "significance": "This defines the current campaign phase.",
                    "next_watch": "Watch for the first HP gain.",
                    "severity": "notice",
                }

            dispatcher = NotificationDispatcher(cfg, storage, assessor=assessor)
            result = dispatcher.dispatch_pending()
            self.assertEqual([[rich["id"]]], seen)
            self.assertEqual("suppressed", storage.delivery_status("obsidian", sparse["id"]))
            self.assertEqual("delivered", storage.delivery_status("obsidian", rich["id"]))
            self.assertEqual(1, result["sent"])
            self.assertEqual(1, result["suppressed"])
            storage.close()

    def test_allowlisted_milestone_cannot_be_vetoed_by_the_assessor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary), obsidian=True)
            storage = Storage(cfg.database_path)
            event = storage.emit_event(
                "goal.blocked",
                "Goal blocked",
                interesting=True,
                goal_id="goal-1",
                occurred_at="2026-08-04T01:30:00Z",
            )

            def assessor(**_: object) -> dict[str, object]:
                return {
                    "significant": False,
                    "headline": "Goal blocked",
                    "assessment": "The current goal cannot progress until its prerequisite changes.",
                    "significance": "Campaign progress has stopped.",
                    "next_watch": "Watch for a revised supporting goal.",
                    "severity": "warning",
                }

            dispatcher = NotificationDispatcher(cfg, storage, assessor=assessor)
            result = dispatcher.dispatch_pending()
            self.assertEqual(1, result["sent"])
            self.assertEqual("delivered", storage.delivery_status("obsidian", event["id"]))
            shard = cfg.notifications.obsidian_vault_path / "01 Projects/Meridian 59 Bot/Journal/2026-08-04.md"
            self.assertIn("Goal blocked", shard.read_text(encoding="utf-8"))
            storage.close()


if __name__ == "__main__":
    unittest.main()
