from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .controller import BotController
from .storage import StorageError
from .utils import redact


def _error(exc: Exception) -> tuple[int, dict[str, Any]]:
    code = getattr(exc, "code", "INVALID_REQUEST" if isinstance(exc, ValueError) else "INTERNAL_ERROR")
    statuses = {
        "NOT_FOUND": 404,
        "VERSION_CONFLICT": 409,
        "IDEMPOTENCY_CONFLICT": 409,
        "INVALID_TRANSITION": 409,
        "ONBOARDING_REQUIRED": 409,
        "GOAL_DEFERRED": 409,
        "POLICY_DENIED": 403,
        "CONTROLLER_NOT_READY": 503,
        "MODEL_UNAVAILABLE": 503,
    }
    details = getattr(exc, "result", {}) if isinstance(getattr(exc, "result", {}), dict) else {}
    return statuses.get(code, 400 if isinstance(exc, (ValueError, StorageError)) else 500), {
        "code": code,
        "message": str(exc) if code != "INTERNAL_ERROR" else "internal controller error",
        "retryable": code in {"VERSION_CONFLICT", "CONTROLLER_NOT_READY", "BROKER_UNAVAILABLE", "MODEL_UNAVAILABLE"},
        "details": details,
    }


class ApiServers:
    def __init__(self, controller: BotController):
        self.controller = controller
        self.control: ThreadingHTTPServer | None = None
        self.dashboard: ThreadingHTTPServer | None = None
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        self.control = ThreadingHTTPServer(
            (self.controller.config.controller.control_bind, self.controller.config.controller.control_port),
            self._control_handler(),
        )
        self.dashboard = ThreadingHTTPServer(
            (self.controller.config.controller.dashboard_bind, self.controller.config.controller.dashboard_port),
            self._dashboard_handler(),
        )
        for name, server in (("control-api", self.control), ("dashboard-api", self.dashboard)):
            thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self) -> None:
        for server in (self.control, self.dashboard):
            if server:
                server.shutdown()
                server.server_close()

    def _control_handler(self) -> type[BaseHTTPRequestHandler]:
        controller = self.controller
        token = controller.config.control_token

        class ControlHandler(JsonHandler):
            def finish(self) -> None:
                try:
                    super().finish()
                finally:
                    controller.storage.close()

            def authorized(self) -> bool:
                return self.headers.get("authorization") == f"Bearer {token}"

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/v1/health/live":
                    return self.send_json(200, {"ok": True, "state": controller.state})
                if not self.authorized():
                    return self.send_json(401, {"code": "UNAUTHORIZED", "message": "bearer token required"})
                query = parse_qs(parsed.query)
                try:
                    if parsed.path == "/v1/health/ready":
                        ready = controller.state in {"running", "degraded"}
                        return self.send_json(200 if ready else 503, {"ready": ready, "state": controller.state, "dependencies": controller.dependencies})
                    if parsed.path == "/v1/status":
                        return self.send_json(200, controller.status(detail=_first(query, "detail", "summary"), include_recent_events=int(_first(query, "include_recent_events", "3"))))
                    if parsed.path == "/v1/character":
                        return self.send_json(200, controller.character_status())
                    if parsed.path == "/v1/goals":
                        statuses = [
                            part
                            for item in query.get("statuses", [])
                            for part in item.split(",")
                            if part
                        ]
                        return self.send_json(
                            200,
                            {"goals": controller.storage.goals(statuses or None)},
                        )
                    if parsed.path == "/v1/proposals":
                        return self.send_json(200, {"proposals": controller.storage.proposals(_first(query, "status", "pending") or None)})
                    if parsed.path == "/v1/consequences":
                        return self.send_json(200, {"consequences": controller.storage.recent_consequences(int(_first(query, "limit", "20")))})
                    if parsed.path == "/v1/persona":
                        return self.send_json(200, controller.persona())
                    if parsed.path == "/v1/events":
                        kinds = [part for item in query.get("kinds", []) for part in item.split(",") if part]
                        return self.send_json(200, controller.storage.events(after_cursor=int(_first(query, "after_cursor", "0")), limit=int(_first(query, "limit", "50")), interesting_only=_bool(_first(query, "interesting_only", "false")), kinds=kinds or None))
                    if parsed.path == "/v1/knowledge/metadata":
                        return self.send_json(200, controller.knowledge.metadata())
                    if parsed.path == "/v1/knowledge/search":
                        kinds = [part for item in query.get("kinds", []) for part in item.split(",") if part]
                        return self.send_json(
                            200,
                            controller.knowledge.search(
                                _first(query, "q", ""),
                                kinds=kinds or None,
                                limit=int(_first(query, "limit", "8")),
                            ),
                        )
                    if parsed.path == "/v1/knowledge/resolve":
                        kinds = [part for item in query.get("kinds", []) for part in item.split(",") if part]
                        return self.send_json(
                            200,
                            controller.knowledge.resolve(
                                _first(query, "q", ""),
                                kinds=kinds or None,
                                limit=int(_first(query, "limit", "8")),
                                allow_fuzzy=_bool(_first(query, "allow_fuzzy", "false")),
                            ),
                        )
                    if parsed.path.startswith("/v1/knowledge/entities/"):
                        entity_id = unquote(parsed.path[len("/v1/knowledge/entities/") :])
                        result = controller.knowledge.get(entity_id)
                        return self.send_json(200 if result.get("status") == "found" else 404, result)
                    return self.send_json(404, {"code": "NOT_FOUND", "message": "route not found"})
                except Exception as exc:
                    status, body = _error(exc)
                    return self.send_json(status, body)

            def do_POST(self) -> None:
                if not self.authorized():
                    return self.send_json(401, {"code": "UNAUTHORIZED", "message": "bearer token required"})
                parsed = urlparse(self.path)
                try:
                    body = self.read_json()
                    if parsed.path == "/v1/goals/draft":
                        return self.send_json(200, controller.draft_goal(body))
                    if parsed.path == "/v1/goals":
                        return self.send_json(201, controller.submit_goal(body))
                    if parsed.path.startswith("/v1/goals/") and parsed.path.endswith("/commands"):
                        goal_id = parsed.path[len("/v1/goals/") : -len("/commands")].strip("/")
                        return self.send_json(200, controller.manage_goal({**body, "goal_id": goal_id}))
                    if parsed.path.startswith("/v1/proposals/") and parsed.path.endswith("/decision"):
                        proposal_id = parsed.path[len("/v1/proposals/") : -len("/decision")].strip("/")
                        return self.send_json(200, controller.decide_proposal({**body, "proposal_id": proposal_id}))
                    if parsed.path == "/v1/knowledge/validate-goal":
                        goal = body.get("goal") if isinstance(body.get("goal"), dict) else body
                        return self.send_json(200, controller.validate_goal(goal))
                    if parsed.path == "/v1/knowledge/progression-context":
                        return self.send_json(200, controller.progression_context(body))
                    if parsed.path == "/v1/runtime/safe-stop":
                        controller.safe_stop()
                        return self.send_json(202, {"stopping": True})
                    return self.send_json(404, {"code": "NOT_FOUND", "message": "route not found"})
                except Exception as exc:
                    status, response = _error(exc)
                    return self.send_json(status, response)

            def do_PUT(self) -> None:
                if not self.authorized():
                    return self.send_json(401, {"code": "UNAUTHORIZED", "message": "bearer token required"})
                try:
                    if urlparse(self.path).path != "/v1/persona":
                        return self.send_json(404, {"code": "NOT_FOUND", "message": "route not found"})
                    return self.send_json(200, controller.set_persona(self.read_json()))
                except Exception as exc:
                    status, response = _error(exc)
                    return self.send_json(status, response)

            do_HEAD = do_GET

        return ControlHandler

    def _dashboard_handler(self) -> type[BaseHTTPRequestHandler]:
        controller = self.controller

        class DashboardHandler(JsonHandler):
            def finish(self) -> None:
                try:
                    super().finish()
                finally:
                    controller.storage.close()

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if parsed.path == "/health":
                    return self.send_json(200, {"ok": controller.state not in {"stopped", "incompatible"}, "state": controller.state})
                if parsed.path == "/status":
                    return self.send_json(200, redact(controller.status(include_recent_events=3)))
                if parsed.path == "/goals":
                    return self.send_json(200, {"goals": redact(controller.storage.goals())})
                if parsed.path == "/events":
                    return self.send_json(200, controller.storage.events(after_cursor=int(_first(query, "after_cursor", "0")), limit=int(_first(query, "limit", "50")), interesting_only=_bool(_first(query, "interesting_only", "true"))))
                if parsed.path in {"/", "/index.html"}:
                    body = DASHBOARD_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("content-type", "text/html; charset=utf-8")
                    self.send_header("content-length", str(len(body)))
                    self.send_header("cache-control", "no-store")
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                    return
                return self.send_json(404, {"code": "NOT_FOUND", "message": "route not found"})

            def do_POST(self) -> None:
                self.send_json(405, {"code": "METHOD_NOT_ALLOWED", "message": "dashboard is read-only"})

            do_PUT = do_POST
            do_PATCH = do_POST
            do_DELETE = do_POST
            do_HEAD = do_GET

        return DashboardHandler


class JsonHandler(BaseHTTPRequestHandler):
    server_version = "MeridianBot/0.1"

    def send_json(self, status: int, value: Any) -> None:
        body = json.dumps(redact(value), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0"))
        if length <= 0 or length > 1_000_000:
            raise ValueError("request body must be 1-1000000 bytes")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def log_message(self, format: str, *args: Any) -> None:
        return


def _first(query: dict[str, list[str]], key: str, default: str) -> str:
    return query.get(key, [default])[0]


def _bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


DASHBOARD_HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Meridian 59 Bot</title><style>body{font:16px system-ui;background:#111827;color:#e5e7eb;max-width:900px;margin:3rem auto;padding:0 1rem}pre{white-space:pre-wrap;background:#1f2937;padding:1rem;border-radius:.5rem}</style></head><body><h1>Meridian 59 Bot</h1><p>Read-only local-network status.</p><pre id=s>Loading…</pre><script>async function r(){let x=await fetch('/status');document.querySelector('#s').textContent=JSON.stringify(await x.json(),null,2)}r();setInterval(r,10000)</script></body></html>"""
