from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from meridian_bot.config import BotConfig
from meridian_bot.notifications import NotificationDispatcher
from meridian_bot.model import JOURNAL_ASSESSOR_SYSTEM, ModelError, VllmClient
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
            self.assertGreaterEqual(complete.call_args.kwargs["max_tokens"], 1200)

    def test_vllm_reports_reasoning_only_response_clearly(self) -> None:
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
                "meridian_bot.model.urllib.request.urlopen", return_value=Response()
            ):
                with self.assertRaisesRegex(
                    ModelError, "reasoning but no final JSON"
                ):
                    client._complete(
                        [{"role": "system", "content": "Return JSON."}], 5
                    )

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
