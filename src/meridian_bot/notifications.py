from __future__ import annotations

import subprocess
import time
from collections import defaultdict
from typing import Any, Callable

from .config import BotConfig
from .obsidian import ObsidianJournal
from .storage import Storage


SEVERITY = {"debug": 0, "info": 1, "notice": 2, "warning": 3, "critical": 4}

# Obsidian is an executive campaign summary, not the interesting-event stream.
# Desktop alerts may still use severity, while the complete durable history stays
# available through SQLite/MCP. Only these sparse outcome milestones reach the
# journal's LLM assessment layer.
JOURNAL_GOAL_KINDS = {
    "goal.active",
    "goal.succeeded",
    "goal.failed",
    "goal.blocked",
    "goal.paused",
}
JOURNAL_PROGRESS_KINDS = {
    "progress.hp_gained",
    "progress.skill_learned",
    "progress.spell_learned",
    "progress.skill_milestone",
    "progress.spell_milestone",
}
JOURNAL_EVENT_KINDS = {
    "character.died",
    "pvp.engagement.completed",
    # The controller emits this only after the same deterministic blocker is
    # repeated three times, so it is an exceptional campaign stall rather than
    # one more raw warning line.
    "planner.stalled",
    # Emitted only after a structured purchase claim repeatedly contradicts
    # fresh ordinary-client merchant/quote evidence (or a verified price cap).
    "planner.preflight.failed",
}


