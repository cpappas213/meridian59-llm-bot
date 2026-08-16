from __future__ import annotations

import unittest

from meridian_bot.tactical_protocol import (
    EXECUTE_STEP,
    PLAN_CREATE,
    PLAN_REVISE,
    REPAIR_ACTION,
    REPAIR_PLAN,
    TACTICAL_PROTOCOL_VERSION,
    TacticalProtocolError,
    compile_action_response,
    compile_plan_response,
    make_action_option,
    make_state_token,
    select_rule_cards,
    tactical_system_prompt,
)


class TacticalProtocolTests(unittest.TestCase):
    request_id = "tactical-request-1"

    def _state_token(self, *, room_id: int = 106) -> str:
        return make_state_token(
            {"room": {"id": room_id}, "observation_revision": 7},
            request_id=self.request_id,
            goal_id="goal-1",
            phase_id="phase-bank",
            plan_fingerprint="plan-fingerprint-1",
        )

    def _travel_option(self, state_token: str) -> dict[str, object]:
        return make_action_option(
            plan_fingerprint="plan-fingerprint-1",
            observation_token=state_token,
            step_id="reach-bank",
            tool="travel",
            locked_arguments={"to": 54},
            free_argument_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            expected_observation={"room_id": 54},
        )

    def _action_response(
        self, action_token: str, *, arguments: dict[str, object] | None = None
    ) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "action_token": action_token,
            "arguments": arguments or {},
            "rationale": "Continue the verified bank prerequisite.",
            "expected_observation": {"room_id": 54},
        }

    def test_action_token_binds_step_tool_and_locked_arguments(self) -> None:
        state_token = self._state_token()
        binding = {
            "plan_fingerprint": "plan-fingerprint-1",
            "observation_token": state_token,
            "step_id": "reach-bank",
            "tool": "travel",
            "locked_arguments": {"to": 54},
            "free_argument_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "expected_observation": {"room_id": 54},
        }
        option = make_action_option(**binding)
        changed_step = make_action_option(**{**binding, "step_id": "withdraw-funds"})
        changed_tool = make_action_option(**{**binding, "tool": "bank"})
        changed_arguments = make_action_option(
            **{**binding, "locked_arguments": {"to": 55}}
        )
        changed_plan = make_action_option(
            **{**binding, "plan_fingerprint": "plan-fingerprint-2"}
        )
        changed_state = make_action_option(
            **{**binding, "observation_token": self._state_token(room_id=107)}
        )
        changed_schema = make_action_option(
            **{
                **binding,
                "free_argument_schema": {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "additionalProperties": False,
                },
            }
        )
        changed_expectation = make_action_option(
            **{**binding, "expected_observation": {"room_id": 55}}
        )

        issued_token = option["action_token"]
        self.assertIsInstance(issued_token, str)
        self.assertTrue(issued_token)
        self.assertNotEqual(issued_token, changed_step["action_token"])
        self.assertNotEqual(issued_token, changed_tool["action_token"])
        self.assertNotEqual(issued_token, changed_arguments["action_token"])
        self.assertNotEqual(issued_token, changed_plan["action_token"])
        self.assertNotEqual(issued_token, changed_state["action_token"])
        self.assertNotEqual(issued_token, changed_schema["action_token"])
        self.assertNotEqual(issued_token, changed_expectation["action_token"])

        decision = compile_action_response(
            self._action_response(str(issued_token)),
            [option],
            request_id=self.request_id,
            state_token=state_token,
        )
        self.assertEqual("act", decision["decision"])
        self.assertEqual("reach-bank", decision["plan_step_id"])
        self.assertEqual("travel", decision["tool"])
        self.assertEqual({"to": 54}, decision["arguments"])
        self.assertEqual({"room_id": 54}, decision["expected_observation"])

    def test_state_token_binds_request_goal_phase_plan_and_observation(self) -> None:
        base = make_state_token(
            {"room": {"id": 106}, "observation_revision": 7},
            request_id=self.request_id,
            goal_id="goal-1",
            phase_id="phase-bank",
            plan_fingerprint="plan-fingerprint-1",
        )
        variants = {
            make_state_token(
                {"room": {"id": 106}, "observation_revision": 7},
                request_id="tactical-request-2",
                goal_id="goal-1",
                phase_id="phase-bank",
                plan_fingerprint="plan-fingerprint-1",
            ),
            make_state_token(
                {"room": {"id": 106}, "observation_revision": 7},
                request_id=self.request_id,
                goal_id="goal-2",
                phase_id="phase-bank",
                plan_fingerprint="plan-fingerprint-1",
            ),
            make_state_token(
                {"room": {"id": 106}, "observation_revision": 7},
                request_id=self.request_id,
                goal_id="goal-1",
                phase_id="phase-shop",
                plan_fingerprint="plan-fingerprint-1",
            ),
            make_state_token(
                {"room": {"id": 106}, "observation_revision": 7},
                request_id=self.request_id,
                goal_id="goal-1",
                phase_id="phase-bank",
                plan_fingerprint="plan-fingerprint-2",
            ),
            make_state_token(
                {"room": {"id": 107}, "observation_revision": 7},
                request_id=self.request_id,
                goal_id="goal-1",
                phase_id="phase-bank",
                plan_fingerprint="plan-fingerprint-1",
            ),
        }
        self.assertNotIn(base, variants)
        self.assertEqual(5, len(variants))

    def test_action_compiler_rejects_unknown_stale_and_overridden_tokens(self) -> None:
        state_token = self._state_token()
        option = self._travel_option(state_token)

        with self.assertRaises(TacticalProtocolError):
            compile_action_response(
                self._action_response("not-an-issued-token"),
                [option],
                request_id=self.request_id,
                state_token=state_token,
            )

        fresh_state_token = self._state_token(room_id=107)
        with self.assertRaises(TacticalProtocolError):
            compile_action_response(
                self._action_response(str(option["action_token"])),
                [option],
                request_id=self.request_id,
                state_token=fresh_state_token,
            )

        with self.assertRaises(TacticalProtocolError):
            compile_action_response(
                self._action_response(
                    str(option["action_token"]), arguments={"to": 999}
                ),
                [option],
                request_id=self.request_id,
                state_token=state_token,
            )

    def test_action_compiler_rejects_tampered_option_and_empty_option_set(self) -> None:
        state_token = self._state_token()
        option = self._travel_option(state_token)
        tampered = {**option, "locked_arguments": {"to": 999}}

        with self.assertRaises(TacticalProtocolError) as tampered_error:
            compile_action_response(
                self._action_response(str(option["action_token"])),
                [tampered],
                request_id=self.request_id,
                state_token=state_token,
            )
        self.assertEqual(
            "ACTION_TOKEN_BINDING_MISMATCH", tampered_error.exception.code
        )

        with self.assertRaises(TacticalProtocolError) as empty_error:
            compile_action_response(
                self._action_response(str(option["action_token"])),
                [],
                request_id=self.request_id,
                state_token=state_token,
            )
        self.assertEqual("NO_LEGAL_ACTIONS", empty_error.exception.code)

    def test_action_compiler_rejects_changed_controller_expectation(self) -> None:
        state_token = self._state_token()
        option = self._travel_option(state_token)
        response = self._action_response(str(option["action_token"]))
        response["expected_observation"] = {"room_id": 999}

        with self.assertRaises(TacticalProtocolError) as mismatch:
            compile_action_response(
                response,
                [option],
                request_id=self.request_id,
                state_token=state_token,
            )
        self.assertEqual("EXPECTED_OBSERVATION_MISMATCH", mismatch.exception.code)

    def test_action_option_rejects_locked_required_schema_conflict(self) -> None:
        with self.assertRaises(TacticalProtocolError) as conflict:
            make_action_option(
                plan_fingerprint="plan-fingerprint-1",
                observation_token=self._state_token(),
                step_id="reach-bank",
                tool="travel",
                locked_arguments={"to": 54},
                free_argument_schema={
                    "type": "object",
                    "properties": {"to": {"type": "integer"}},
                    "required": ["to"],
                    "additionalProperties": False,
                },
            )
        self.assertEqual("LOCKED_ARGUMENT_SCHEMA_CONFLICT", conflict.exception.code)

    def test_protocol_rejects_non_finite_numbers(self) -> None:
        for label, value in (
            ("nan", float("nan")),
            ("positive infinity", float("inf")),
            ("negative infinity", float("-inf")),
        ):
            with self.subTest(label=label):
                with self.assertRaises(TacticalProtocolError) as invalid_state:
                    make_state_token(
                        {"vigor_ratio": value},
                        request_id=self.request_id,
                        goal_id="goal-1",
                    )
                self.assertEqual(
                    "INVALID_STATE_CONTEXT", invalid_state.exception.code
                )
                self.assertEqual(
                    "$.vigor_ratio", invalid_state.exception.details["path"]
                )

        state_token = self._state_token()
        option = make_action_option(
            plan_fingerprint="plan-fingerprint-1",
            observation_token=state_token,
            step_id="set-threshold",
            tool="autopilot",
            free_argument_schema={
                "type": "object",
                "properties": {
                    "flee_below": {"type": "number", "minimum": 0, "maximum": 1}
                },
                "required": ["flee_below"],
                "additionalProperties": False,
            },
        )
        with self.assertRaises(TacticalProtocolError) as invalid_response:
            compile_action_response(
                {
                    "request_id": self.request_id,
                    "action_token": option["action_token"],
                    "arguments": {"flee_below": float("nan")},
                    "rationale": "Use the bounded threshold.",
                    "expected_observation": {},
                },
                [option],
                request_id=self.request_id,
                state_token=state_token,
            )
        self.assertEqual("INVALID_RESPONSE_OBJECT", invalid_response.exception.code)
        self.assertEqual(
            "$.arguments.flee_below", invalid_response.exception.details["path"]
        )

    def test_plan_compiler_appends_exact_selected_safe_ending(self) -> None:
        candidates = {
            "safe:106": {
                "candidate_id": "safe:106",
                "room_id": 106,
                "name": "The Barloque Adventurers Hall",
                "flags": ["ROOM_NO_COMBAT"],
                "evidence": {
                    "source_tier": "source-derived",
                    "source_ref": "test fixture",
                },
            }
        }
        response = {
            "request_id": self.request_id,
            "summary": "Withdraw the required funds.",
            "steps": [
                {
                    "id": "reach-bank",
                    "outcome": "Reach verified bank room 54.",
                    "tool": "travel",
                    "verification": "Current room id is 54.",
                },
                {
                    "id": "withdraw-funds",
                    "outcome": "Withdraw the required funds.",
                    "tool": "bank",
                    "verification": "Fresh currency and a bank receipt confirm it.",
                },
            ],
            "safe_ending": {
                "candidate_id": "safe:106",
                "rationale": "Return to the verified no-combat hall after banking.",
            },
            "assumptions": [],
            "revision_reason": None,
        }

        compiled = compile_plan_response(
            response,
            candidates,
            request_id=self.request_id,
        )

        self.assertEqual(3, len(compiled["steps"]))
        self.assertEqual("reach-bank", compiled["steps"][0]["id"])
        final_step = compiled["steps"][-1]
        self.assertEqual("travel", final_step["tool"])
        self.assertEqual(final_step["id"], compiled["safe_ending"]["step_id"])
        self.assertEqual(106, compiled["safe_ending"]["room_id"])
        self.assertIn(
            "106",
            f"{final_step.get('outcome', '')} {final_step.get('verification', '')}",
        )
        self.assertEqual(
            "Return to the verified no-combat hall after banking.",
            compiled["safe_ending"]["rationale"],
        )

        unknown = {
            **response,
            "safe_ending": {
                "candidate_id": "safe:999",
                "rationale": "This candidate was not offered.",
            },
        }
        with self.assertRaises(TacticalProtocolError):
            compile_plan_response(
                unknown,
                candidates,
                request_id=self.request_id,
            )

    def test_plan_compiler_requires_at_least_one_work_step(self) -> None:
        response = {
            "request_id": self.request_id,
            "summary": "A safe epilogue is not phase work.",
            "steps": [],
            "safe_ending": {
                "candidate_id": "safe:106",
                "rationale": "Finish in the verified no-combat hall.",
            },
            "assumptions": [],
            "revision_reason": None,
        }
        with self.assertRaises(TacticalProtocolError) as missing_work:
            compile_plan_response(
                response,
                {"safe:106": {"room_id": 106}},
                request_id=self.request_id,
            )
        self.assertEqual("PLAN_WORK_STEP_REQUIRED", missing_work.exception.code)

    def test_rule_routing_selects_only_relevant_commerce_and_safety_cards(self) -> None:
        cards = select_rule_cards(
            mode=PLAN_CREATE,
            phase={
                "kind": "commerce",
                "objective": (
                    "Withdraw bank funds, discover a merchant buyer after a refusal, "
                    "sell the item, and finish safely."
                ),
            },
            tool_names=("travel", "bank", "merchants", "sell"),
            feedback={
                "reason": "The previous merchant refused the item; discover a buyer first."
            },
            limit=3,
        )

        identifiers = [str(card["id"]).casefold() for card in cards]
        self.assertTrue(any("bank" in identifier for identifier in identifiers))
        self.assertTrue(
            any(
                "merchant" in identifier or "buyer" in identifier
                for identifier in identifiers
            )
        )
        self.assertTrue(any("safe" in identifier for identifier in identifiers))
        self.assertFalse(any("farm" in identifier for identifier in identifiers))
        self.assertFalse(any("pvp" in identifier for identifier in identifiers))

    def test_each_mode_has_a_narrow_non_union_prompt(self) -> None:
        self.assertIsInstance(TACTICAL_PROTOCOL_VERSION, str)
        self.assertTrue(TACTICAL_PROTOCOL_VERSION.startswith("tactical"))
        plan_modes = (PLAN_CREATE, PLAN_REVISE, REPAIR_PLAN)
        action_modes = (EXECUTE_STEP, REPAIR_ACTION)

        for mode in (*plan_modes, *action_modes):
            with self.subTest(mode=mode):
                prompt = tactical_system_prompt(mode)
                compact = "".join(prompt.split())
                self.assertIn(mode, prompt)
                self.assertLess(len(prompt), 6_000)
                self.assertNotIn("plan|act|wait|propose_goal", compact)
                self.assertNotIn('"decision":', compact)

                if mode in plan_modes:
                    self.assertNotIn("action_token", prompt)
                    self.assertIn("one to nine", prompt.casefold())
                else:
                    self.assertIn("action_token", prompt)
                    self.assertNotIn("execution_plan", prompt)
                    self.assertIn("1000 characters", prompt)
                    self.assertIn("expected_observation exactly", prompt)

        create_prompt = tactical_system_prompt(PLAN_CREATE)
        self.assertIn("at most 20 assumptions", create_prompt)
        self.assertIn("outcomes and verifications at most 600", create_prompt)
        self.assertIn("repeat_count from 1 to 100", create_prompt)

        repair_prompt = tactical_system_prompt(REPAIR_PLAN)
        self.assertIn("initial PLAN_CREATE", repair_prompt)
        self.assertIn("revision_reason must be null", repair_prompt)
        self.assertIn("PLAN_REVISE", repair_prompt)
        self.assertIn("revision_reason must remain non-empty", repair_prompt)


if __name__ == "__main__":
    unittest.main()
