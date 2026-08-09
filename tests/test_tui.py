from __future__ import annotations

import contextlib
import io
import unittest

from meridian_bot.tui import (
    prompt_goal_command,
    prompt_new_goal,
    render_character_status,
    render_dashboard,
    run_tui,
)


class FakeApi:
    def __init__(self) -> None:
        self.draft_requests: list[dict[str, object]] = []
        self.submissions: list[dict[str, object]] = []
        self.character_status_requests = 0
        self.drafts: list[dict[str, object]] = [
            {
                "title": "Reach the bank",
                "objective": "Reach the Tos bank safely.",
                "success_criteria": [
                    {
                        "id": "at_bank",
                        "kind": "location_reached",
                        "location": "First Royal Bank of Tos",
                        "room_id": 54,
                    }
                ],
                "constraints": {"avoid_death": True},
                "priority": 60,
                "activation": "queue",
            }
        ]

    def status(self) -> dict[str, object]:
        return {
            "controller": {"state": "running", "control_owner": "keeper"},
            "game": {
                "connection": "joined",
                "character_name": "Sable",
                "location": "Tos",
                "vitals": {
                    "health": {"current": 40, "max": 50},
                    "mana": {"current": 20, "max": 30},
                    "vigor": {"current": 15, "max": 20},
                },
                "risk": "low",
                "carried_currency": 12,
                "observation_age_seconds": 0.4,
            },
            "onboarding": {"status": "ready"},
            "goal": {
                "id": "goal-1",
                "title": "Reach the bank",
                "objective": "Reach the Tos bank.",
                "status": "active",
                "version": 2,
                "priority": 50,
                "progress_percent": 25,
                "criteria": [
                    {"kind": "location_reached", "met": False, "detail": "Not there yet."}
                ],
            },
            "campaign": {
                "development": {
                    "skills": [{"name": "Dodge", "ability": 12}],
                    "spells": [{"name": "Blink", "ability": 8}],
                },
                "readiness": {
                    "equipment_state": "known",
                    "healing_supply_count": 2,
                    "recent_combat_deaths": 0,
                },
            },
        }

    def goals(self) -> list[dict[str, object]]:
        return [
            {
                "id": "goal-1",
                "title": "Reach the bank",
                "objective": "Reach the Tos bank.",
                "status": "active",
                "version": 2,
                "priority": 50,
                "success_criteria": [],
            },
            {
                "id": "goal-2",
                "title": "Buy bread",
                "status": "queued",
                "version": 1,
                "priority": 40,
                "success_criteria": [],
            },
        ]

    def events(self) -> list[dict[str, object]]:
        return [
            {
                "occurred_at": "2026-08-08T12:34:56Z",
                "severity": "notice",
                "summary": "Controller started",
            }
        ]

    def character_status(self) -> dict[str, object]:
        self.character_status_requests += 1
        return {
            "game": {
                "connection": "joined",
                "character_name": "Sable",
                "location": "Tos Inn",
                "room_id": 52,
                "vitals": {
                    "health": {"value": 40, "max": 50, "pct": 80},
                    "mana": {"value": 20, "max": 30, "pct": 67},
                    "vigor": {
                        "value": 100,
                        "scale_max": 200,
                        "pct": 50,
                        "rest_threshold": 80,
                        "rested": True,
                    },
                },
                "attributes": {
                    "might": {"value": 25, "display_scale": 50, "hard_cap": 70},
                    "intellect": {
                        "value": 30,
                        "display_scale": 50,
                        "hard_cap": 70,
                    },
                },
                "risk": "low",
                "carried_currency": 12,
                "observation_age_seconds": 0.4,
            },
            "abilities": {
                "ability_scale": "0-100",
                "skills": [
                    {"name": "Dodge", "ability": 12},
                    {"name": "Mace Fighting", "ability": 34},
                ],
                "spells": [
                    {
                        "name": "Blink",
                        "ability": 8,
                        "school": "Riija",
                        "level": 1,
                        "mana": 15,
                    }
                ],
                "spell_readiness": [
                    {
                        "name": "Blink",
                        "castable": False,
                        "blocked_by": ["mana 10/15"],
                    }
                ],
            },
            "inventory": {
                "known": True,
                "items": [
                    {"name": "mace", "quantity": 1, "equipped": True},
                    {"name": "wheel of cheese", "quantity": 3, "equipped": False},
                ],
                "capacity": {
                    "known": True,
                    "items": 2,
                    "weight": 8,
                    "weight_max": 50,
                },
            },
            "equipment": {
                "state": "known",
                "equipped": [{"name": "mace", "slot": "hands"}],
                "wielded_weapons": ["mace"],
            },
        }

    def draft_goal(
        self,
        prompt: str,
        *,
        current_goal: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.draft_requests.append(
            {"prompt": prompt, "current_goal": current_goal}
        )
        index = min(len(self.draft_requests) - 1, len(self.drafts) - 1)
        return {"goal": self.drafts[index], "validation": {"warnings": []}}

    def submit_goal(self, payload: dict[str, object]) -> dict[str, object]:
        self.submissions.append(payload)
        return {"goal": {**payload, "id": "submitted-goal"}}


class TuiTests(unittest.TestCase):
    def test_dashboard_renders_goal_queue_vitals_and_abilities(self) -> None:
        api = FakeApi()

        rendered = render_dashboard(api.status(), api.goals(), api.events(), width=100)

        self.assertIn("Sable", rendered)
        self.assertIn("HP 40/50", rendered)
        self.assertIn("Reach the bank", rendered)
        self.assertIn("Buy bread", rendered)
        self.assertIn("Dodge 12", rendered)
        self.assertIn("Blink 8", rendered)
        self.assertIn("Controller started", rendered)
        self.assertIn("[S] Character status", rendered)

    def test_dashboard_color_mode_marks_states_vitals_and_sections(self) -> None:
        api = FakeApi()

        rendered = render_dashboard(
            api.status(), api.goals(), api.events(), width=100, color=True
        )

        self.assertIn("\x1b[", rendered)
        self.assertIn("\x1b[32mrunning\x1b[0m", rendered)
        self.assertIn("\x1b[1;36m", rendered)

    def test_detailed_character_status_lists_every_requested_category(self) -> None:
        api = FakeApi()

        rendered = render_character_status(api.character_status(), width=100)

        self.assertIn("Dodge", rendered)
        self.assertIn("12/100", rendered)
        self.assertIn("Mace Fighting", rendered)
        self.assertIn("Blink", rendered)
        self.assertIn("8/100", rendered)
        self.assertIn("not castable", rendered)
        self.assertIn("wheel of cheese", rendered)
        self.assertIn("3 x", rendered)
        self.assertIn("Wielding: mace", rendered)
        self.assertIn("mace (hands)", rendered)
        self.assertIn("Health: 40 / 50 (80%)", rendered)
        self.assertIn("Vigor: 100 / 200 (50%; rested; rest threshold 80)", rendered)
        self.assertIn("Might: 25 (display scale 50; hard cap 70)", rendered)
        self.assertNotIn("{'value':", rendered)

    def test_new_goal_flow_drafts_plain_language_and_approves(self) -> None:
        api = FakeApi()
        answers = iter(["Reach the Tos bank safely.", "a"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            payload = prompt_new_goal(api, input_fn=lambda _: next(answers))

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual("Reach the bank", payload["title"])
        self.assertEqual(60, payload["priority"])
        self.assertEqual("queue", payload["activation"])
        self.assertTrue(str(payload["request_id"]).startswith("tui-goal-"))
        self.assertEqual(
            [{"prompt": "Reach the Tos bank safely.", "current_goal": None}],
            api.draft_requests,
        )
        self.assertIn("Structured goal draft", output.getvalue())
        self.assertIn("100 is highest", output.getvalue())

    def test_new_goal_flow_reprompts_model_with_current_draft(self) -> None:
        api = FakeApi()
        revised = {
            **api.drafts[0],
            "priority": 90,
            "constraints": {"avoid_death": True, "bank_before_hazard": True},
        }
        api.drafts.append(revised)
        answers = iter(
            [
                "Reach the Tos bank safely.",
                "m",
                "Raise the priority to 90 and bank before danger.",
                "approve",
            ]
        )

        with contextlib.redirect_stdout(io.StringIO()):
            payload = prompt_new_goal(api, input_fn=lambda _: next(answers))

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(90, payload["priority"])
        self.assertEqual(api.drafts[0], api.draft_requests[1]["current_goal"])
        self.assertEqual(
            "Raise the priority to 90 and bank before danger.",
            api.draft_requests[1]["prompt"],
        )

    def test_new_goal_flow_can_cancel_without_submission_payload(self) -> None:
        api = FakeApi()
        answers = iter(["Reach the Tos bank safely.", "cancel"])

        with contextlib.redirect_stdout(io.StringIO()):
            payload = prompt_new_goal(api, input_fn=lambda _: next(answers))

        self.assertIsNone(payload)

    def test_invalid_review_choice_does_not_call_model_again(self) -> None:
        api = FakeApi()
        answers = iter(["Reach the Tos bank safely.", "maybe", "approve"])

        with contextlib.redirect_stdout(io.StringIO()):
            payload = prompt_new_goal(api, input_fn=lambda _: next(answers))

        self.assertIsNotNone(payload)
        self.assertEqual(1, len(api.draft_requests))

    def test_goal_management_uses_selected_version_and_explicit_cancel(self) -> None:
        goals = FakeApi().goals()
        pause_answers = iter(["1", "p"])
        with contextlib.redirect_stdout(io.StringIO()):
            goal_id, pause = prompt_goal_command(
                goals, input_fn=lambda _: next(pause_answers)
            ) or (None, None)
        self.assertEqual("goal-1", goal_id)
        self.assertEqual("pause", pause["action"])
        self.assertEqual(2, pause["expected_version"])

        cancel_answers = iter(["2", "c", "CANCEL"])
        with contextlib.redirect_stdout(io.StringIO()):
            goal_id, cancel = prompt_goal_command(
                goals, input_fn=lambda _: next(cancel_answers)
            ) or (None, None)
        self.assertEqual("goal-2", goal_id)
        self.assertEqual("cancel", cancel["action"])
        self.assertEqual("operator_requested", cancel["cause"])

        reprioritize_answers = iter(["2", "e", "80"])
        reprioritize_prompts: list[str] = []

        def reprioritize_input(prompt: str) -> str:
            reprioritize_prompts.append(prompt)
            return next(reprioritize_answers)

        with contextlib.redirect_stdout(io.StringIO()):
            goal_id, reprioritize = prompt_goal_command(
                goals, input_fn=reprioritize_input
            ) or (None, None)
        self.assertEqual("goal-2", goal_id)
        self.assertEqual("reprioritize", reprioritize["action"])
        self.assertEqual(80, reprioritize["priority"])
        self.assertIn(
            "New priority (0 lowest, 100 highest) [50; Esc to go back]: ",
            reprioritize_prompts,
        )

    def test_escape_cancels_goal_creation_from_each_prompt_stage(self) -> None:
        initial = FakeApi()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(
                prompt_new_goal(initial, input_fn=lambda _: "\x1b")
            )
        self.assertEqual([], initial.draft_requests)

        review = FakeApi()
        review_answers = iter(["Reach the bank.", "\x1b"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(
                prompt_new_goal(review, input_fn=lambda _: next(review_answers))
            )
        self.assertEqual(1, len(review.draft_requests))

        modify = FakeApi()
        modify_answers = iter(["Reach the bank.", "m", "\x1b"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(
                prompt_new_goal(modify, input_fn=lambda _: next(modify_answers))
            )
        self.assertEqual(1, len(modify.draft_requests))

    def test_escape_cancels_goal_management_from_nested_prompts(self) -> None:
        goals = FakeApi().goals()
        for answers in (
            ["\x1b"],
            ["1", "\x1b"],
            ["2", "e", "\x1b"],
            ["2", "c", "\x1b"],
        ):
            with self.subTest(answers=answers):
                responses = iter(answers)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertIsNone(
                        prompt_goal_command(
                            goals, input_fn=lambda _: next(responses)
                        )
                    )

    def test_tui_can_attach_and_quit_without_stopping_controller(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = run_tui(FakeApi(), key_reader=lambda _: "q")

        self.assertEqual(0, result)
        self.assertIn("MERIDIAN 59 BOT CONSOLE", output.getvalue())

    def test_tui_submits_only_after_draft_approval(self) -> None:
        api = FakeApi()
        keys = iter(["n", "q"])
        answers = iter(["Reach the Tos bank safely.", "approve"])

        with contextlib.redirect_stdout(io.StringIO()):
            result = run_tui(
                api,
                key_reader=lambda _: next(keys),
                input_fn=lambda _: next(answers),
            )

        self.assertEqual(0, result)
        self.assertEqual(1, len(api.draft_requests))
        self.assertEqual(1, len(api.submissions))

    def test_tui_s_opens_detailed_status_and_returns_to_dashboard(self) -> None:
        api = FakeApi()
        keys = iter(["s", "q"])

        with contextlib.redirect_stdout(io.StringIO()):
            result = run_tui(
                api,
                key_reader=lambda _: next(keys),
                input_fn=lambda _: "",
            )

        self.assertEqual(0, result)
        self.assertEqual(1, api.character_status_requests)

    def test_escape_returns_from_character_status_to_dashboard(self) -> None:
        api = FakeApi()
        keys = iter(["s", "q"])

        with contextlib.redirect_stdout(io.StringIO()):
            result = run_tui(
                api,
                key_reader=lambda _: next(keys),
                input_fn=lambda _: "\x1b",
            )

        self.assertEqual(0, result)
        self.assertEqual(1, api.character_status_requests)


if __name__ == "__main__":
    unittest.main()
