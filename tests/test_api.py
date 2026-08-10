from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

from meridian_bot.api import ApiServers
from meridian_bot.controller import BotController
from meridian_bot.storage import InvalidTransition

from .helpers import config, goal_payload
from .test_knowledge import make_compendium


class ApiTests(unittest.TestCase):
    def request_json(
        self,
        url: str,
        *,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"authorization": "Bearer test-token", "content-type": "application/json"},
            method="POST" if body is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.load(response)

    def test_control_auth_and_separate_read_only_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))

            class DraftModel:
                def draft_goal(self, **_: object) -> dict[str, object]:
                    return {
                        "title": "Meet the operator's request",
                        "objective": "Complete the requested outcome.",
                        "success_criteria": [
                            {"id": "confirmed", "kind": "operator_confirmed"}
                        ],
                        "constraints": {},
                        "priority": 50,
                        "activation": "queue",
                    }

            servers = ApiServers(controller)
            try:
                controller.startup(connect_game=False)
                controller.model = DraftModel()  # type: ignore[assignment]
                servers.start()
                control_port = servers.control.server_address[1]
                dashboard_port = servers.dashboard.server_address[1]
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(f"http://127.0.0.1:{control_port}/v1/status", timeout=2)
                self.assertEqual(401, denied.exception.code)
                request = urllib.request.Request(
                    f"http://127.0.0.1:{control_port}/v1/status",
                    headers={"authorization": "Bearer test-token"},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual("running", json.load(response)["controller"]["state"])
                goals_request = urllib.request.Request(
                    f"http://127.0.0.1:{control_port}/v1/goals",
                    headers={"authorization": "Bearer test-token"},
                )
                with urllib.request.urlopen(goals_request, timeout=2) as response:
                    self.assertEqual({"goals": []}, json.load(response))
                character_request = urllib.request.Request(
                    f"http://127.0.0.1:{control_port}/v1/character",
                    headers={"authorization": "Bearer test-token"},
                )
                with urllib.request.urlopen(character_request, timeout=2) as response:
                    character_status = json.load(response)
                    self.assertEqual(200, response.status)
                    self.assertIn("abilities", character_status)
                    self.assertIn("inventory", character_status)
                    self.assertIn("equipment", character_status)
                draft_status, draft = self.request_json(
                    f"http://127.0.0.1:{control_port}/v1/goals/draft",
                    body={"prompt": "Do something useful."},
                )
                self.assertEqual(200, draft_status)
                self.assertEqual(
                    "Meet the operator's request", draft["goal"]["title"]
                )
                mutation = urllib.request.Request(
                    f"http://127.0.0.1:{dashboard_port}/goals",
                    data=b"{}",
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as read_only:
                    urllib.request.urlopen(mutation, timeout=2)
                self.assertEqual(405, read_only.exception.code)
            finally:
                servers.stop()
                controller.storage.close()

    def test_knowledge_routes_resolve_validate_and_reject_unknown_goal_location(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = config(root)
            harness = make_compendium(root)
            value = replace(value, harness=replace(value.harness, root=harness, expected_revision="fixture-revision"))
            controller = BotController(value)
            servers = ApiServers(controller)
            try:
                controller.startup(connect_game=False)
                servers.start()
                control_port = servers.control.server_address[1]
                base = f"http://127.0.0.1:{control_port}"

                status, resolved = self.request_json(base + "/v1/knowledge/resolve?q=Tos%20Inn&kinds=location")
                self.assertEqual(200, status)
                self.assertEqual(52, resolved["entity"]["facts"]["room_id"])

                _, validation = self.request_json(
                    base + "/v1/knowledge/validate-goal",
                    body={
                        "goal": {
                            "objective": "Explore Silverfall.",
                            "success_criteria": [{"kind": "location_reached", "location": "Silverfall"}],
                        }
                    },
                )
                self.assertFalse(validation["valid"])
                self.assertEqual("UNKNOWN_LOCATION", validation["errors"][0]["code"])

                goal_request = urllib.request.Request(
                    base + "/v1/goals",
                    data=json.dumps(
                        {
                            "request_id": "silverfall-regression",
                            "objective": "Explore Silverfall.",
                            "success_criteria": [{"kind": "location_reached", "location": "Silverfall"}],
                        }
                    ).encode("utf-8"),
                    headers={"authorization": "Bearer test-token", "content-type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(goal_request, timeout=2)
                self.assertEqual(400, rejected.exception.code)
                error = json.load(rejected.exception)
                self.assertEqual("KNOWLEDGE_VALIDATION_FAILED", error["code"])
                self.assertEqual("UNKNOWN_LOCATION", error["details"]["errors"][0]["code"])
            finally:
                servers.stop()
                controller.storage.close()

    def test_active_goal_commitment_guard_requires_verified_or_operator_cause(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = BotController(config(Path(temporary)))
            try:
                goal = controller.storage.submit_goal(goal_payload("commitment"))["goal"]

                with self.assertRaisesRegex(InvalidTransition, "GOAL_COMMITMENT_GUARD"):
                    controller.manage_goal(
                        {
                            "request_id": "cancel-too-fast",
                            "goal_id": goal["id"],
                            "expected_version": goal["version"],
                            "action": "cancel",
                            "reason": "No criterion moved immediately.",
                        }
                    )

                current = controller.storage.goal(goal["id"])
                cancelled = controller.manage_goal(
                    {
                        "request_id": "cancel-by-human",
                        "goal_id": goal["id"],
                        "expected_version": current["version"],
                        "action": "cancel",
                        "cause": "operator_requested",
                        "reason": "The human explicitly requested cancellation.",
                    }
                )
                self.assertEqual("cancelled", cancelled["goal"]["status"])
                self.assertTrue(cancelled["cancellation_assessment"]["allowed"])
            finally:
                controller.storage.close()


if __name__ == "__main__":
    unittest.main()
