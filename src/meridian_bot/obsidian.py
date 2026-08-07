from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import NotificationConfig
from .utils import ensure_under


class ObsidianJournal:
    """An idempotent, human-facing projection of LLM significance assessments."""

    def __init__(self, config: NotificationConfig, timezone_name: str = "UTC"):
        self.config = config
        self.timezone_name = timezone_name
        try:
            self.timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"configured timezone is unavailable: {timezone_name}") from exc

    @property
    def project_dir(self) -> Path:
        if self.config.obsidian_vault_path is None:
            raise ValueError("Obsidian vault path is not configured")
        return ensure_under(
            self.config.obsidian_vault_path,
            self.config.obsidian_vault_path / self.config.obsidian_project_relative_path,
        )

    def local_datetime(self, value: str) -> datetime:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(self.timezone)

    def local_day(self, event: dict[str, Any]) -> str:
        return self.local_datetime(str(event["occurred_at"])).date().isoformat()

    def contains_event(self, event: dict[str, Any]) -> bool:
        shard = self._shard_path(self.local_day(event))
        marker = f"<!-- m59-event:{event['id']} -->"
        return shard.is_file() and marker in shard.read_text(encoding="utf-8")

    def deliver_assessment(
        self,
        assessment: dict[str, Any],
        events: list[dict[str, Any]],
        *,
        model_name: str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        if not events:
            raise ValueError("an Obsidian assessment requires at least one source event")
        days = {self.local_day(event) for event in events}
        if len(days) != 1:
            raise ValueError("an Obsidian assessment cannot span local calendar days")
        day = days.pop()
        project = self.project_dir
        journal = ensure_under(project, project / self.config.obsidian_journal_subdirectory)
        journal.mkdir(parents=True, exist_ok=True)
        shard = self._shard_path(day)
        markers = [f"<!-- m59-event:{event['id']} -->" for event in events]
        latest = max(self.local_datetime(str(event["occurred_at"])) for event in events)
        if shard.is_file():
            current = shard.read_text(encoding="utf-8")
            if all(marker in current for marker in markers):
                self._ensure_index(project, day)
                if context:
                    self.update_executive_summary(context, assessment, occurred_at=latest)
                return False

        earliest = min(self.local_datetime(str(event["occurred_at"])) for event in events)
        headline = self._single_line(str(assessment.get("headline") or "Character update"), 180)
        narrative = self._single_line(str(assessment.get("assessment") or ""), 700)
        significance = self._single_line(str(assessment.get("significance") or ""), 350)
        next_watch = self._single_line(str(assessment.get("next_watch") or ""), 350)
        severity = self._single_line(str(assessment.get("severity") or "notice"), 20)
        if not narrative:
            raise ValueError("LLM assessment did not contain an assessment narrative")

        time_label = self._display_time(latest)
        source_window = self._display_time(earliest)
        if earliest != latest:
            source_window += f" to {self._display_time(latest)}"
        marker_text = "\n".join(markers)
        lines = [
            "",
            marker_text,
            f"## {time_label} — {headline}",
            "",
            narrative,
        ]
        if significance:
            lines.extend(["", f"**Why it matters:** {significance}"])
        if next_watch:
            lines.extend(["", f"**What to watch next:** {next_watch}"])
        lines.extend(
            [
                "",
                f"_LLM assessment · `{self._single_line(model_name, 120)}` · {severity} · "
                f"{len(events)} source event{'s' if len(events) != 1 else ''} from {source_window}_",
                "",
            ]
        )
        if not shard.exists():
            shard.write_text(
                "---\n"
                f"title: \"Meridian 59 Bot Journal — {day}\"\n"
                f"date: {day}\n"
                "type: \"meridian-59-bot-journal\"\n"
                f"timezone: {self.timezone_name}\n"
                "tags: [meridian59-bot, journal]\n"
                "---\n\n"
                f"# Meridian 59 Bot Journal — {day}\n",
                encoding="utf-8",
            )
        with shard.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines))
            handle.flush()
        self._ensure_index(project, day)
        if context:
            self.update_executive_summary(context, assessment, occurred_at=latest)
        return True

    def update_executive_summary(
        self,
        context: dict[str, Any],
        assessment: dict[str, Any] | None = None,
        *,
        occurred_at: datetime | None = None,
    ) -> None:
        """Replace the controller-owned project note with a current campaign view."""
        project = self.project_dir
        project.mkdir(parents=True, exist_ok=True)
        index = ensure_under(project, project / self.config.obsidian_index_filename)
        milestone_at = occurred_at or datetime.now(self.timezone)
        if milestone_at.tzinfo is None:
            milestone_at = milestone_at.replace(tzinfo=self.timezone)
        else:
            milestone_at = milestone_at.astimezone(self.timezone)
        updated_at = datetime.now(self.timezone)

        character = self._single_line(str(context.get("character") or "Unknown"), 120)
        location = self._single_line(str(context.get("location") or "Unknown"), 160)
        vitals = context.get("vitals") if isinstance(context.get("vitals"), dict) else {}
        health = vitals.get("health") if isinstance(vitals.get("health"), dict) else {}
        current_hp = health.get("current", health.get("value"))
        maximum_hp = health.get("max")
        health_text = "Unknown"
        if current_hp is not None and maximum_hp is not None:
            health_text = f"{current_hp}/{maximum_hp} HP"
        elif maximum_hp is not None:
            health_text = f"{maximum_hp} max HP"

        goal = context.get("active_goal") if isinstance(context.get("active_goal"), dict) else None
        if goal is None and isinstance(context.get("current_goal"), dict):
            goal = context["current_goal"]
        if goal:
            goal_title = self._single_line(
                str(goal.get("title") or goal.get("objective") or "Untitled goal"), 220
            )
            completion = goal.get("completion") if isinstance(goal.get("completion"), dict) else {}
            percent = completion.get("percent_estimate")
            summary = self._single_line(str(completion.get("summary") or ""), 280)
            goal_text = f"{goal_title} ({goal.get('status', 'active')})"
            if percent is not None:
                goal_text += f" — {percent}%"
            if summary:
                goal_text += f"; {summary}"
        else:
            goal_text = "No active goal"

        controller = context.get("controller") if isinstance(context.get("controller"), dict) else {}
        dependencies = (
            controller.get("dependencies")
            if isinstance(controller.get("dependencies"), dict)
            else {}
        )
        unhealthy = [
            f"{name}: {value}"
            for name, value in dependencies.items()
            if any(
                marker in str(value).casefold()
                for marker in ("unhealthy", "degraded", "failed", "error", "incompatible")
            )
        ]
        dependency_text = "; ".join(unhealthy) if unhealthy else "No reported dependency failures"
        risk = self._single_line(str(context.get("risk") or "Unknown"), 120)
        liveness = context.get("liveness") if isinstance(context.get("liveness"), dict) else {}
        liveness_state = self._single_line(str(liveness.get("state") or "unknown"), 40)
        suppression = (
            liveness.get("safety_suppression")
            if isinstance(liveness.get("safety_suppression"), dict)
            else None
        )
        if suppression:
            blocker_names = ", ".join(
                str(value) for value in suppression.get("blocker_kinds", [])
            ) or "deterministic safety preflight"
            first_blocked = suppression.get("first_blocked_at")
            try:
                first_blocked = (
                    self._display_time(self.local_datetime(str(first_blocked)))
                    if first_blocked
                    else "an unknown time"
                )
            except (TypeError, ValueError):
                first_blocked = str(first_blocked or "an unknown time")
            liveness_text = (
                f"{liveness_state}; {blocker_names} repeated "
                f"{suppression.get('same_blocker_count', 0)} times since "
                f"{first_blocked}"
            )
        else:
            last_progress = liveness.get("last_verified_progress_at")
            liveness_text = liveness_state
            if last_progress:
                try:
                    last_progress = self._display_time(
                        self.local_datetime(str(last_progress))
                    )
                except (TypeError, ValueError):
                    last_progress = str(last_progress)
                liveness_text += f"; last verified progress {last_progress}"

        assessment = assessment if isinstance(assessment, dict) else {}
        headline = self._single_line(
            str(assessment.get("headline") or "No milestone recorded yet"), 180
        )
        narrative = self._single_line(str(assessment.get("assessment") or ""), 500)
        next_watch = self._single_line(str(assessment.get("next_watch") or ""), 280)

        journal_dir = ensure_under(project, project / self.config.obsidian_journal_subdirectory)
        links: list[str] = []
        if journal_dir.is_dir():
            for shard in sorted(journal_dir.glob("*.md"), reverse=True):
                day = shard.stem
                rel = f"{self.config.obsidian_journal_subdirectory}/{day}"
                links.append(f"- [[{rel}|{day}]]")
        if not links:
            links.append("- No milestone entries yet")

        lines = [
            "---",
            'title: "Meridian 59 Bot"',
            'type: "meridian-59-bot-executive-summary"',
            "tags: [project, meridian59-bot]",
            "---",
            "",
            "# Meridian 59 Bot",
            "",
            "## Current campaign",
            "",
            f"_Current state refreshed {self._display_time(updated_at)}; "
            f"latest milestone occurred {self._display_time(milestone_at)}._",
            "",
            f"- **Character:** {character}",
            f"- **Location:** {location}",
            f"- **Health:** {self._single_line(health_text, 80)}",
            f"- **Goal:** {goal_text}",
            f"- **Risk:** {risk}",
            f"- **Liveness:** {self._single_line(liveness_text, 320)}",
            f"- **System:** {self._single_line(dependency_text, 300)}",
            "",
            "## Latest milestone",
            "",
            f"**{headline}**",
        ]
        if narrative:
            lines.extend(["", narrative])
        if next_watch:
            lines.extend(["", f"**What to watch next:** {next_watch}"])
        lines.extend(["", "## Journal", "", *links, ""])

        temporary = index.with_suffix(index.suffix + ".tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        temporary.replace(index)

    def refresh_executive_summary(self, context: dict[str, Any]) -> None:
        """Rebuild the index from fresh state and the latest existing assessment."""
        journal_dir = ensure_under(
            self.project_dir,
            self.project_dir / self.config.obsidian_journal_subdirectory,
        )
        assessment: dict[str, Any] | None = None
        occurred_at: datetime | None = None
        if journal_dir.is_dir():
            shards = sorted(journal_dir.glob("*.md"), reverse=True)
            for shard in shards:
                parsed = self._latest_assessment_from_shard(shard)
                if parsed is None:
                    continue
                assessment, occurred_at = parsed
                break
        self.update_executive_summary(context, assessment, occurred_at=occurred_at)

    def _latest_assessment_from_shard(
        self, shard: Path
    ) -> tuple[dict[str, Any], datetime | None] | None:
        lines = shard.read_text(encoding="utf-8").splitlines()
        headings = [
            index
            for index, line in enumerate(lines)
            if line.startswith("## ") and " — " in line
        ]
        if not headings:
            return None
        start = headings[-1]
        time_label, headline = lines[start][3:].split(" — ", 1)
        occurred_at: datetime | None = None
        try:
            occurred_at = datetime.strptime(
                time_label.rsplit(" ", 1)[0], "%Y-%m-%d %I:%M:%S %p"
            ).replace(tzinfo=self.timezone)
        except ValueError:
            pass
        narrative_lines: list[str] = []
        narrative_done = False
        next_watch = ""
        severity = "notice"
        for line in lines[start + 1 :]:
            if line.startswith("## ") or line.startswith("<!-- m59-event:"):
                break
            if line.startswith("**What to watch next:**"):
                next_watch = line[len("**What to watch next:**") :].strip()
                continue
            if line.startswith("_LLM assessment"):
                parts = [part.strip() for part in line.strip("_").split("·")]
                if len(parts) >= 3:
                    severity = parts[2]
                continue
            if line.startswith("**"):
                narrative_done = True
                continue
            if line.strip() and not narrative_done:
                narrative_lines.append(line.strip())
            elif narrative_lines:
                narrative_done = True
        return (
            {
                "headline": headline.strip(),
                "assessment": " ".join(narrative_lines),
                "next_watch": next_watch,
                "severity": severity,
            },
            occurred_at,
        )

    def _shard_path(self, day: str) -> Path:
        project = self.project_dir
        journal = ensure_under(project, project / self.config.obsidian_journal_subdirectory)
        return ensure_under(journal, journal / f"{day}.md")

    def _ensure_index(self, project: Path, day: str) -> None:
        project.mkdir(parents=True, exist_ok=True)
        index = ensure_under(project, project / self.config.obsidian_index_filename)
        rel = f"{self.config.obsidian_journal_subdirectory}/{day}"
        link = f"- [[{rel}|{day}]]"
        if not index.exists():
            index.write_text(
                "---\ntags: [project, meridian59-bot]\n---\n\n# Meridian 59 Bot\n\n"
                "LLM assessments of significant bot developments are projected here from the "
                "controller's durable event database.\n\n## Journal\n\n",
                encoding="utf-8",
            )
        current = index.read_text(encoding="utf-8")
        if link not in current:
            with index.open("a", encoding="utf-8", newline="\n") as handle:
                if current and not current.endswith("\n"):
                    handle.write("\n")
                handle.write(link + "\n")

    @staticmethod
    def _display_time(value: datetime) -> str:
        return value.strftime("%Y-%m-%d %I:%M:%S %p %Z").replace(" 0", " ")

    @staticmethod
    def _single_line(value: str, limit: int) -> str:
        safe = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
        safe = re.sub(r"\s+", " ", safe).strip()
        safe = safe.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
        safe = safe.replace("[[", "\\[\\[").replace("![[", "!\\[\\[")
        if len(safe) <= limit:
            return safe
        if limit <= 1:
            return "…"[:limit]
        prefix = safe[: limit - 1].rsplit(" ", 1)[0] or safe[: limit - 1]
        return prefix.rstrip() + "…"