class NotificationDispatcher:
    def __init__(
        self,
        config: BotConfig,
        storage: Storage,
        *,
        assessor: Callable[..., dict[str, Any]] | None = None,
        context_provider: Callable[[], dict[str, Any]] | None = None,
    ):
        self.config = config
        self.storage = storage
        self.journal = ObsidianJournal(config.notifications, config.deployment.timezone)
        self.assessor = assessor
        self.context_provider = context_provider or (lambda: {})
        self._obsidian_failures = 0
        self._obsidian_retry_after = 0.0

    def refresh_executive_summary(self) -> None:
        if (
            self.config.notifications.obsidian_enabled
            and self.config.notifications.obsidian_vault_path is not None
        ):
            self.journal.refresh_executive_summary(self.context_provider())

    def dispatch_pending(self, limit: int = 100) -> dict[str, int]:
        result = {"sent": 0, "failed": 0, "suppressed": 0}
        if self.config.notifications.windows_enabled:
            self._dispatch_windows(limit, result)
        if self.config.notifications.obsidian_enabled and self.config.notifications.obsidian_vault_path:
            if time.monotonic() < self._obsidian_retry_after:
                result["failed"] += 1
            else:
                failures_before = result["failed"]
                self._dispatch_obsidian(limit, result)
                if result["failed"] > failures_before:
                    self._obsidian_failures += 1
                    delay = min(300.0, 2.0 ** min(self._obsidian_failures, 8))
                    self._obsidian_retry_after = time.monotonic() + delay
                else:
                    self._obsidian_failures = 0
                    self._obsidian_retry_after = 0.0
        return result

    def _dispatch_windows(self, limit: int, result: dict[str, int]) -> None:
        for event in self.storage.pending_delivery_events("windows", limit=limit):
            if not self._meets_desktop_threshold(event):
                self.storage.record_delivery("windows", event["id"], "suppressed")
                result["suppressed"] += 1
                continue
            try:
                self._windows_toast(event)
                self.storage.record_delivery("windows", event["id"], "delivered")
                result["sent"] += 1
            except Exception as exc:
                self.storage.record_delivery("windows", event["id"], "failed", str(exc)[:500])
                result["failed"] += 1

    def _dispatch_obsidian(self, limit: int, result: dict[str, int]) -> None:
        pending = self.storage.pending_delivery_events("obsidian", limit=limit)
        unseen: list[dict[str, Any]] = []
        for event in pending:
            try:
                if self.journal.contains_event(event):
                    self.storage.record_delivery("obsidian", event["id"], "delivered")
                    self._remember_journal_milestones([event])
                    result["sent"] += 1
                else:
                    unseen.append(event)
            except Exception as exc:
                self.storage.record_delivery("obsidian", event["id"], "failed", str(exc)[:500])
                result["failed"] += 1

        unseen = self._select_journal_milestones(unseen, result)

        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in unseen:
            try:
                by_day[self.journal.local_day(event)].append(event)
            except Exception as exc:
                self.storage.record_delivery("obsidian", event["id"], "failed", str(exc)[:500])
                result["failed"] += 1

        batch_size = self.config.notifications.obsidian_assessment_batch_size
        for day in sorted(by_day):
            events = by_day[day]
            for offset in range(0, len(events), batch_size):
                self._assess_and_deliver(events[offset : offset + batch_size], result)

    def _assess_and_deliver(self, events: list[dict[str, Any]], result: dict[str, int]) -> None:
        context: dict[str, Any] = {}
        try:
            if self.assessor is None:
                raise RuntimeError("Obsidian LLM assessor is not configured")
            context = self.context_provider()
            assessment = self.assessor(events=events, context=context)
            # The deterministic selector has already established that every
            # source event is an executive milestone. The model explains that
            # milestone; it does not get to silently veto a verified death,
            # goal transition, HP gain, or other allowlisted outcome.
            assessment["significant"] = True
            self.journal.deliver_assessment(
                assessment,
                events,
                model_name=self.config.model.name,
                context=context,
            )
            for event in events:
                self.storage.record_delivery("obsidian", event["id"], "delivered")
                result["sent"] += 1
            self._remember_journal_milestones(events)
        except Exception as exc:
            for event in events:
                self.storage.record_delivery("obsidian", event["id"], "failed", str(exc)[:500])
                result["failed"] += 1
            # A busy model must not leave the current-campaign snapshot stale.
            # Preserve the prior LLM milestone while refreshing live state; the
            # failed source milestone stays queued for a later assessment.
            if context:
                try:
                    self.journal.refresh_executive_summary(context)
                except (OSError, ValueError):
                    pass

    @staticmethod
    def _journal_milestone_key(event: dict[str, Any]) -> str | None:
        kind = str(event.get("kind") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if kind in JOURNAL_GOAL_KINDS:
            goal_id = event.get("goal_id")
            return f"{kind}:{goal_id}" if goal_id else None
        if kind in JOURNAL_PROGRESS_KINDS:
            if kind == "progress.hp_gained":
                return f"{kind}:{data.get('after')}"
            name = str(data.get("name") or "").strip().casefold()
            value = data.get("milestone", data.get("ability"))
            return f"{kind}:{name}:{value}" if name and value is not None else None
        if kind in JOURNAL_EVENT_KINDS:
            return f"{kind}:{event.get('id')}"
        if kind == "property.transaction" and data.get("protected_or_valuable") is True:
            return f"{kind}:{event.get('id')}"
        return None

    @staticmethod
    def _journal_event_score(event: dict[str, Any]) -> int:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        return (
            (10 if isinstance(data.get("completion"), dict) else 0)
            + (5 if data.get("outcome") is not None else 0)
            + len(data)
        )

    def _select_journal_milestones(
        self, events: list[dict[str, Any]], result: dict[str, int]
    ) -> list[dict[str, Any]]:
        remembered_raw = self.storage.get_runtime("obsidian_milestone_keys_v1", [])
        remembered = {
            str(value) for value in remembered_raw if isinstance(value, str)
        } if isinstance(remembered_raw, list) else set()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            key = self._journal_milestone_key(event)
            if key is None or key in remembered:
                self.storage.record_delivery("obsidian", event["id"], "suppressed")
                result["suppressed"] += 1
                continue
            grouped[key].append(event)

        selected: list[dict[str, Any]] = []
        for values in grouped.values():
            chosen = max(values, key=self._journal_event_score)
            selected.append(chosen)
            for duplicate in values:
                if duplicate["id"] == chosen["id"]:
                    continue
                self.storage.record_delivery("obsidian", duplicate["id"], "suppressed")
                result["suppressed"] += 1
        return sorted(selected, key=lambda event: str(event.get("occurred_at") or ""))

    def _remember_journal_milestones(self, events: list[dict[str, Any]]) -> None:
        runtime_key = "obsidian_milestone_keys_v1"
        existing = self.storage.get_runtime(runtime_key, [])
        keys = [str(value) for value in existing if isinstance(value, str)] if isinstance(existing, list) else []
        for event in events:
            key = self._journal_milestone_key(event)
            if key and key not in keys:
                keys.append(key)
        self.storage.set_runtime(runtime_key, keys[-1000:])

    def _meets_desktop_threshold(self, event: dict[str, Any]) -> bool:
        minimum = SEVERITY.get(self.config.notifications.minimum_severity, 2)
        severity = SEVERITY.get(str(event.get("severity")), 1)
        return severity >= minimum or bool(event.get("data", {}).get("notify"))

    @staticmethod
    def _windows_toast(event: dict[str, Any]) -> None:
        # Desktop alerts stay deterministic and immediate. Obsidian is the slower,
        # interpretive LLM assessment channel.
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.BalloonTipTitle=$args[0];$n.BalloonTipText=$args[1];$n.Visible=$true;"
            "$n.ShowBalloonTip(8000);Start-Sleep -Seconds 9;$n.Dispose()"
        )
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                script,
                "Meridian 59 bot",
                str(event.get("summary", "Event"))[:240],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
