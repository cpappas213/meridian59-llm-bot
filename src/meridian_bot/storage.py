from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .contracts import (
    CRITERION_FIELDS_BY_KIND,
    CRITERION_KINDS,
    GOAL_CONSTRAINT_FIELDS,
    GOAL_EVENT_KINDS,
)
from .persona import PERSONA_FIELDS, PERSONA_LIST_FIELDS, PERSONA_STRING_FIELDS
from .utils import canonical_json, json_hash, timestamp, uuid7


TERMINAL_GOAL_STATES = {"succeeded", "failed", "cancelled"}
GOAL_STATES = {"proposed", "queued", "active", "paused", "blocked", *TERMINAL_GOAL_STATES}
class StorageError(RuntimeError):
    code = "INTERNAL_ERROR"


class ConflictError(StorageError):
    code = "VERSION_CONFLICT"


class IdempotencyConflict(StorageError):
    code = "IDEMPOTENCY_CONFLICT"


class InvalidTransition(StorageError):
    code = "INVALID_TRANSITION"


class NotFound(StorageError):
    code = "NOT_FOUND"


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._migration_lock = threading.Lock()
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            self._local.connection = connection
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def initialize(self) -> None:
        with self._migration_lock:
            connection = self._connect()
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS idempotency (
                    request_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS goals (
                    id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    success_criteria_json TEXT NOT NULL,
                    constraints_json TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    activated_at TEXT,
                    terminal_at TEXT,
                    blocked_reason TEXT,
                    completion_json TEXT NOT NULL,
                    retry_of_goal_id TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_goal
                    ON goals(status) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS goals_queue
                    ON goals(status, priority DESC, created_at ASC);

                CREATE TABLE IF NOT EXISTS goal_lessons (
                    id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL REFERENCES goals(id),
                    goal_family TEXT NOT NULL,
                    tactic_key TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    failed_state_json TEXT NOT NULL,
                    evidence_event_ids_json TEXT NOT NULL,
                    retry_when_json TEXT NOT NULL,
                    suggested_goals_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    unlocked_at TEXT,
                    resolved_at TEXT,
                    resolution_goal_id TEXT
                );
                CREATE INDEX IF NOT EXISTS goal_lessons_family_status
                    ON goal_lessons(goal_family, status, created_at DESC);
                CREATE INDEX IF NOT EXISTS goal_lessons_goal
                    ON goal_lessons(goal_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS goal_transitions (
                    id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL REFERENCES goals(id),
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    reason TEXT,
                    actor TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    goal_version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    expected_value TEXT,
                    goal_draft_json TEXT NOT NULL,
                    risk_summary TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    resulting_goal_id TEXT
                );

                CREATE TABLE IF NOT EXISTS persona_versions (
                    version INTEGER PRIMARY KEY AUTOINCREMENT,
                    persona_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    occurred_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    interesting INTEGER NOT NULL,
                    character_json TEXT,
                    goal_id TEXT,
                    location_json TEXT,
                    summary TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    correlation_id TEXT,
                    causation_id TEXT,
                    policy_decision_id TEXT
                );
                CREATE INDEX IF NOT EXISTS events_kind_cursor ON events(kind, cursor);
                CREATE INDEX IF NOT EXISTS events_interesting_cursor ON events(interesting, cursor);

                CREATE TABLE IF NOT EXISTS consequence_assessments (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    action_class TEXT NOT NULL,
                    target_json TEXT NOT NULL,
                    expected_effects_json TEXT NOT NULL,
                    goal_rationale TEXT NOT NULL,
                    safer_alternatives_json TEXT NOT NULL,
                    guidance TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    pre_action_event_id TEXT,
                    outcome_event_id TEXT,
                    action_attempt_id TEXT
                );

                CREATE TABLE IF NOT EXISTS action_attempts (
                    id TEXT PRIMARY KEY,
                    goal_id TEXT,
                    state_snapshot_id TEXT,
                    action_kind TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    prepared_at TEXT NOT NULL,
                    sent_at TEXT,
                    finished_at TEXT,
                    result_json TEXT,
                    error_code TEXT,
                    public_rationale TEXT,
                    policy_decision_id TEXT,
                    correlation_id TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS state_snapshots (
                    id TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    data_hash TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS campaign_runs (
                    id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL REFERENCES goals(id),
                    goal_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    strategy_summary TEXT NOT NULL,
                    active_phase_id TEXT,
                    working_memory_json TEXT NOT NULL,
                    progress_checkpoint_json TEXT NOT NULL,
                    external_blocker_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    terminal_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_campaign_run_per_goal
                    ON campaign_runs(goal_id) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS campaign_runs_status
                    ON campaign_runs(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS campaign_phases (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES campaign_runs(id),
                    parent_phase_id TEXT REFERENCES campaign_phases(id),
                    ordinal INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    success_criteria_json TEXT NOT NULL,
                    abandon_predicates_json TEXT NOT NULL,
                    budget_json TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_failure_json TEXT,
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    updated_at TEXT NOT NULL,
                    terminal_at TEXT
                );
                CREATE INDEX IF NOT EXISTS campaign_phases_run
                    ON campaign_phases(run_id, ordinal, created_at);
                CREATE INDEX IF NOT EXISTS campaign_phases_status
                    ON campaign_phases(run_id, status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS phase_attempts (
                    id TEXT PRIMARY KEY,
                    phase_id TEXT NOT NULL REFERENCES campaign_phases(id),
                    action_attempt_id TEXT,
                    semantic_action TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    expected_effect_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    verification_json TEXT,
                    created_at TEXT NOT NULL,
                    terminal_at TEXT
                );
                CREATE INDEX IF NOT EXISTS phase_attempts_signature
                    ON phase_attempts(phase_id, signature, created_at DESC);
                CREATE INDEX IF NOT EXISTS phase_attempts_status
                    ON phase_attempts(phase_id, status, created_at DESC);

                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    sink TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (sink, event_id)
                );

                CREATE TABLE IF NOT EXISTS runtime_kv (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                (timestamp(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)",
                (timestamp(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(3, ?)",
                (timestamp(),),
            )

    @staticmethod
    def _loads(value: str | None, default: Any = None) -> Any:
        if value is None:
            return default
        return json.loads(value)

    def _goal_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "version": row["version"],
            "title": row["title"],
            "objective": row["objective"],
            "success_criteria": self._loads(row["success_criteria_json"], []),
            "constraints": self._loads(row["constraints_json"], {}),
            "priority": row["priority"],
            "status": row["status"],
            "source": self._loads(row["source_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "activated_at": row["activated_at"],
            "terminal_at": row["terminal_at"],
            "blocked_reason": row["blocked_reason"],
            "completion": self._loads(row["completion_json"], {}),
            "retry_of_goal_id": row["retry_of_goal_id"],
        }

    def _lesson_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "goal_id": row["goal_id"],
            "goal_family": row["goal_family"],
            "tactic_key": row["tactic_key"],
            "classification": row["classification"],
            "scope": row["scope"],
            "confidence": row["confidence"],
            "status": row["status"],
            "summary": row["summary"],
            "failed_state": self._loads(row["failed_state_json"], {}),
            "evidence_event_ids": self._loads(row["evidence_event_ids_json"], []),
            "retry_when": self._loads(row["retry_when_json"], {}),
            "suggested_goals": self._loads(row["suggested_goals_json"], []),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "unlocked_at": row["unlocked_at"],
            "resolved_at": row["resolved_at"],
            "resolution_goal_id": row["resolution_goal_id"],
        }

    def _event_in_tx(
        self,
        connection: sqlite3.Connection,
        kind: str,
        summary: str,
        *,
        severity: str = "info",
        interesting: bool = False,
        occurred_at: str | None = None,
        character: dict[str, Any] | None = None,
        goal_id: str | None = None,
        location: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        policy_decision_id: str | None = None,
    ) -> dict[str, Any]:
        event_id = uuid7()
        recorded = timestamp()
        occurred = occurred_at or recorded
        cursor = connection.execute(
            """
            INSERT INTO events(
                id, occurred_at, recorded_at, kind, severity, interesting,
                character_json, goal_id, location_json, summary, data_json,
                correlation_id, causation_id, policy_decision_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                occurred,
                recorded,
                kind,
                severity,
                int(interesting),
                canonical_json(character) if character else None,
                goal_id,
                canonical_json(location) if location else None,
                summary,
                canonical_json(data or {}),
                correlation_id,
                causation_id,
                policy_decision_id,
            ),
        ).lastrowid
        return {
            "cursor": cursor,
            "id": event_id,
            "occurred_at": occurred,
            "recorded_at": recorded,
            "kind": kind,
            "severity": severity,
            "interesting": interesting,
            "character": character,
            "goal_id": goal_id,
            "location": location,
            "summary": summary,
            "data": data or {},
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "policy_decision_id": policy_decision_id,
        }

    def emit_event(self, kind: str, summary: str, **kwargs: Any) -> dict[str, Any]:
        with self.transaction() as connection:
            return self._event_in_tx(connection, kind, summary, **kwargs)

    def events(
        self,
        *,
        after_cursor: int = 0,
        limit: int = 50,
        interesting_only: bool = False,
        kinds: list[str] | None = None,
        goal_id: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        clauses = ["cursor > ?"]
        params: list[Any] = [int(after_cursor)]
        if interesting_only:
            clauses.append("interesting = 1")
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kinds)
        if goal_id is not None:
            clauses.append("goal_id = ?")
            params.append(str(goal_id))
        params.append(limit + 1)
        rows = self._connect().execute(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY cursor ASC LIMIT ?",
            params,
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [self._event_from_row(row) for row in rows]
        return {
            "events": items,
            "next_cursor": items[-1]["cursor"] if items else int(after_cursor),
            "has_more": has_more,
        }

    def current_event_cursor(self) -> int:
        row = self._connect().execute("SELECT COALESCE(MAX(cursor), 0) AS cursor FROM events").fetchone()
        return int(row["cursor"] if row is not None else 0)

    def goal_event_anchor(self, goal_id: str) -> int | None:
        row = self._connect().execute(
            "SELECT MIN(cursor) AS cursor FROM events WHERE goal_id=? AND kind='goal.submitted'",
            (str(goal_id),),
        ).fetchone()
        if row is None or row["cursor"] is None:
            return None
        return int(row["cursor"])

    def upgrade_legacy_pvp_goal_criteria(self) -> list[dict[str, Any]]:
        """Replace the old uncorrelated engagement+property pair in live goals.

        ``pvp.phase.completed`` is emitted only after a server-accepted swing,
        target disappearance, and a completed loot sweep, so one correlated
        event is a more truthful verifier than two generic events.
        """

        upgraded: list[dict[str, Any]] = []
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM goals WHERE status IN ('queued','active','paused','blocked')"
            ).fetchall()
            for row in rows:
                criteria = self._loads(row["success_criteria_json"], [])
                if not isinstance(criteria, list):
                    continue
                engagement_indexes = [
                    index
                    for index, item in enumerate(criteria)
                    if isinstance(item, dict)
                    and item.get("kind") == "event_occurred"
                    and item.get("event_kind") == "pvp.engagement.completed"
                ]
                property_indexes = [
                    index
                    for index, item in enumerate(criteria)
                    if isinstance(item, dict)
                    and item.get("kind") == "event_occurred"
                    and item.get("event_kind") == "property.transaction"
                ]
                if not engagement_indexes or not property_indexes:
                    continue
                anchor_row = connection.execute(
                    "SELECT MIN(cursor) AS cursor FROM events WHERE goal_id=? AND kind='goal.submitted'",
                    (row["id"],),
                ).fetchone()
                anchor = int(anchor_row["cursor"]) if anchor_row and anchor_row["cursor"] is not None else int(
                    connection.execute("SELECT COALESCE(MAX(cursor), 0) AS cursor FROM events").fetchone()["cursor"]
                )
                replace_at = min([*engagement_indexes, *property_indexes])
                removed = set([*engagement_indexes, *property_indexes])
                rebuilt: list[dict[str, Any]] = []
                for index, item in enumerate(criteria):
                    if index == replace_at:
                        rebuilt.append(
                            {
                                "id": "pvp-phase-completed",
                                "kind": "event_occurred",
                                "event_kind": "pvp.phase.completed",
                                "after_cursor": anchor,
                            }
                        )
                    if index not in removed and isinstance(item, dict):
                        rebuilt.append(item)
                now = timestamp()
                version = int(row["version"]) + 1
                connection.execute(
                    "UPDATE goals SET success_criteria_json=?, completion_json=?, version=?, updated_at=? WHERE id=?",
                    (
                        canonical_json(rebuilt),
                        canonical_json({"percent_estimate": 0, "summary": "criteria upgraded", "evidence_event_ids": []}),
                        version,
                        now,
                        row["id"],
                    ),
                )
                self._event_in_tx(
                    connection,
                    "goal.criteria.upgraded",
                    f"Upgraded legacy PvP evidence criteria: {row['title']}",
                    severity="notice",
                    interesting=True,
                    goal_id=row["id"],
                    data={
                        "from_event_kinds": ["pvp.engagement.completed", "property.transaction"],
                        "to_event_kind": "pvp.phase.completed",
                        "after_cursor": anchor,
                    },
                )
                goal = self._goal_from_row(
                    connection.execute("SELECT * FROM goals WHERE id=?", (row["id"],)).fetchone()
                )
                if goal:
                    upgraded.append(goal)
        return upgraded

    def upgrade_legacy_raza_exit_goal_criteria(self) -> list[dict[str, Any]]:
        """Replace manual Raza-graduation checks with durable client evidence.

        Early goal drafts used ``operator_confirmed`` for "leave Raza" because
        the generic criterion language had no negative region predicate.  The
        ordinary-client ``leave_raza`` adapter now provides an exact, goal-
        scoped completion event, so retaining a manual check would leave an
        otherwise autonomous tutorial goal permanently half complete.
        """

        upgraded: list[dict[str, Any]] = []
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM goals WHERE status IN ('queued','active','paused','blocked')"
            ).fetchall()
            for row in rows:
                goal_text = f"{row['title']} {row['objective']}".casefold()
                if "raza" not in goal_text or not any(
                    marker in goal_text
                    for marker in ("leave raza", "left raza", "out of raza", "outside raza")
                ):
                    continue
                criteria = self._loads(row["success_criteria_json"], [])
                if not isinstance(criteria, list):
                    continue
                replace_indexes = [
                    index
                    for index, item in enumerate(criteria)
                    if isinstance(item, dict)
                    and item.get("kind") == "operator_confirmed"
                    and "raza"
                    in str(item.get("id") or "").casefold().replace("-", "_")
                ]
                if not replace_indexes:
                    continue
                anchor_row = connection.execute(
                    "SELECT MIN(cursor) AS cursor FROM events WHERE goal_id=? AND kind='goal.submitted'",
                    (row["id"],),
                ).fetchone()
                anchor = (
                    int(anchor_row["cursor"])
                    if anchor_row and anchor_row["cursor"] is not None
                    else int(
                        connection.execute(
                            "SELECT COALESCE(MAX(cursor), 0) AS cursor FROM events"
                        ).fetchone()["cursor"]
                    )
                )
                replace = set(replace_indexes)
                rebuilt: list[dict[str, Any]] = []
                for index, item in enumerate(criteria):
                    if index in replace:
                        rebuilt.append(
                            {
                                "id": item.get("id") or "left_raza",
                                "kind": "event_occurred",
                                "event_kind": "raza.left",
                                "after_cursor": anchor,
                            }
                        )
                    elif isinstance(item, dict):
                        rebuilt.append(item)
                now = timestamp()
                version = int(row["version"]) + 1
                connection.execute(
                    "UPDATE goals SET success_criteria_json=?, completion_json=?, version=?, updated_at=? WHERE id=?",
                    (
                        canonical_json(rebuilt),
                        canonical_json(
                            {
                                "percent_estimate": 0,
                                "summary": "Raza exit criterion upgraded",
                                "evidence_event_ids": [],
                            }
                        ),
                        version,
                        now,
                        row["id"],
                    ),
                )
                self._event_in_tx(
                    connection,
                    "goal.criteria.upgraded",
                    f"Upgraded Raza exit evidence criterion: {row['title']}",
                    severity="notice",
                    interesting=True,
                    goal_id=row["id"],
                    data={
                        "from_kind": "operator_confirmed",
                        "to_kind": "event_occurred",
                        "event_kind": "raza.left",
                        "after_cursor": anchor,
                    },
                )
                goal = self._goal_from_row(
                    connection.execute(
                        "SELECT * FROM goals WHERE id=?", (row["id"],)
                    ).fetchone()
                )
                if goal is not None:
                    upgraded.append(goal)
        return upgraded

    def latest_events(
        self,
        *,
        limit: int = 50,
        interesting_only: bool = False,
        kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        clauses: list[str] = []
        params: list[Any] = []
        if interesting_only:
            clauses.append("interesting = 1")
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kinds)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = self._connect().execute(
            f"SELECT * FROM events {where} ORDER BY cursor DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._event_from_row(row) for row in reversed(rows)]

    def _event_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "cursor": row["cursor"],
            "id": row["id"],
            "occurred_at": row["occurred_at"],
            "recorded_at": row["recorded_at"],
            "kind": row["kind"],
            "severity": row["severity"],
            "interesting": bool(row["interesting"]),
            "character": self._loads(row["character_json"]),
            "goal_id": row["goal_id"],
            "location": self._loads(row["location_json"]),
            "summary": row["summary"],
            "data": self._loads(row["data_json"], {}),
            "correlation_id": row["correlation_id"],
            "causation_id": row["causation_id"],
            "policy_decision_id": row["policy_decision_id"],
            "redaction": {"applied": True, "fields_removed": []},
        }

    def _idempotent_result(
        self, connection: sqlite3.Connection, request_id: str, operation: str, body: dict[str, Any]
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT operation, request_hash, response_json FROM idempotency WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        digest = json_hash(body)
        if row["operation"] != operation or row["request_hash"] != digest:
            raise IdempotencyConflict(f"request_id {request_id} was already used for different input")
        return self._loads(row["response_json"], {})

    def _save_idempotent(
        self,
        connection: sqlite3.Connection,
        request_id: str,
        operation: str,
        body: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO idempotency(request_id, operation, request_hash, response_json, created_at) VALUES(?,?,?,?,?)",
            (request_id, operation, json_hash(body), canonical_json(response), timestamp()),
        )

    @staticmethod
    def _reject_unknown(payload: dict[str, Any], allowed: set[str], operation: str) -> None:
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unknown {operation} field(s): {', '.join(sorted(unknown))}")

    @staticmethod
    def _validate_goal(payload: dict[str, Any]) -> dict[str, Any]:
        objective_value = payload.get("objective")
        if not isinstance(objective_value, str):
            raise ValueError("objective must be a string")
        objective = objective_value.strip()
        if not objective:
            raise ValueError("objective is required")
        if len(objective) > 4000:
            raise ValueError("objective must be at most 4000 characters")
        criteria = payload.get("success_criteria")
        if not isinstance(criteria, list) or not criteria:
            raise ValueError("success_criteria must contain at least one typed criterion")
        if len(criteria) > 20 or any(not isinstance(item, dict) or not item.get("kind") for item in criteria):
            raise ValueError("success_criteria must contain 1-20 criterion objects with kind")
        ids: set[str] = set()
        for index, item in enumerate(criteria):
            kind = item["kind"]
            if not isinstance(kind, str):
                raise ValueError(f"success criterion {index + 1} kind must be a string")
            if kind not in CRITERION_KINDS:
                supported = ", ".join(CRITERION_KINDS)
                raise ValueError(f"unsupported success criterion kind: {kind}. Supported kinds: {supported}")
            unknown = set(item) - CRITERION_FIELDS_BY_KIND[kind]
            if unknown:
                allowed = ", ".join(sorted(CRITERION_FIELDS_BY_KIND[kind]))
                raise ValueError(
                    f"unknown {kind} criterion field(s): {', '.join(sorted(unknown))}. Allowed fields: {allowed}"
                )
            raw_id = item.get("id")
            if raw_id is not None and (not isinstance(raw_id, str) or not raw_id.strip()):
                raise ValueError(f"success criterion {index + 1} id must be a non-empty string")
            criterion_id = raw_id.strip() if isinstance(raw_id, str) else f"criterion_{index + 1}"
            if criterion_id in ids:
                raise ValueError(f"duplicate success criterion id: {criterion_id}")
            ids.add(criterion_id)
            operator = item.get("operator", ">=")
            if kind in {"numeric_threshold", "numeric_delta"} and operator not in {">=", ">", "<=", "<", "=="}:
                raise ValueError(f"{kind}.operator must be one of >=, >, <=, <, ==")
            if kind == "state_equals":
                if not isinstance(item.get("path"), str) or not item["path"].strip() or "value" not in item:
                    raise ValueError("state_equals requires non-empty path and value")
            elif kind in {"numeric_threshold", "numeric_delta"}:
                if not isinstance(item.get("metric"), str) or not item["metric"].strip():
                    raise ValueError(f"{kind} requires a non-empty metric")
                if not isinstance(item.get("value"), (int, float)) or isinstance(item.get("value"), bool):
                    raise ValueError(f"{kind}.value must be a number")
                if kind == "numeric_delta" and (
                    not isinstance(item.get("baseline"), (int, float)) or isinstance(item.get("baseline"), bool)
                ):
                    raise ValueError("numeric_delta.baseline must be a number")
            elif kind == "inventory_contains":
                if not isinstance(item.get("item"), str) or not item["item"].strip():
                    raise ValueError("inventory_contains requires a non-empty item")
                count = item.get("count", 1)
                if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                    raise ValueError("inventory_contains.count must be an integer of at least 1")
            elif kind == "location_reached":
                names = [item.get("location"), item.get("room")]
                has_name = any(isinstance(value, str) and value.strip() for value in names)
                room_id = item.get("room_id")
                if not has_name and not (isinstance(room_id, (str, int)) and not isinstance(room_id, bool)):
                    raise ValueError("location_reached requires location, room, or room_id")
            elif kind == "event_occurred":
                if not isinstance(item.get("event_kind"), str) or not item["event_kind"].strip():
                    raise ValueError("event_occurred requires a non-empty event_kind")
                if item["event_kind"] not in GOAL_EVENT_KINDS:
                    raise ValueError(
                        "unsupported event_occurred.event_kind: "
                        f"{item['event_kind']}. Supported goal event kinds: "
                        + ", ".join(GOAL_EVENT_KINDS)
                    )
                after = item.get("after_cursor", 0)
                if not isinstance(after, int) or isinstance(after, bool) or after < 0:
                    raise ValueError("event_occurred.after_cursor must be a non-negative integer")
            elif kind in {"composite_all", "composite_any"}:
                references = item.get("criteria", item.get("criterion_ids"))
                if not isinstance(references, list) or not references or any(
                    not isinstance(reference, str) or not reference for reference in references
                ):
                    raise ValueError(f"{kind} requires a non-empty array of criterion ids")
        priority_value = payload.get("priority", 50)
        if not isinstance(priority_value, int) or isinstance(priority_value, bool):
            raise ValueError("priority must be an integer")
        priority = priority_value
        if not 0 <= priority <= 100:
            raise ValueError("priority must be 0-100")
        title_value = payload.get("title")
        if title_value is not None and not isinstance(title_value, str):
            raise ValueError("title must be a string")
        title = (title_value or objective[:117]).strip()
        if not 1 <= len(title) <= 120:
            raise ValueError("title must be 1-120 characters")
        constraints = payload.get("constraints", {})
        if not isinstance(constraints, dict):
            raise ValueError("constraints must be an object")
        unknown_constraints = set(constraints) - GOAL_CONSTRAINT_FIELDS
        if unknown_constraints:
            allowed = ", ".join(sorted(GOAL_CONSTRAINT_FIELDS))
            raise ValueError(
                f"unknown constraint field(s): {', '.join(sorted(unknown_constraints))}. Allowed fields: {allowed}"
            )
        for field in {"avoid_death", "bank_before_hazard"}:
            if field in constraints and not isinstance(constraints[field], bool):
                raise ValueError(f"constraints.{field} must be a boolean")
        purchase_plan = constraints.get("purchase_plan")
        if purchase_plan is not None:
            if not isinstance(purchase_plan, dict):
                raise ValueError("constraints.purchase_plan must be an object")
            allowed_purchase_fields = {
                "offering_kind",
                "item",
                "merchant_class",
                "room_id",
                "maximum_price",
            }
            unknown_purchase_fields = set(purchase_plan) - allowed_purchase_fields
            if unknown_purchase_fields:
                raise ValueError(
                    "unknown constraints.purchase_plan field(s): "
                    + ", ".join(sorted(unknown_purchase_fields))
                    + ". Allowed fields: "
                    + ", ".join(sorted(allowed_purchase_fields))
                )
            for field in ("item", "merchant_class"):
                value = purchase_plan.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"constraints.purchase_plan.{field} must be a non-empty string")
            offering_kind = purchase_plan.get("offering_kind", "item")
            if offering_kind not in {"item", "skill", "spell"}:
                raise ValueError(
                    "constraints.purchase_plan.offering_kind must be item, skill, or spell"
                )
            room_id = purchase_plan.get("room_id")
            if not isinstance(room_id, int) or isinstance(room_id, bool) or room_id < 1:
                raise ValueError("constraints.purchase_plan.room_id must be a positive integer")
            maximum_price = purchase_plan.get("maximum_price")
            if maximum_price is not None and (
                not isinstance(maximum_price, int)
                or isinstance(maximum_price, bool)
                or maximum_price < 0
            ):
                raise ValueError(
                    "constraints.purchase_plan.maximum_price must be a non-negative integer"
                )
            if offering_kind in {"skill", "spell"} and (
                not isinstance(maximum_price, int)
                or isinstance(maximum_price, bool)
                or maximum_price <= 0
            ):
                raise ValueError(
                    "constraints.purchase_plan.maximum_price must be a positive integer for skill/spell training"
                )
        operator_notes = constraints.get("operator_notes", "")
        if not isinstance(operator_notes, str):
            raise ValueError("constraints.operator_notes must be a string")
        if len(operator_notes) > 4000:
            raise ValueError("constraints.operator_notes must be at most 4000 characters")
        return {
            "title": title,
            "objective": objective,
            "success_criteria": criteria,
            "constraints": constraints,
            "priority": priority,
            "activation": str(payload.get("activation", "queue")),
            "source": {"kind": "higher_level_agent", "actor": "operator_agent"},
        }

    def submit_goal(
        self,
        payload: dict[str, Any],
        *,
        retry_of_goal_id: str | None = None,
        preserve_replaced_active: bool = False,
    ) -> dict[str, Any]:
        self._reject_unknown(
            payload,
            {"request_id", "title", "objective", "success_criteria", "constraints", "priority", "activation"},
            "submit_goal",
        )
        request_id = str(payload.get("request_id", ""))
        if not request_id:
            raise ValueError("request_id is required")
        normalized = self._validate_goal(payload)
        if normalized["activation"] not in {"queue", "replace_active_pause", "replace_active_cancel"}:
            raise ValueError("invalid activation")
        with self.transaction() as connection:
            prior = self._idempotent_result(connection, request_id, "submit_goal", payload)
            if prior is not None:
                return prior
            event_anchor = int(
                connection.execute("SELECT COALESCE(MAX(cursor), 0) AS cursor FROM events").fetchone()["cursor"]
            )
            cursor_changes: list[dict[str, Any]] = []
            anchored_criteria: list[dict[str, Any]] = []
            for index, criterion in enumerate(normalized["success_criteria"]):
                anchored = dict(criterion)
                if anchored.get("kind") == "event_occurred":
                    supplied = anchored.get("after_cursor")
                    anchored["after_cursor"] = event_anchor
                    if supplied != event_anchor:
                        cursor_changes.append(
                            {
                                "criterion_id": anchored.get("id") or f"criterion_{index + 1}",
                                "supplied": supplied,
                                "anchored": event_anchor,
                            }
                        )
                anchored_criteria.append(anchored)
            normalized["success_criteria"] = anchored_criteria
            now = timestamp()
            active = connection.execute("SELECT * FROM goals WHERE status='active'").fetchone()
            if active and normalized["activation"].startswith("replace_active"):
                target = (
                    "paused"
                    if preserve_replaced_active or normalized["activation"].endswith("pause")
                    else "cancelled"
                )
                self._transition_in_tx(connection, active, target, "replaced by new goal", "operator_agent")
            goal_id = uuid7()
            source = dict(normalized["source"])
            source["request_id"] = request_id
            connection.execute(
                """
                INSERT INTO goals(
                    id, version, title, objective, success_criteria_json,
                    constraints_json, priority, status, source_json, created_at,
                    updated_at, completion_json, retry_of_goal_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    goal_id,
                    1,
                    normalized["title"],
                    normalized["objective"],
                    canonical_json(normalized["success_criteria"]),
                    canonical_json(normalized["constraints"]),
                    normalized["priority"],
                    "queued",
                    canonical_json(source),
                    now,
                    now,
                    canonical_json({"percent_estimate": 0, "summary": "queued", "evidence_event_ids": []}),
                    retry_of_goal_id,
                ),
            )
            connection.execute(
                "INSERT INTO goal_transitions VALUES(?,?,?,?,?,?,?,?)",
                (uuid7(), goal_id, None, "queued", "submitted", "operator_agent", now, 1),
            )
            self._event_in_tx(
                connection,
                "goal.submitted",
                f"Goal submitted: {normalized['title']}",
                interesting=True,
                goal_id=goal_id,
                data={"priority": normalized["priority"]},
                correlation_id=request_id,
            )
            self._promote_in_tx(connection)
            row = connection.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
            goal = self._goal_from_row(row)
            position = self._queue_position_in_tx(connection, goal_id)
            response = {
                "goal": goal,
                "queue_position": position,
                "warnings": (
                    [{
                        "code": "EVENT_CURSOR_ANCHORED",
                        "message": "Event criteria were anchored to the controller's current durable cursor.",
                        "changes": cursor_changes,
                    }]
                    if cursor_changes
                    else []
                ),
            }
            if active and normalized["activation"] == "replace_active_cancel" and preserve_replaced_active:
                response["warnings"].append(
                    {
                        "code": "ACTIVE_GOAL_PRESERVED",
                        "message": "The replaced active goal was paused rather than cancelled so fresh work is not abandoned irreversibly.",
                        "goal_id": active["id"],
                    }
                )
            self._save_idempotent(connection, request_id, "submit_goal", payload, response)
            return response

    def idempotent_result(self, request_id: str, operation: str, body: dict[str, Any]) -> dict[str, Any] | None:
        return self._idempotent_result(self._connect(), str(request_id), operation, body)

    def _queue_position_in_tx(self, connection: sqlite3.Connection, goal_id: str) -> int | None:
        rows = connection.execute(
            "SELECT id FROM goals WHERE status='queued' ORDER BY priority DESC, created_at ASC"
        ).fetchall()
        for index, row in enumerate(rows, start=1):
            if row["id"] == goal_id:
                return index
        return None

    def _transition_in_tx(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        target: str,
        reason: str,
        actor: str,
        *,
        blocked_reason: str | None = None,
    ) -> dict[str, Any]:
        if target not in GOAL_STATES:
            raise InvalidTransition(f"unknown goal state {target}")
        source = row["status"]
        allowed = {
            "queued": {"active", "paused", "cancelled"},
            "active": {"paused", "blocked", "succeeded", "failed", "cancelled"},
            "paused": {"queued", "cancelled"},
            "blocked": {"queued", "failed", "cancelled"},
        }
        allowed_targets = set(allowed.get(source, set()))
        # Pausing or blocking stops execution; it does not make an already
        # observable outcome incomplete.  Only the deterministic controller
        # verifier may close inactive work directly.  Human/LLM commands still
        # have to resume a paused goal or use the explicit confirmation path.
        if actor == "controller" and source in {"paused", "blocked"} and target == "succeeded":
            allowed_targets.add("succeeded")
        if target not in allowed_targets:
            raise InvalidTransition(f"cannot transition goal {row['id']} from {source} to {target}")
        now = timestamp()
        version = int(row["version"]) + 1
        activated = row["activated_at"] or (now if target == "active" else None)
        terminal = now if target in TERMINAL_GOAL_STATES else None
        connection.execute(
            """
            UPDATE goals SET version=?, status=?, updated_at=?, activated_at=?,
              terminal_at=?, blocked_reason=? WHERE id=?
            """,
            (version, target, now, activated, terminal, blocked_reason, row["id"]),
        )
        connection.execute(
            "INSERT INTO goal_transitions VALUES(?,?,?,?,?,?,?,?)",
            (uuid7(), row["id"], source, target, reason, actor, now, version),
        )
        interesting = target in {"active", "paused", "blocked", *TERMINAL_GOAL_STATES}
        self._event_in_tx(
            connection,
            f"goal.{target}",
            f"Goal {target}: {row['title']}",
            severity="warning" if target in {"blocked", "failed"} else "notice" if interesting else "info",
            interesting=interesting,
            goal_id=row["id"],
            data={"from": source, "reason": reason, "version": version, "blocked_reason": blocked_reason},
        )
        return self._goal_from_row(connection.execute("SELECT * FROM goals WHERE id=?", (row["id"],)).fetchone()) or {}

    def _promote_in_tx(self, connection: sqlite3.Connection) -> dict[str, Any] | None:
        if connection.execute("SELECT 1 FROM goals WHERE status='active'").fetchone():
            return None
        row = connection.execute(
            "SELECT * FROM goals WHERE status='queued' ORDER BY priority DESC, created_at ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return self._transition_in_tx(connection, row, "active", "scheduler promotion", "controller")

    def promote(self) -> dict[str, Any] | None:
        with self.transaction() as connection:
            return self._promote_in_tx(connection)

    def preempt_for_higher_priority(
        self,
        active_goal_id: str,
        *,
        reason: str,
        phase_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically yield an active goal to strictly higher-priority work.

        The controller calls this only after it has verified a safe campaign
        phase boundary and released any keeper.  Requeueing the interrupted
        goal in the same transaction preserves its campaign/checkpoints and
        guarantees that it is eligible for automatic resumption when the
        higher-priority goal becomes terminal.
        """

        with self.transaction() as connection:
            active = connection.execute(
                "SELECT * FROM goals WHERE id=? AND status='active'",
                (active_goal_id,),
            ).fetchone()
            if active is None:
                return None
            candidate = connection.execute(
                """SELECT * FROM goals
                   WHERE status='queued' AND priority>?
                   ORDER BY priority DESC,created_at ASC LIMIT 1""",
                (int(active["priority"]),),
            ).fetchone()
            if candidate is None:
                return None

            detail = str(reason or "safe campaign boundary reached")[:1000]
            paused = self._transition_in_tx(
                connection,
                active,
                "paused",
                detail,
                "controller",
            )
            paused_row = connection.execute(
                "SELECT * FROM goals WHERE id=?", (active_goal_id,)
            ).fetchone()
            if paused_row is None:
                raise NotFound(active_goal_id)
            requeued = self._transition_in_tx(
                connection,
                paused_row,
                "queued",
                "automatically requeued after cooperative priority preemption",
                "controller",
            )
            promoted = self._promote_in_tx(connection)
            if promoted is None or promoted.get("id") != candidate["id"]:
                raise RuntimeError(
                    "cooperative priority preemption did not promote the selected goal"
                )
            self._event_in_tx(
                connection,
                "goal.priority_preempted",
                (
                    f"Yielded {active['title']} to higher-priority goal "
                    f"{candidate['title']} at a safe campaign boundary"
                ),
                severity="notice",
                interesting=True,
                goal_id=active_goal_id,
                data={
                    "preempted_goal_id": active_goal_id,
                    "preempted_priority": int(active["priority"]),
                    "activated_goal_id": candidate["id"],
                    "activated_priority": int(candidate["priority"]),
                    "phase_id": phase_id,
                    "reason": detail,
                },
            )
            return {
                "preempted_goal": requeued,
                "activated_goal": promoted,
                "phase_id": phase_id,
            }

    def manage_goal(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_unknown(
            payload,
            {"request_id", "goal_id", "expected_version", "action", "priority", "reason", "cause"},
            "manage_goal",
        )
        request_id = str(payload.get("request_id", ""))
        if not request_id:
            raise ValueError("request_id is required")
        goal_id = str(payload.get("goal_id", ""))
        action = str(payload.get("action", ""))
        with self.transaction() as connection:
            prior = self._idempotent_result(connection, request_id, "manage_goal", payload)
            if prior is not None:
                return prior
            confirmation_recorded = False
            row = connection.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
            if row is None:
                raise NotFound(f"goal not found: {goal_id}")
            expected = payload.get("expected_version")
            if expected is not None and int(expected) != int(row["version"]):
                raise ConflictError(f"expected version {expected}, current version {row['version']}")
            reason = str(payload.get("reason") or f"{action} requested by the operator agent")
            if action == "reprioritize":
                priority = int(payload.get("priority"))
                if not 0 <= priority <= 100:
                    raise ValueError("priority must be 0-100")
                version = int(row["version"]) + 1
                connection.execute(
                    "UPDATE goals SET priority=?, version=?, updated_at=? WHERE id=?",
                    (priority, version, timestamp(), goal_id),
                )
                goal = self._goal_from_row(connection.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone())
            else:
                targets = {"pause": "paused", "resume": "queued", "cancel": "cancelled"}
                if action == "confirm_complete":
                    criteria = self._loads(row["success_criteria_json"], [])
                    if not any(item.get("kind") == "operator_confirmed" for item in criteria):
                        raise InvalidTransition(
                            "confirm_complete is valid only for a goal with an operator_confirmed criterion"
                        )
                    observable_ids = {
                        str(item.get("id") or f"criterion_{index + 1}")
                        for index, item in enumerate(criteria)
                        if item.get("kind") != "operator_confirmed"
                    }
                    completion = self._loads(row["completion_json"], {})
                    result_by_id = {
                        str(item.get("id")): bool(item.get("met"))
                        for item in completion.get("criteria", [])
                        if isinstance(item, dict)
                    }
                    unmet_observable = sorted(criterion_id for criterion_id in observable_ids if not result_by_id.get(criterion_id))
                    if unmet_observable:
                        raise InvalidTransition(
                            "observable criteria are not verified: " + ", ".join(unmet_observable)
                        )
                    self._event_in_tx(
                        connection,
                        "goal.operator_confirmed",
                        f"Operator confirmed the goal outcome: {row['title']}",
                        interesting=True,
                        goal_id=goal_id,
                        data={
                            "reason": reason,
                            "terminal_deferred_for_safe_ending": True,
                        },
                    )
                    goal = self._goal_from_row(row)
                    confirmation_recorded = True
                elif action in targets:
                    if action == "resume" and row["status"] == "blocked":
                        # Pre-0.2 campaign state could leave an active phase
                        # attached to a blocked goal. Close that stale run before
                        # requeueing so the retry gets a fresh plan rather than
                        # resuming the exact action that caused the block.
                        self._complete_campaign_run_in_tx(
                            connection, goal_id, status="blocked"
                        )
                    goal = self._transition_in_tx(connection, row, targets[action], reason, "operator_agent")
                else:
                    raise ValueError("action must be pause, resume, cancel, reprioritize, or confirm_complete")
            self._promote_in_tx(connection)
            response = {"goal": goal}
            if confirmation_recorded:
                response["confirmation_recorded"] = True
            self._save_idempotent(connection, request_id, "manage_goal", payload, response)
            return response

    def active_goal(self) -> dict[str, Any] | None:
        row = self._connect().execute("SELECT * FROM goals WHERE status='active'").fetchone()
        return self._goal_from_row(row)

    def goal(self, goal_id: str) -> dict[str, Any] | None:
        return self._goal_from_row(self._connect().execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone())

    def goals(self, statuses: list[str] | None = None) -> list[dict[str, Any]]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = self._connect().execute(
                f"SELECT * FROM goals WHERE status IN ({placeholders}) ORDER BY priority DESC, created_at ASC",
                statuses,
            ).fetchall()
        else:
            rows = self._connect().execute(
                "SELECT * FROM goals ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, priority DESC, created_at DESC"
            ).fetchall()
        return [self._goal_from_row(row) or {} for row in rows]

    def goal_events(
        self,
        goal_id: str,
        *,
        kinds: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["goal_id=?"]
        params: list[Any] = [goal_id]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kinds)
        params.append(max(1, min(int(limit), 500)))
        rows = self._connect().execute(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY cursor DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._event_from_row(row) for row in reversed(rows)]

    def goal_lesson(self, lesson_id: str) -> dict[str, Any] | None:
        row = self._connect().execute("SELECT * FROM goal_lessons WHERE id=?", (lesson_id,)).fetchone()
        return self._lesson_from_row(row)

    def goal_lessons(
        self,
        *,
        statuses: list[str] | None = None,
        goal_family: str | None = None,
        goal_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if goal_family:
            clauses.append("goal_family=?")
            params.append(goal_family)
        if goal_id:
            clauses.append("goal_id=?")
            params.append(goal_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 200)))
        rows = self._connect().execute(
            f"SELECT * FROM goal_lessons{where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._lesson_from_row(row) or {} for row in rows]

    def create_goal_lesson(self, value: dict[str, Any]) -> dict[str, Any]:
        required = {
            "goal_id",
            "goal_family",
            "tactic_key",
            "classification",
            "scope",
            "confidence",
            "summary",
            "failed_state",
            "evidence_event_ids",
            "retry_when",
            "suggested_goals",
        }
        missing = required - set(value)
        if missing:
            raise ValueError(f"goal lesson missing field(s): {', '.join(sorted(missing))}")
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT * FROM goal_lessons WHERE goal_family=? AND tactic_key=?
                   AND classification=? AND status='deferred' ORDER BY created_at DESC LIMIT 1""",
                (value["goal_family"], value["tactic_key"], value["classification"]),
            ).fetchone()
            if existing is not None:
                prior_evidence = self._loads(existing["evidence_event_ids_json"], [])
                evidence = list(dict.fromkeys([*prior_evidence, *value["evidence_event_ids"]]))[-100:]
                connection.execute(
                    "UPDATE goal_lessons SET evidence_event_ids_json=?,updated_at=? WHERE id=?",
                    (canonical_json(evidence), timestamp(), existing["id"]),
                )
                return self._lesson_from_row(connection.execute("SELECT * FROM goal_lessons WHERE id=?", (existing["id"],)).fetchone()) or {}
            lesson_id = uuid7()
            now = timestamp()
            connection.execute(
                """INSERT INTO goal_lessons(
                    id,goal_id,goal_family,tactic_key,classification,scope,confidence,status,summary,
                    failed_state_json,evidence_event_ids_json,retry_when_json,suggested_goals_json,
                    created_at,updated_at,unlocked_at,resolved_at,resolution_goal_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lesson_id,
                    value["goal_id"],
                    value["goal_family"],
                    value["tactic_key"],
                    value["classification"],
                    value["scope"],
                    float(value["confidence"]),
                    "deferred",
                    str(value["summary"])[:1000],
                    canonical_json(value["failed_state"]),
                    canonical_json(value["evidence_event_ids"]),
                    canonical_json(value["retry_when"]),
                    canonical_json(value["suggested_goals"]),
                    now,
                    now,
                    None,
                    None,
                    None,
                ),
            )
            self._event_in_tx(
                connection,
                "goal.lesson.created",
                f"Learned why a goal is deferred: {value['classification']}",
                severity="notice",
                interesting=True,
                goal_id=value["goal_id"],
                data={
                    "lesson_id": lesson_id,
                    "goal_family": value["goal_family"],
                    "classification": value["classification"],
                    "scope": value["scope"],
                    "summary": str(value["summary"])[:500],
                    "retry_when": value["retry_when"],
                    "suggested_goals": value["suggested_goals"],
                    "evidence_event_ids": value["evidence_event_ids"],
                },
            )
            return self._lesson_from_row(connection.execute("SELECT * FROM goal_lessons WHERE id=?", (lesson_id,)).fetchone()) or {}

    def update_goal_lesson(
        self,
        lesson_id: str,
        status: str,
        *,
        resolution_goal_id: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"deferred", "unlocked", "resolved"}:
            raise ValueError("goal lesson status must be deferred, unlocked, or resolved")
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM goal_lessons WHERE id=?", (lesson_id,)).fetchone()
            if row is None:
                raise NotFound(lesson_id)
            if row["status"] == status and (not resolution_goal_id or row["resolution_goal_id"] == resolution_goal_id):
                return self._lesson_from_row(row) or {}
            now = timestamp()
            unlocked = now if status == "unlocked" else None if status == "deferred" else row["unlocked_at"]
            resolved = now if status == "resolved" else None
            connection.execute(
                """UPDATE goal_lessons SET status=?,updated_at=?,unlocked_at=?,resolved_at=?,
                   resolution_goal_id=COALESCE(?,resolution_goal_id) WHERE id=?""",
                (status, now, unlocked, resolved, resolution_goal_id, lesson_id),
            )
            is_tactic = row["scope"] == "tactic"
            kind = (
                ("tactic.retry_unlocked" if is_tactic else "goal.retry_unlocked")
                if status == "unlocked"
                else "goal.lesson.resolved"
                if status == "resolved"
                else "goal.deferred"
            )
            summary = (
                (
                    "Deferred tactic is eligible for a revised retry"
                    if is_tactic
                    else "Deferred goal is eligible for a revised retry"
                )
                if status == "unlocked"
                else "Goal lesson resolved by verified success"
                if status == "resolved"
                else "Goal remains deferred"
            )
            self._event_in_tx(
                connection,
                kind,
                summary,
                severity="notice",
                interesting=True,
                goal_id=row["goal_id"],
                data={"lesson_id": lesson_id, "goal_family": row["goal_family"], "resolution_goal_id": resolution_goal_id, "evidence": evidence or {}},
            )
            return self._lesson_from_row(connection.execute("SELECT * FROM goal_lessons WHERE id=?", (lesson_id,)).fetchone()) or {}

    def mark_retry_started(self, lesson_id: str, retry_goal_id: str) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM goal_lessons WHERE id=?", (lesson_id,)).fetchone()
            if row is None:
                raise NotFound(lesson_id)
            connection.execute(
                "UPDATE goal_lessons SET resolution_goal_id=?,updated_at=? WHERE id=?",
                (retry_goal_id, timestamp(), lesson_id),
            )
            self._event_in_tx(
                connection,
                "goal.retry.started",
                "Started a revised retry after prerequisites changed",
                severity="notice",
                interesting=True,
                goal_id=retry_goal_id,
                data={"lesson_id": lesson_id, "prior_goal_id": row["goal_id"], "goal_family": row["goal_family"]},
            )
            return self._lesson_from_row(connection.execute("SELECT * FROM goal_lessons WHERE id=?", (lesson_id,)).fetchone()) or {}

    def resolve_goal_lessons(self, goal_family: str, resolution_goal_id: str) -> list[dict[str, Any]]:
        lessons = self.goal_lessons(statuses=["deferred", "unlocked"], goal_family=goal_family, limit=200)
        return [
            self.update_goal_lesson(lesson["id"], "resolved", resolution_goal_id=resolution_goal_id)
            for lesson in lessons
        ]

    def set_goal_completion(
        self,
        goal_id: str,
        completion: dict[str, Any],
        *,
        terminal: str | None = None,
        reason: str = "criteria evaluated",
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
            if row is None:
                raise NotFound(goal_id)
            if terminal:
                connection.execute(
                    "UPDATE goals SET completion_json=? WHERE id=?",
                    (canonical_json(completion), goal_id),
                )
                row = connection.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
                result = self._transition_in_tx(connection, row, terminal, reason, "controller")
                self._promote_in_tx(connection)
                return result
            serialized = canonical_json(completion)
            # Criteria are evaluated every controller turn. An unchanged
            # observation is not a goal mutation and must not manufacture a
            # fresh version/timestamp that supervision mistakes for progress.
            if row["completion_json"] == serialized:
                return self._goal_from_row(row) or {}
            previous = self._loads(row["completion_json"], {})
            semantic = self._completion_semantics(completion)
            previous_semantic = self._completion_semantics(previous)
            if semantic == previous_semantic:
                # Keep current verifier detail (for example, the room presently
                # observed) without churning the optimistic-concurrency version.
                connection.execute(
                    "UPDATE goals SET completion_json=? WHERE id=?",
                    (serialized, goal_id),
                )
                return self._goal_from_row(
                    connection.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
                ) or {}
            version = int(row["version"]) + 1
            changed_at = timestamp()
            connection.execute(
                "UPDATE goals SET completion_json=?, version=?, updated_at=? WHERE id=?",
                (serialized, version, changed_at, goal_id),
            )
            return self._goal_from_row(connection.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()) or {}

    @staticmethod
    def _completion_semantics(completion: dict[str, Any]) -> dict[str, Any]:
        criteria = completion.get("criteria", [])
        if not isinstance(criteria, list):
            criteria = []
        criterion_states = [
            {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "met": item.get("met") is True,
            }
            for item in criteria
            if isinstance(item, dict)
        ]
        return {
            "percent_estimate": int(completion.get("percent_estimate", 0) or 0),
            "all_met": completion.get("all_met") is True,
            "met_count": sum(1 for item in criterion_states if item["met"]),
            "criteria": criterion_states,
        }

    def _complete_campaign_run_in_tx(
        self,
        connection: sqlite3.Connection,
        goal_id: str,
        *,
        status: str,
    ) -> str | None:
        if status not in {"succeeded", "blocked", "cancelled"}:
            raise ValueError("invalid campaign run terminal status")
        run = connection.execute(
            "SELECT id FROM campaign_runs WHERE goal_id=? AND status='active' "
            "ORDER BY created_at DESC LIMIT 1",
            (goal_id,),
        ).fetchone()
        if run is None:
            return None
        now = timestamp()
        terminal_phase_status = "succeeded" if status == "succeeded" else "superseded"
        connection.execute(
            """UPDATE campaign_phases SET status=?,updated_at=?,terminal_at=?
               WHERE run_id=? AND status IN ('active','paused')""",
            (terminal_phase_status, now, now, run["id"]),
        )
        connection.execute(
            """UPDATE campaign_runs SET status=?,active_phase_id=NULL,
               updated_at=?,terminal_at=? WHERE id=?""",
            (status, now, now, run["id"]),
        )
        return str(run["id"])

    def block_goal(self, goal_id: str, *, reason: str, blocked_reason: str) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
            if row is None:
                raise NotFound(goal_id)
            result = self._transition_in_tx(
                connection,
                row,
                "blocked",
                reason,
                "controller",
                blocked_reason=blocked_reason,
            )
            # Goal execution and campaign execution are one lifecycle. Leaving
            # an active internal phase attached to a blocked strategic goal
            # makes supervision claim both "blocked" and "active" and prevents
            # a clean campaign run when the goal is deliberately retried.
            self._complete_campaign_run_in_tx(connection, goal_id, status="blocked")
            self._promote_in_tx(connection)
            return result

    def requeue_repaired_blocker(
        self, goal_id: str, *, blocked_reason: str, reason: str
    ) -> dict[str, Any] | None:
        """Requeue a goal only when its exact controller blocker was invalidated."""

        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if (
                row is None
                or row["status"] != "blocked"
                or row["blocked_reason"] != blocked_reason
            ):
                return self._goal_from_row(row)
            repaired = self._transition_in_tx(
                connection, row, "queued", reason, "controller"
            )
            promoted = self._promote_in_tx(connection)
            return promoted if promoted and promoted.get("id") == goal_id else repaired

    def create_proposal(self, draft: dict[str, Any], reason: str, expected_value: str = "", risk_summary: str = "") -> dict[str, Any]:
        normalized = self._validate_goal({**draft, "activation": "queue"})
        now = timestamp()
        proposal_id = uuid7()
        goal_draft = {key: normalized[key] for key in ("title", "objective", "success_criteria", "constraints", "priority")}
        title_key = " ".join(goal_draft["title"].split()).casefold()
        existing_id: str | None = None
        with self.transaction() as connection:
            for row in connection.execute(
                "SELECT id, goal_draft_json FROM proposals WHERE status='pending' ORDER BY created_at ASC"
            ).fetchall():
                pending = self._loads(row["goal_draft_json"], {})
                pending_key = " ".join(str(pending.get("title", "")).split()).casefold()
                if pending_key == title_key:
                    existing_id = row["id"]
                    break
            if existing_id is None:
                connection.execute(
                    "INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (proposal_id, 1, "pending", reason, expected_value, canonical_json(goal_draft), risk_summary, now, now, None, None),
                )
                self._event_in_tx(
                    connection,
                    "proposal.created",
                    f"Bot proposed goal: {goal_draft['title']}",
                    severity="notice",
                    interesting=True,
                    data={"proposal_id": proposal_id, "reason": reason},
                )
        return self.proposal(existing_id or proposal_id) or {}

    def proposal(self, proposal_id: str) -> dict[str, Any] | None:
        row = self._connect().execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        return self._proposal_from_row(row) if row else None

    def _proposal_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "version": row["version"], "status": row["status"],
            "reason": row["reason"], "expected_value": row["expected_value"],
            "goal_draft": self._loads(row["goal_draft_json"], {}), "risk_summary": row["risk_summary"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "expires_at": row["expires_at"], "resulting_goal_id": row["resulting_goal_id"],
        }

    def proposals(self, status: str | None = "pending") -> list[dict[str, Any]]:
        if status:
            rows = self._connect().execute("SELECT * FROM proposals WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
        else:
            rows = self._connect().execute("SELECT * FROM proposals ORDER BY created_at DESC").fetchall()
        return [self._proposal_from_row(row) for row in rows]

    def decide_proposal(self, payload: dict[str, Any], *, retry_of_goal_id: str | None = None) -> dict[str, Any]:
        self._reject_unknown(payload, {"request_id", "proposal_id", "action", "reason"}, "proposal decision")
        request_id = str(payload.get("request_id", ""))
        proposal_id = str(payload.get("proposal_id", ""))
        action = str(payload.get("action", ""))
        if action not in {"accept", "reject"}:
            raise ValueError("proposal action must be accept or reject")
        if not request_id:
            raise ValueError("request_id is required")
        with self.transaction() as connection:
            prior = self._idempotent_result(connection, request_id, "decide_proposal", payload)
            if prior is not None:
                return prior
            row = connection.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise NotFound(proposal_id)
            if row["status"] != "pending":
                raise InvalidTransition(f"proposal is already {row['status']}")
            now = timestamp()
            resulting_goal: dict[str, Any] | None = None
            if action == "accept":
                draft = self._loads(row["goal_draft_json"], {})
                event_anchor = int(
                    connection.execute("SELECT COALESCE(MAX(cursor), 0) AS cursor FROM events").fetchone()["cursor"]
                )
                draft["success_criteria"] = [
                    {
                        **criterion,
                        **({"after_cursor": event_anchor} if criterion.get("kind") == "event_occurred" else {}),
                    }
                    for criterion in draft.get("success_criteria", [])
                    if isinstance(criterion, dict)
                ]
                goal_id = uuid7()
                connection.execute(
                    """INSERT INTO goals(id,version,title,objective,success_criteria_json,constraints_json,priority,status,source_json,created_at,updated_at,completion_json,retry_of_goal_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (goal_id, 1, draft["title"], draft["objective"], canonical_json(draft["success_criteria"]), canonical_json(draft.get("constraints", {})), int(draft.get("priority", 50)), "queued", canonical_json({"kind": "controller_proposal", "proposal_id": proposal_id, "request_id": request_id}), now, now, canonical_json({"percent_estimate": 0, "summary": "queued", "evidence_event_ids": []}), retry_of_goal_id),
                )
                connection.execute("INSERT INTO goal_transitions VALUES(?,?,?,?,?,?,?,?)", (uuid7(), goal_id, None, "queued", "proposal accepted", "operator_agent", now, 1))
                connection.execute("UPDATE proposals SET status='accepted', version=version+1, updated_at=?, resulting_goal_id=? WHERE id=?", (now, goal_id, proposal_id))
                self._promote_in_tx(connection)
                resulting_goal = self._goal_from_row(connection.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone())
            else:
                connection.execute("UPDATE proposals SET status='rejected', version=version+1, updated_at=? WHERE id=?", (now, proposal_id))
            reason = str(payload.get("reason", "")).strip()
            draft = self._loads(row["goal_draft_json"], {})
            self._event_in_tx(
                connection,
                f"proposal.{action}ed" if action == "accept" else "proposal.rejected",
                f"Proposal {action}ed by control request: {draft.get('title', row['id'])}",
                interesting=True,
                data={
                    "proposal_id": proposal_id,
                    "proposal_title": draft.get("title"),
                    "resulting_goal_id": resulting_goal and resulting_goal["id"],
                    "reason": reason or None,
                },
            )
            response = {"proposal": self._proposal_from_row(connection.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()), "goal": resulting_goal}
            self._save_idempotent(connection, request_id, "decide_proposal", payload, response)
            return response

    def persona(self) -> dict[str, Any]:
        row = self._connect().execute("SELECT * FROM persona_versions ORDER BY version DESC LIMIT 1").fetchone()
        if row is None:
            return {"version": 0, "persona": None}
        return {"version": row["version"], **self._loads(row["persona_json"], {})}

    def set_persona(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_unknown(payload, {"request_id", "expected_version", "persona"}, "persona update")
        request_id = str(payload.get("request_id", ""))
        persona = payload.get("persona")
        if not request_id or not isinstance(persona, dict):
            raise ValueError("request_id and persona object are required")
        unknown = set(persona) - PERSONA_FIELDS
        if unknown:
            allowed = ", ".join(sorted(PERSONA_FIELDS))
            raise ValueError(f"unknown persona field(s): {', '.join(sorted(unknown))}. Allowed fields: {allowed}")
        name_value = persona.get("name")
        if not isinstance(name_value, str):
            raise ValueError("persona.name must be a string")
        name = name_value.strip()
        if not name or len(name) > 120:
            raise ValueError("persona.name must be 1-120 characters")
        for field in PERSONA_STRING_FIELDS - {"name"}:
            if field in persona and not isinstance(persona[field], str):
                raise ValueError(f"persona.{field} must be a string")
        for field in PERSONA_LIST_FIELDS:
            if field in persona and (
                not isinstance(persona[field], list)
                or any(not isinstance(item, str) for item in persona[field])
            ):
                raise ValueError(f"persona.{field} must be an array of strings")
        maximum_value = persona.get("max_reply_characters", 360)
        if not isinstance(maximum_value, int) or isinstance(maximum_value, bool):
            raise ValueError("persona.max_reply_characters must be an integer")
        maximum = maximum_value
        if not 1 <= maximum <= 1000:
            raise ValueError("persona.max_reply_characters must be 1-1000")
        persona = {**persona, "name": name, "max_reply_characters": maximum}
        with self.transaction() as connection:
            prior = self._idempotent_result(connection, request_id, "set_persona", payload)
            if prior is not None:
                return prior
            current = connection.execute("SELECT version FROM persona_versions ORDER BY version DESC LIMIT 1").fetchone()
            current_version = int(current["version"]) if current else 0
            expected = payload.get("expected_version")
            if expected is not None and int(expected) != current_version:
                raise ConflictError(f"expected version {expected}, current version {current_version}")
            created = timestamp()
            version = connection.execute(
                "INSERT INTO persona_versions(persona_json,created_at,created_by) VALUES(?,?,?)",
                (canonical_json(persona), created, "operator"),
            ).lastrowid
            response = {"version": version, **persona, "created_at": created, "created_by": "operator"}
            self._event_in_tx(connection, "persona.updated", f"Conversation persona updated to version {version}", interesting=True, data={"version": version})
            self._save_idempotent(connection, request_id, "set_persona", payload, response)
            return response

    def create_action_attempt(
        self,
        goal_id: str | None,
        snapshot_id: str | None,
        kind: str,
        arguments: dict[str, Any],
        rationale: str,
        policy_decision_id: str,
        correlation_id: str,
    ) -> str:
        attempt_id = uuid7()
        self._connect().execute(
            """INSERT INTO action_attempts(id,goal_id,state_snapshot_id,action_kind,arguments_json,state,prepared_at,public_rationale,policy_decision_id,correlation_id)
               VALUES(?,?,?,?,?,'prepared',?,?,?,?)""",
            (attempt_id, goal_id, snapshot_id, kind, canonical_json(arguments), timestamp(), rationale, policy_decision_id, correlation_id),
        )
        return attempt_id

    def record_snapshot(self, snapshot: dict[str, Any]) -> str:
        snapshot_id = str(snapshot.get("id") or uuid7())
        safe = {**snapshot, "id": snapshot_id}
        self._connect().execute(
            "INSERT OR IGNORE INTO state_snapshots(id,observed_at,data_json,data_hash) VALUES(?,?,?,?)",
            (
                snapshot_id,
                str(snapshot.get("observed_at") or timestamp()),
                canonical_json(safe),
                json_hash(safe),
            ),
        )
        return snapshot_id

    def update_action_attempt(self, attempt_id: str, state: str, *, result: Any = None, error_code: str | None = None) -> None:
        sent_at = timestamp() if state == "sent" else None
        finished_at = timestamp() if state in {"succeeded", "failed", "unknown"} else None
        self._connect().execute(
            """UPDATE action_attempts SET state=?, sent_at=COALESCE(?,sent_at), finished_at=COALESCE(?,finished_at),
               result_json=COALESCE(?,result_json), error_code=? WHERE id=?""",
            (state, sent_at, finished_at, canonical_json(result) if result is not None else None, error_code, attempt_id),
        )

    def record_consequence(self, assessment: dict[str, Any]) -> dict[str, Any]:
        assessment_id = assessment.get("id") or uuid7()
        now = timestamp()
        with self.transaction() as connection:
            event = self._event_in_tx(
                connection,
                "consequence.assessed",
                str(assessment.get("summary") or f"Consequential action assessed: {assessment['action_class']}"),
                severity="info",
                interesting=True,
                goal_id=assessment.get("goal_id"),
                data={**assessment, "assessment_id": assessment_id, "notify": bool(assessment.get("notify", True))},
                policy_decision_id=assessment.get("policy_decision_id"),
            )
            connection.execute(
                """INSERT INTO consequence_assessments(id,status,action_class,target_json,expected_effects_json,goal_rationale,
                   safer_alternatives_json,guidance,decision,recorded_at,pre_action_event_id,outcome_event_id,action_attempt_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (assessment_id, "assessed", assessment["action_class"], canonical_json(assessment.get("target", {})), canonical_json(assessment.get("expected_effects", {})), str(assessment.get("goal_rationale", "")), canonical_json(assessment.get("safer_alternatives", [])), str(assessment.get("guidance", "")), str(assessment.get("decision", "allow_with_caution")), now, event["id"], None, assessment.get("action_attempt_id")),
            )
        return {"id": assessment_id, "pre_action_event_id": event["id"], **assessment}

    def complete_consequence(self, assessment_id: str, *, outcome: dict[str, Any], succeeded: bool) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT c.*, a.goal_id, a.policy_decision_id, a.correlation_id
                   FROM consequence_assessments c
                   LEFT JOIN action_attempts a ON a.id=c.action_attempt_id
                   WHERE c.id=?""",
                (assessment_id,),
            ).fetchone()
            if row is None:
                raise NotFound(assessment_id)
            event = self._event_in_tx(
                connection,
                "consequence.executed" if succeeded else "consequence.failed",
                f"Consequential action {'completed' if succeeded else 'failed'}: {row['action_class']}",
                severity="info" if succeeded else "warning",
                interesting=True,
                goal_id=row["goal_id"],
                data={"assessment_id": assessment_id, "outcome": outcome, "notify": True},
                correlation_id=row["correlation_id"],
                policy_decision_id=row["policy_decision_id"],
            )
            connection.execute("UPDATE consequence_assessments SET status=?, outcome_event_id=? WHERE id=?", ("executed" if succeeded else "failed", event["id"], assessment_id))
            return event

    def recent_consequences(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._connect().execute("SELECT * FROM consequence_assessments ORDER BY recorded_at DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": row["id"], "status": row["status"], "action_class": row["action_class"], "target": self._loads(row["target_json"], {}), "expected_effects": self._loads(row["expected_effects_json"], {}), "goal_rationale": row["goal_rationale"], "guidance": row["guidance"], "decision": row["decision"], "recorded_at": row["recorded_at"], "pre_action_event_id": row["pre_action_event_id"], "outcome_event_id": row["outcome_event_id"]} for row in rows]

    def _campaign_run_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "goal_id": row["goal_id"],
            "goal_version": row["goal_version"],
            "status": row["status"],
            "strategy_summary": row["strategy_summary"],
            "active_phase_id": row["active_phase_id"],
            "working_memory": self._loads(row["working_memory_json"], {}),
            "progress_checkpoint": self._loads(row["progress_checkpoint_json"], {}),
            "external_blocker": self._loads(row["external_blocker_json"], None),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "terminal_at": row["terminal_at"],
        }

    def _campaign_phase_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "parent_phase_id": row["parent_phase_id"],
            "ordinal": row["ordinal"],
            "kind": row["kind"],
            "objective": row["objective"],
            "status": row["status"],
            "success_criteria": self._loads(row["success_criteria_json"], []),
            "abandon_predicates": self._loads(row["abandon_predicates_json"], []),
            "budget": self._loads(row["budget_json"], {}),
            "context": self._loads(row["context_json"], {}),
            "rationale": row["rationale"],
            "attempt_count": row["attempt_count"],
            "last_failure": self._loads(row["last_failure_json"], None),
            "created_at": row["created_at"],
            "activated_at": row["activated_at"],
            "updated_at": row["updated_at"],
            "terminal_at": row["terminal_at"],
        }

    def _phase_attempt_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "phase_id": row["phase_id"],
            "action_attempt_id": row["action_attempt_id"],
            "semantic_action": row["semantic_action"],
            "signature": row["signature"],
            "expected_effect": self._loads(row["expected_effect_json"], {}),
            "status": row["status"],
            "result": self._loads(row["result_json"], None),
            "verification": self._loads(row["verification_json"], None),
            "created_at": row["created_at"],
            "terminal_at": row["terminal_at"],
        }

    def ensure_campaign_run(self, goal: dict[str, Any]) -> dict[str, Any]:
        """Return the restart-safe execution run for one active strategic goal."""
        goal_id = str(goal.get("id") or "")
        if not goal_id:
            raise ValueError("campaign run requires a goal id")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM campaign_runs WHERE goal_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
                (goal_id,),
            ).fetchone()
            if row is not None:
                if int(row["goal_version"]) != int(goal.get("version", row["goal_version"])):
                    connection.execute(
                        "UPDATE campaign_runs SET goal_version=?,updated_at=? WHERE id=?",
                        (int(goal.get("version", row["goal_version"])), timestamp(), row["id"]),
                    )
                    row = connection.execute(
                        "SELECT * FROM campaign_runs WHERE id=?", (row["id"],)
                    ).fetchone()
                return self._campaign_run_from_row(row) or {}
            now = timestamp()
            run_id = uuid7()
            strategy = str(goal.get("objective") or goal.get("title") or "")[:2000]
            connection.execute(
                """INSERT INTO campaign_runs(
                    id,goal_id,goal_version,status,strategy_summary,active_phase_id,
                    working_memory_json,progress_checkpoint_json,external_blocker_json,
                    created_at,updated_at,terminal_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    goal_id,
                    int(goal.get("version", 1)),
                    "active",
                    strategy,
                    None,
                    canonical_json({"verified_facts": [], "rejected_hypotheses": [], "open_questions": []}),
                    canonical_json({"goal_completion": goal.get("completion", {})}),
                    None,
                    now,
                    now,
                    None,
                ),
            )
            self._event_in_tx(
                connection,
                "campaign.started",
                f"Started long-horizon execution for: {goal.get('title') or goal_id}",
                interesting=True,
                goal_id=goal_id,
                data={"run_id": run_id, "strategy_summary": strategy},
            )
            return self._campaign_run_from_row(
                connection.execute("SELECT * FROM campaign_runs WHERE id=?", (run_id,)).fetchone()
            ) or {}

    def campaign_run(self, goal_id: str, *, include_terminal: bool = False) -> dict[str, Any] | None:
        clause = "" if include_terminal else " AND status='active'"
        row = self._connect().execute(
            f"SELECT * FROM campaign_runs WHERE goal_id=?{clause} ORDER BY created_at DESC LIMIT 1",
            (goal_id,),
        ).fetchone()
        return self._campaign_run_from_row(row)

    def active_campaign_phase(self, run_id: str) -> dict[str, Any] | None:
        row = self._connect().execute(
            "SELECT * FROM campaign_phases WHERE run_id=? AND status='active' ORDER BY ordinal DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return self._campaign_phase_from_row(row)

    def campaign_phases(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            "SELECT * FROM campaign_phases WHERE run_id=? ORDER BY ordinal,created_at",
            (run_id,),
        ).fetchall()
        return [self._campaign_phase_from_row(row) or {} for row in rows]

    def create_campaign_phase(
        self,
        run: dict[str, Any],
        phase: dict[str, Any],
        *,
        mode: str = "replace",
    ) -> dict[str, Any]:
        if mode not in {"replace", "push", "start"}:
            raise ValueError("campaign phase mode must be replace, push, or start")
        kind = str(phase.get("kind") or "").strip()
        objective = str(phase.get("objective") or "").strip()
        if not kind or not objective:
            raise ValueError("campaign phase requires non-empty kind and objective")
        criteria = phase.get("success_criteria", [])
        if not isinstance(criteria, list):
            raise ValueError("campaign phase success_criteria must be an array")
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM campaign_phases WHERE run_id=? AND status='active' ORDER BY ordinal DESC LIMIT 1",
                (run["id"],),
            ).fetchone()
            parent_phase_id = None
            if current is not None:
                if mode == "push":
                    parent_phase_id = current["id"]
                    connection.execute(
                        "UPDATE campaign_phases SET status='paused',updated_at=? WHERE id=?",
                        (timestamp(), current["id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE campaign_phases SET status='superseded',updated_at=?,terminal_at=? WHERE id=?",
                        (timestamp(), timestamp(), current["id"]),
                    )
            ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(ordinal),0)+1 AS next FROM campaign_phases WHERE run_id=?",
                    (run["id"],),
                ).fetchone()["next"]
            )
            phase_id = uuid7()
            now = timestamp()
            connection.execute(
                """INSERT INTO campaign_phases(
                    id,run_id,parent_phase_id,ordinal,kind,objective,status,
                    success_criteria_json,abandon_predicates_json,budget_json,
                    context_json,rationale,attempt_count,last_failure_json,
                    created_at,activated_at,updated_at,terminal_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    phase_id,
                    run["id"],
                    parent_phase_id,
                    ordinal,
                    kind,
                    objective,
                    "active",
                    canonical_json(criteria),
                    canonical_json(phase.get("abandon_predicates", [])),
                    canonical_json(phase.get("budget", {"max_actions": 24, "max_minutes": 45})),
                    canonical_json(phase.get("context", {})),
                    str(phase.get("rationale") or "")[:2000],
                    0,
                    None,
                    now,
                    now,
                    now,
                    None,
                ),
            )
            connection.execute(
                "UPDATE campaign_runs SET active_phase_id=?,external_blocker_json=NULL,updated_at=? WHERE id=?",
                (phase_id, now, run["id"]),
            )
            self._event_in_tx(
                connection,
                "campaign.phase.started",
                f"Started internal phase: {objective[:180]}",
                interesting=False,
                goal_id=run["goal_id"],
                data={
                    "run_id": run["id"],
                    "phase_id": phase_id,
                    "parent_phase_id": parent_phase_id,
                    "kind": kind,
                    "mode": mode,
                },
            )
            return self._campaign_phase_from_row(
                connection.execute("SELECT * FROM campaign_phases WHERE id=?", (phase_id,)).fetchone()
            ) or {}

    def transition_campaign_phase(
        self,
        phase_id: str,
        status: str,
        *,
        reason: str = "",
        resume_parent: bool = False,
    ) -> dict[str, Any]:
        if status not in {"succeeded", "failed", "superseded", "paused"}:
            raise ValueError("invalid campaign phase transition")
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM campaign_phases WHERE id=?", (phase_id,)).fetchone()
            if row is None:
                raise NotFound(f"campaign phase not found: {phase_id}")
            now = timestamp()
            connection.execute(
                "UPDATE campaign_phases SET status=?,last_failure_json=CASE WHEN ?='failed' THEN ? ELSE last_failure_json END,updated_at=?,terminal_at=? WHERE id=?",
                (
                    status,
                    status,
                    canonical_json({"reason": reason, "recorded_at": now}) if status == "failed" else None,
                    now,
                    now if status in {"succeeded", "failed", "superseded"} else None,
                    phase_id,
                ),
            )
            active_phase_id = None
            if resume_parent and row["parent_phase_id"]:
                connection.execute(
                    "UPDATE campaign_phases SET status='active',updated_at=? WHERE id=? AND status='paused'",
                    (now, row["parent_phase_id"]),
                )
                active_phase_id = row["parent_phase_id"]
            connection.execute(
                "UPDATE campaign_runs SET active_phase_id=?,updated_at=? WHERE id=?",
                (active_phase_id, now, row["run_id"]),
            )
            run = connection.execute("SELECT * FROM campaign_runs WHERE id=?", (row["run_id"],)).fetchone()
            self._event_in_tx(
                connection,
                f"campaign.phase.{status}",
                f"Internal phase {status}: {row['objective'][:180]}",
                interesting=False,
                severity="warning" if status == "failed" else "info",
                goal_id=run["goal_id"] if run else None,
                data={
                    "run_id": row["run_id"],
                    "phase_id": phase_id,
                    "parent_phase_id": row["parent_phase_id"],
                    "kind": row["kind"],
                    "reason": reason[:1000],
                    "resumed_parent": active_phase_id,
                },
            )
            return self._campaign_phase_from_row(
                connection.execute("SELECT * FROM campaign_phases WHERE id=?", (phase_id,)).fetchone()
            ) or {}

    def update_campaign_phase_guardrails(
        self,
        phase_id: str,
        *,
        abandon_predicates: list[dict[str, Any]],
        context: dict[str, Any],
        reason: str,
        success_criteria: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Persist a safe normalization of optional model-authored phase guards."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM campaign_phases WHERE id=?", (phase_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"campaign phase not found: {phase_id}")
            now = timestamp()
            connection.execute(
                """UPDATE campaign_phases
                   SET success_criteria_json=?,abandon_predicates_json=?,context_json=?,updated_at=?
                   WHERE id=?""",
                (
                    canonical_json(success_criteria)
                    if success_criteria is not None
                    else row["success_criteria_json"],
                    canonical_json(abandon_predicates),
                    canonical_json(context),
                    now,
                    phase_id,
                ),
            )
            run = connection.execute(
                "SELECT * FROM campaign_runs WHERE id=?", (row["run_id"],)
            ).fetchone()
            self._event_in_tx(
                connection,
                "campaign.phase.guardrails.normalized",
                "Normalized optional internal phase criteria and guardrails",
                interesting=False,
                goal_id=run["goal_id"] if run else None,
                data={
                    "run_id": row["run_id"],
                    "phase_id": phase_id,
                    "remaining_predicates": len(abandon_predicates),
                    "success_criteria_normalized": success_criteria is not None,
                    "reason": reason[:1000],
                },
            )
            return self._campaign_phase_from_row(
                connection.execute(
                    "SELECT * FROM campaign_phases WHERE id=?", (phase_id,)
                ).fetchone()
            ) or {}

    def create_phase_attempt(
        self,
        phase_id: str,
        *,
        semantic_action: str,
        signature: str,
        expected_effect: Any,
        action_attempt_id: str | None = None,
    ) -> str:
        attempt_id = uuid7()
        now = timestamp()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO phase_attempts(
                    id,phase_id,action_attempt_id,semantic_action,signature,
                    expected_effect_json,status,result_json,verification_json,
                    created_at,terminal_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    phase_id,
                    action_attempt_id,
                    semantic_action,
                    signature,
                    canonical_json(expected_effect or {}),
                    "prepared",
                    None,
                    None,
                    now,
                    None,
                ),
            )
            connection.execute(
                "UPDATE campaign_phases SET attempt_count=attempt_count+1,updated_at=? WHERE id=?",
                (now, phase_id),
            )
        return attempt_id

    def update_phase_attempt(
        self,
        attempt_id: str,
        status: str,
        *,
        action_attempt_id: str | None = None,
        result: Any = None,
        verification: Any = None,
    ) -> dict[str, Any]:
        if status not in {"prepared", "sent", "succeeded", "failed", "unknown", "suppressed"}:
            raise ValueError("invalid phase attempt status")
        terminal = timestamp() if status in {"succeeded", "failed", "unknown", "suppressed"} else None
        self._connect().execute(
            """UPDATE phase_attempts SET status=?,action_attempt_id=COALESCE(?,action_attempt_id),
               result_json=COALESCE(?,result_json),verification_json=COALESCE(?,verification_json),
               terminal_at=COALESCE(?,terminal_at) WHERE id=?""",
            (
                status,
                action_attempt_id,
                canonical_json(result) if result is not None else None,
                canonical_json(verification) if verification is not None else None,
                terminal,
                attempt_id,
            ),
        )
        row = self._connect().execute("SELECT * FROM phase_attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise NotFound(f"phase attempt not found: {attempt_id}")
        return self._phase_attempt_from_row(row) or {}

    def phase_attempts(
        self,
        phase_id: str,
        *,
        signature: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if signature is None:
            rows = self._connect().execute(
                "SELECT * FROM phase_attempts WHERE phase_id=? ORDER BY created_at DESC LIMIT ?",
                (phase_id, max(1, min(int(limit), 500))),
            ).fetchall()
        else:
            rows = self._connect().execute(
                "SELECT * FROM phase_attempts WHERE phase_id=? AND signature=? ORDER BY created_at DESC LIMIT ?",
                (phase_id, signature, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._phase_attempt_from_row(row) or {} for row in reversed(rows)]

    def update_campaign_memory(
        self,
        run_id: str,
        *,
        working_memory: dict[str, Any] | None = None,
        progress_checkpoint: dict[str, Any] | None = None,
        external_blocker: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = self._connect().execute("SELECT * FROM campaign_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFound(f"campaign run not found: {run_id}")
        self._connect().execute(
            """UPDATE campaign_runs SET working_memory_json=COALESCE(?,working_memory_json),
               progress_checkpoint_json=COALESCE(?,progress_checkpoint_json),
               external_blocker_json=COALESCE(?,external_blocker_json),updated_at=? WHERE id=?""",
            (
                canonical_json(working_memory) if working_memory is not None else None,
                canonical_json(progress_checkpoint) if progress_checkpoint is not None else None,
                canonical_json(external_blocker) if external_blocker is not None else None,
                timestamp(),
                run_id,
            ),
        )
        return self._campaign_run_from_row(
            self._connect().execute("SELECT * FROM campaign_runs WHERE id=?", (run_id,)).fetchone()
        ) or {}

    def clear_campaign_external_blocker(self, run_id: str) -> dict[str, Any]:
        """Clear a campaign blocker after its supporting evidence is repaired."""

        row = self._connect().execute(
            "SELECT * FROM campaign_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"campaign run not found: {run_id}")
        self._connect().execute(
            "UPDATE campaign_runs SET external_blocker_json=NULL,updated_at=? WHERE id=?",
            (timestamp(), run_id),
        )
        return self._campaign_run_from_row(
            self._connect().execute(
                "SELECT * FROM campaign_runs WHERE id=?", (run_id,)
            ).fetchone()
        ) or {}

    def complete_campaign_run(self, goal_id: str, *, status: str = "succeeded") -> dict[str, Any] | None:
        with self.transaction() as connection:
            run_id = self._complete_campaign_run_in_tx(
                connection, goal_id, status=status
            )
        if run_id is None:
            return None
        return self.campaign_run(goal_id, include_terminal=True)

    def get_runtime(self, key: str, default: Any = None) -> Any:
        row = self._connect().execute("SELECT value_json FROM runtime_kv WHERE key=?", (key,)).fetchone()
        return self._loads(row["value_json"], default) if row else default

    def set_runtime(self, key: str, value: Any) -> None:
        self._connect().execute(
            """INSERT INTO runtime_kv(key,value_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
            (key, canonical_json(value), timestamp()),
        )

    def delivery_status(self, sink: str, event_id: str) -> str | None:
        row = self._connect().execute("SELECT status FROM notification_deliveries WHERE sink=? AND event_id=?", (sink, event_id)).fetchone()
        return row["status"] if row else None

    def pending_delivery_events(self, sink: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            """SELECT e.* FROM events e
               LEFT JOIN notification_deliveries d ON d.sink=? AND d.event_id=e.id
               WHERE e.interesting=1 AND (d.status IS NULL OR d.status NOT IN ('delivered','suppressed'))
               ORDER BY e.cursor ASC LIMIT ?""",
            (sink, max(1, min(int(limit), 500))),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def record_delivery(self, sink: str, event_id: str, status: str, error: str | None = None) -> None:
        self._connect().execute(
            """INSERT INTO notification_deliveries(sink,event_id,status,attempts,last_error,updated_at)
               VALUES(?,?,?,1,?,?) ON CONFLICT(sink,event_id) DO UPDATE SET
               status=excluded.status,attempts=notification_deliveries.attempts+1,last_error=excluded.last_error,updated_at=excluded.updated_at""",
            (sink, event_id, status, error, timestamp()),
        )

    def quick_check(self) -> str:
        return str(self._connect().execute("PRAGMA quick_check").fetchone()[0])

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
