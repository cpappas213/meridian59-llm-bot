from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from meridian_bot.cli import parser, prompt_persona, setup_persona
from meridian_bot.config import OnboardingConfig
from meridian_bot.controller import BotController

from .helpers import config


class CliTests(unittest.TestCase):
    def test_parser_exposes_local_persona_setup(self) -> None:
        args = parser().parse_args(
            [
                "--config",
                "bot.toml",
                "setup-persona",
                "--update-existing",
                "--reuse-current",
                "--replace-existing-character",
            ]
        )

        self.assertEqual("setup-persona", args.command)
        self.assertTrue(args.update_existing)
        self.assertTrue(args.reuse_current)
        self.assertTrue(args.replace_existing_character)

    def test_prompt_persona_collects_every_supported_field(self) -> None:
        answers = iter(
            [
                "",
                "Sable",
                "",
                "A pragmatic, sharp-witted adventurer.",
                "curious, wry, self-possessed",
                "brief during danger, natural rather than theatrical",
                "competence, self-preservation",
                "credentials, out-of-game system details",
                "Warm slowly; remember favors and betrayals.",
                "not-a-number",
                "500",
            ]
        )
        output: list[str] = []

        persona = prompt_persona(
            input_fn=lambda _: next(answers),
            output_fn=output.append,
        )

        self.assertEqual("Sable", persona["name"])
        self.assertEqual(
            ["curious", "wry", "self-possessed"], persona["traits"]
        )
        self.assertEqual(
            ["brief during danger", "natural rather than theatrical"],
            persona["speech_style"],
        )
        self.assertEqual(500, persona["max_reply_characters"])
        self.assertIn("A character name is required.", output)
        self.assertIn("A voice and identity concept is required.", output)
        guidance = "\n".join(output)
        self.assertIn("2-4 sentences or 40-100 words", guidance)
        self.assertIn("initial build, roleplay-aware planning", guidance)
        self.assertIn("Never include credentials or private data", guidance)
        self.assertIn("Enter a whole number from 1 through 1000.", output)

    def test_setup_persona_persists_first_run_and_preserves_it_on_reinstall(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = replace(
                config(Path(temporary)), onboarding=OnboardingConfig(enabled=True)
            )
            answers = iter(
                [
                    "Sable",
                    "A curious mystic.",
                    "curious, guarded",
                    "brief in danger",
                    "competence",
                    "credentials",
                    "Warm toward proven allies.",
                    "360",
                ]
            )
            first_output: list[str] = []

            result = setup_persona(
                value,
                input_fn=lambda _: next(answers),
                output_fn=first_output.append,
            )

            self.assertEqual(0, result)
            controller = BotController(value)
            try:
                stored = controller.persona()
                self.assertEqual(1, stored["version"])
                self.assertEqual("Sable", stored["name"])
                self.assertEqual("pending", stored["onboarding"]["status"])
            finally:
                controller.close()

            second_output: list[str] = []

            def unexpected_prompt(_: str) -> str:
                self.fail("an existing persona must be preserved without prompting")

            result = setup_persona(
                value,
                input_fn=unexpected_prompt,
                output_fn=second_output.append,
            )

            self.assertEqual(0, result)
            preserved = json.loads(second_output[-1])
            self.assertEqual("preserved", preserved["status"])
            self.assertEqual(1, preserved["version"])
            self.assertEqual("Sable", preserved["name"])

    def test_setup_persona_can_explicitly_reuse_for_character_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = replace(
                config(Path(temporary)), onboarding=OnboardingConfig(enabled=True)
            )
            setup_persona(
                value,
                input_fn=lambda prompt: {
                    "Desired character name: ": "Sable",
                    "Voice and identity concept (one paragraph): ": "A curious mystic.",
                    "Personality traits (comma-separated): ": "curious",
                    "Speech-style rules (comma-separated): ": "brief",
                    "Values and motivations (comma-separated): ": "competence",
                    "Conversation taboos (comma-separated): ": "credentials",
                    "Default posture toward strangers, friends, rivals, favors, and betrayals: ": "Warm slowly.",
                    "Maximum characters per in-game reply [360]: ": "360",
                }[prompt],
                output_fn=lambda _: None,
            )

            result = setup_persona(
                value,
                update_existing=True,
                reuse_current=True,
                replace_existing_character=True,
                output_fn=lambda _: None,
            )

            self.assertEqual(0, result)
            controller = BotController(value)
            try:
                self.assertEqual(2, controller.persona()["version"])
                onboarding = controller.storage.get_runtime("onboarding_v1")
                self.assertTrue(onboarding["replace_existing_character"])
            finally:
                controller.close()

    def test_character_replacement_requires_explicit_persona_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "requires --update-existing"):
                setup_persona(
                    config(Path(temporary)),
                    replace_existing_character=True,
                    output_fn=lambda _: None,
                )


if __name__ == "__main__":
    unittest.main()
