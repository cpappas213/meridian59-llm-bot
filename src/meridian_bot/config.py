from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils import expand_path


SURVIVAL_RECOVERY_THRESHOLD = 0.95
SURVIVAL_FLEE_FLOOR = 0.75


def survival_keeper_thresholds(
    configured_rest: float,
    configured_critical: float,
) -> tuple[float, float]:
    """Return the recovery target and emergency withdraw floor for survive mode."""

    rest = max(SURVIVAL_RECOVERY_THRESHOLD, float(configured_rest))
    flee = max(SURVIVAL_FLEE_FLOOR, float(configured_critical))
    return rest, min(rest, flee)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a table")
    return value


def _unknown(data: dict[str, Any], allowed: set[str], section: str) -> None:
    extra = set(data) - allowed
    if extra:
        raise ValueError(f"unknown {section} setting(s): {', '.join(sorted(extra))}")


@dataclass(frozen=True)
class DeploymentConfig:
    instance_id: str
    timezone: str
    data_dir: Path
    log_dir: Path
    run_dir: Path
    secret_file: Path


@dataclass(frozen=True)
class GameConfig:
    host: str
    port: int
    agent: str
    account_alias: str
    character: str | None
    autojoin: bool


@dataclass(frozen=True)
class HarnessConfig:
    root: Path
    expected_revision: str
    control_url: str
    dashboard_port: int
    lifecycle: str
    node_executable: str
    state_file: Path


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    name: str
    planner_timeout_seconds: int
    responder_timeout_seconds: int
    max_output_tokens: int
    temperature: float
    chat_temperature: float = 0.7
    json_mode: bool = True
    disable_thinking: bool = False
    auth_mode: str = "auto"
    tactical_prompt_protocol: str = "progressive"


@dataclass(frozen=True)
class ControllerConfig:
    control_bind: str
    control_port: int
    dashboard_bind: str
    dashboard_port: int
    active_cadence_seconds: float
    idle_cadence_seconds: float
    error_backoff_max_seconds: float
    fallback_mode: str
    conversation_enabled: bool
    social_poll_seconds: float = 1.0
    proactive_greetings_enabled: bool = False
    greeting_cooldown_seconds: float = 20 * 60
    greetings_per_minute: int = 20
    conversation_history_turns: int = 8
    conversation_window_messages: int = 12
    conversation_window_seconds: float = 30 * 60
    minimum_goal_commitment_seconds: int = 60 * 60
    minimum_stall_seconds: int = 5 * 60


@dataclass(frozen=True)
class PolicyConfig:
    avoid_death: bool
    bank_before_hazard: bool
    rest_health_fraction: float
    critical_health_fraction: float
    carried_currency_bank_threshold: int
    protected_item_value_threshold: int
    protected_item_names: tuple[str, ...]
    consequential_action_guidance: str
    automated_help_pleas: bool = False


@dataclass(frozen=True)
class NotificationConfig:
    windows_enabled: bool
    minimum_severity: str
    obsidian_enabled: bool
    obsidian_vault_path: Path | None
    obsidian_project_relative_path: str
    obsidian_index_filename: str
    obsidian_journal_subdirectory: str
    obsidian_assessment_batch_size: int = 20


@dataclass(frozen=True)
class LearningConfig:
    enabled: bool = True
    no_progress_budget: int = 6
    repeated_tactic_budget: int = 3
    failure_evidence_window_seconds: int = 15 * 60
    wait_budget: int = 10
    survival_interrupt_budget: int = 3
    world_retry_cooldown_seconds: int = 30 * 60
    generic_retry_cooldown_seconds: int = 60 * 60


@dataclass(frozen=True)
class OnboardingConfig:
    """Controls non-mutating persona/selected-character verification."""

    enabled: bool = True
    # Deprecated compatibility fields accepted from existing installations.
    # They are deliberately inert: no configuration can grant the controller
    # character-lifecycle authority.
    create_from_persona: bool = True
    preserve_existing_character: bool = True


@dataclass(frozen=True)
class BotConfig:
    source_path: Path
    deployment: DeploymentConfig
    game: GameConfig
    harness: HarnessConfig
    model: ModelConfig
    controller: ControllerConfig
    policy: PolicyConfig
    notifications: NotificationConfig
    secrets: dict[str, str] = field(repr=False)
    learning: LearningConfig = field(default_factory=LearningConfig)
    onboarding: OnboardingConfig = field(default_factory=OnboardingConfig)

    @property
    def database_path(self) -> Path:
        return self.deployment.data_dir / "controller.sqlite3"

    @property
    def control_token(self) -> str:
        return self.secrets.get("M59_BOT_CONTROL_TOKEN", "")

    @classmethod
    def load(cls, path: str | Path) -> "BotConfig":
        source = Path(path).resolve()
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
        _unknown(
            raw,
            {
                "deployment",
                "game",
                "harness",
                "model",
                "controller",
                "policy",
                "notifications",
                "learning",
                "onboarding",
            },
            "top-level",
        )
        base = source.parent

        dep = _section(raw, "deployment")
        _unknown(dep, {"instance_id", "timezone", "data_dir", "log_dir", "run_dir", "secret_file"}, "deployment")
        deployment = DeploymentConfig(
            instance_id=str(dep.get("instance_id", "primary")),
            timezone=str(dep.get("timezone", "UTC")),
            data_dir=expand_path(str(dep.get("data_dir", "runtime/data")), base=base),
            log_dir=expand_path(str(dep.get("log_dir", "runtime/logs")), base=base),
            run_dir=expand_path(str(dep.get("run_dir", "runtime/run")), base=base),
            secret_file=expand_path(str(dep.get("secret_file", "secrets.env")), base=base),
        )
        secret_values = load_env_file(deployment.secret_file)

        game_raw = _section(raw, "game")
        _unknown(game_raw, {"host", "port", "agent", "account_alias", "character", "autojoin"}, "game")
        game = GameConfig(
            host=str(game_raw.get("host", "127.0.0.1")),
            port=int(game_raw.get("port", 5959)),
            agent=str(game_raw.get("agent", "primary")),
            account_alias=str(game_raw.get("account_alias", "primary")),
            character=str(game_raw.get("character") or "") or None,
            autojoin=bool(game_raw.get("autojoin", True)),
        )

        harness_raw = _section(raw, "harness")
        _unknown(harness_raw, {"root", "expected_revision", "control_url", "dashboard_port", "lifecycle", "node_executable", "state_file"}, "harness")
        harness = HarnessConfig(
            root=expand_path(str(harness_raw.get("root", "../vendor/m59-harness")), base=base),
            expected_revision=str(harness_raw.get("expected_revision", "")),
            control_url=str(harness_raw.get("control_url", "http://127.0.0.1:8901")).rstrip("/"),
            dashboard_port=int(harness_raw.get("dashboard_port", 8902)),
            lifecycle=str(harness_raw.get("lifecycle", "external")),
            node_executable=str(harness_raw.get("node_executable", "node")),
            state_file=expand_path(str(harness_raw.get("state_file", deployment.data_dir / "harness-fleet-state.json")), base=base),
        )
        if harness.lifecycle not in {"external", "controller_managed"}:
            raise ValueError("harness.lifecycle must be external or controller_managed")

        model_raw = _section(raw, "model")
        _unknown(
            model_raw,
            {
                "base_url",
                "name",
                "planner_timeout_seconds",
                "responder_timeout_seconds",
                "max_output_tokens",
                "temperature",
                "chat_temperature",
                "json_mode",
                "disable_thinking",
                "auth_mode",
                "tactical_prompt_protocol",
            },
            "model",
        )
        model = ModelConfig(
            base_url=str(model_raw.get("base_url", "http://127.0.0.1:8000/v1")).rstrip("/"),
            name=str(model_raw.get("name", "")),
            planner_timeout_seconds=int(model_raw.get("planner_timeout_seconds", 120)),
            responder_timeout_seconds=int(model_raw.get("responder_timeout_seconds", 45)),
            max_output_tokens=int(model_raw.get("max_output_tokens", 4096)),
            temperature=float(model_raw.get("temperature", 0.2)),
            chat_temperature=float(model_raw.get("chat_temperature", 0.7)),
            json_mode=bool(model_raw.get("json_mode", True)),
            disable_thinking=bool(model_raw.get("disable_thinking", False)),
            auth_mode=str(model_raw.get("auth_mode", "auto")),
            tactical_prompt_protocol=str(
                model_raw.get("tactical_prompt_protocol", "progressive")
            ),
        )
        if model.auth_mode not in {"auto", "none", "bearer", "anthropic"}:
            raise ValueError(
                "model.auth_mode must be auto, none, bearer, or anthropic"
            )
        if model.tactical_prompt_protocol not in {"legacy", "progressive"}:
            raise ValueError(
                "model.tactical_prompt_protocol must be legacy or progressive"
            )
        if not 0 <= model.temperature <= 2:
            raise ValueError("model.temperature must be between 0 and 2")
        if not 0 <= model.chat_temperature <= 2:
            raise ValueError("model.chat_temperature must be between 0 and 2")

        ctl_raw = _section(raw, "controller")
        _unknown(
            ctl_raw,
            {
                "control_bind",
                "control_port",
                "dashboard_bind",
                "dashboard_port",
                "active_cadence_seconds",
                "idle_cadence_seconds",
                "error_backoff_max_seconds",
                "fallback_mode",
                "conversation_enabled",
                "social_poll_seconds",
                "proactive_greetings_enabled",
                "greeting_cooldown_seconds",
                "greetings_per_minute",
                "conversation_history_turns",
                "conversation_window_messages",
                "conversation_window_seconds",
                "minimum_goal_commitment_seconds",
                "minimum_stall_seconds",
            },
            "controller",
        )
        controller = ControllerConfig(
            control_bind=str(ctl_raw.get("control_bind", "127.0.0.1")),
            control_port=int(ctl_raw.get("control_port", 8903)),
            dashboard_bind=str(ctl_raw.get("dashboard_bind", "0.0.0.0")),
            dashboard_port=int(ctl_raw.get("dashboard_port", 8904)),
            active_cadence_seconds=float(ctl_raw.get("active_cadence_seconds", 3)),
            idle_cadence_seconds=float(ctl_raw.get("idle_cadence_seconds", 30)),
            error_backoff_max_seconds=float(ctl_raw.get("error_backoff_max_seconds", 60)),
            fallback_mode=str(ctl_raw.get("fallback_mode", "survive")),
            conversation_enabled=bool(ctl_raw.get("conversation_enabled", True)),
            social_poll_seconds=float(ctl_raw.get("social_poll_seconds", 1.0)),
            proactive_greetings_enabled=bool(ctl_raw.get("proactive_greetings_enabled", False)),
            greeting_cooldown_seconds=float(ctl_raw.get("greeting_cooldown_seconds", 20 * 60)),
            greetings_per_minute=int(ctl_raw.get("greetings_per_minute", 20)),
            conversation_history_turns=int(ctl_raw.get("conversation_history_turns", 8)),
            conversation_window_messages=int(
                ctl_raw.get("conversation_window_messages", 12)
            ),
            conversation_window_seconds=float(
                ctl_raw.get("conversation_window_seconds", 30 * 60)
            ),
            minimum_goal_commitment_seconds=int(ctl_raw.get("minimum_goal_commitment_seconds", 60 * 60)),
            minimum_stall_seconds=int(ctl_raw.get("minimum_stall_seconds", 5 * 60)),
        )
        if controller.control_bind not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("controller control API must bind to loopback")
        if controller.social_poll_seconds <= 0:
            raise ValueError("controller.social_poll_seconds must be greater than zero")
        if controller.greeting_cooldown_seconds < 0:
            raise ValueError("controller.greeting_cooldown_seconds cannot be negative")
        if not 1 <= controller.greetings_per_minute <= 60:
            raise ValueError("controller.greetings_per_minute must be between 1 and 60")
        if not 1 <= controller.conversation_history_turns <= 20:
            raise ValueError("controller.conversation_history_turns must be between 1 and 20")
        if not 2 <= controller.conversation_window_messages <= 40:
            raise ValueError(
                "controller.conversation_window_messages must be between 2 and 40"
            )
        if controller.conversation_window_seconds <= 0:
            raise ValueError(
                "controller.conversation_window_seconds must be greater than zero"
            )
        if min(controller.minimum_goal_commitment_seconds, controller.minimum_stall_seconds) < 0:
            raise ValueError("controller goal commitment and stall durations cannot be negative")

        policy_raw = _section(raw, "policy")
        _unknown(policy_raw, {"avoid_death", "bank_before_hazard", "rest_health_fraction", "critical_health_fraction", "carried_currency_bank_threshold", "protected_item_value_threshold", "protected_item_names", "consequential_action_guidance", "automated_help_pleas"}, "policy")
        policy = PolicyConfig(
            avoid_death=bool(policy_raw.get("avoid_death", True)),
            bank_before_hazard=bool(policy_raw.get("bank_before_hazard", True)),
            rest_health_fraction=float(policy_raw.get("rest_health_fraction", 0.7)),
            critical_health_fraction=float(policy_raw.get("critical_health_fraction", 0.4)),
            carried_currency_bank_threshold=int(policy_raw.get("carried_currency_bank_threshold", 1)),
            protected_item_value_threshold=int(policy_raw.get("protected_item_value_threshold", 5000)),
            protected_item_names=tuple(str(item) for item in policy_raw.get("protected_item_names", [])),
            consequential_action_guidance=str(policy_raw.get("consequential_action_guidance", "strongly_avoid_unnecessary_loss")),
            automated_help_pleas=bool(policy_raw.get("automated_help_pleas", False)),
        )
        if not 0 < policy.critical_health_fraction <= policy.rest_health_fraction <= 1:
            raise ValueError("health fractions must satisfy 0 < critical <= rest <= 1")

        note_raw = _section(raw, "notifications")
        _unknown(
            note_raw,
            {
                "windows_enabled",
                "minimum_severity",
                "obsidian_enabled",
                "obsidian_vault_path",
                "obsidian_project_relative_path",
                "obsidian_index_filename",
                "obsidian_journal_subdirectory",
                "obsidian_assessment_batch_size",
            },
            "notifications",
        )
        vault_raw = str(note_raw.get("obsidian_vault_path", "") or secret_values.get("M59_OBSIDIAN_VAULT_PATH", ""))
        notifications = NotificationConfig(
            windows_enabled=bool(note_raw.get("windows_enabled", False)),
            minimum_severity=str(note_raw.get("minimum_severity", "notice")),
            obsidian_enabled=bool(note_raw.get("obsidian_enabled", False)),
            obsidian_vault_path=expand_path(vault_raw, base=base) if vault_raw else None,
            obsidian_project_relative_path=str(note_raw.get("obsidian_project_relative_path", "01 Projects/Meridian 59 Bot")),
            obsidian_index_filename=str(note_raw.get("obsidian_index_filename", "Meridian 59 Bot.md")),
            obsidian_journal_subdirectory=str(note_raw.get("obsidian_journal_subdirectory", "Journal")),
            obsidian_assessment_batch_size=int(note_raw.get("obsidian_assessment_batch_size", 20)),
        )
        if not 1 <= notifications.obsidian_assessment_batch_size <= 100:
            raise ValueError("notifications.obsidian_assessment_batch_size must be between 1 and 100")

        learning_raw = _section(raw, "learning")
        _unknown(
            learning_raw,
            {
                "enabled",
                "no_progress_budget",
                "repeated_tactic_budget",
                "failure_evidence_window_seconds",
                "wait_budget",
                "survival_interrupt_budget",
                "world_retry_cooldown_seconds",
                "generic_retry_cooldown_seconds",
            },
            "learning",
        )
        learning = LearningConfig(
            enabled=bool(learning_raw.get("enabled", True)),
            no_progress_budget=int(learning_raw.get("no_progress_budget", 6)),
            repeated_tactic_budget=int(learning_raw.get("repeated_tactic_budget", 3)),
            failure_evidence_window_seconds=int(
                learning_raw.get("failure_evidence_window_seconds", 15 * 60)
            ),
            wait_budget=int(learning_raw.get("wait_budget", 10)),
            survival_interrupt_budget=int(learning_raw.get("survival_interrupt_budget", 3)),
            world_retry_cooldown_seconds=int(learning_raw.get("world_retry_cooldown_seconds", 30 * 60)),
            generic_retry_cooldown_seconds=int(learning_raw.get("generic_retry_cooldown_seconds", 60 * 60)),
        )
        if min(
            learning.no_progress_budget,
            learning.repeated_tactic_budget,
            learning.failure_evidence_window_seconds,
            learning.wait_budget,
            learning.survival_interrupt_budget,
        ) < 1:
            raise ValueError("learning failure budgets and evidence window must be at least 1")
        if min(learning.world_retry_cooldown_seconds, learning.generic_retry_cooldown_seconds) < 0:
            raise ValueError("learning retry cooldowns cannot be negative")

        onboarding_raw = _section(raw, "onboarding")
        _unknown(
            onboarding_raw,
            {
                "enabled",
                "create_from_persona",
                "preserve_existing_character",
            },
            "onboarding",
        )
        onboarding = OnboardingConfig(
            enabled=bool(onboarding_raw.get("enabled", True)),
            create_from_persona=bool(
                onboarding_raw.get("create_from_persona", True)
            ),
            preserve_existing_character=bool(
                onboarding_raw.get("preserve_existing_character", True)
            ),
        )

        for directory in (deployment.data_dir, deployment.log_dir, deployment.run_dir):
            directory.mkdir(parents=True, exist_ok=True)
        if not secret_values.get("M59_BOT_CONTROL_TOKEN"):
            # Persisting this is the installer's responsibility. An ephemeral token
            # keeps a development controller secure rather than opening it silently.
            secret_values["M59_BOT_CONTROL_TOKEN"] = secrets.token_urlsafe(32)

        return cls(
            source,
            deployment,
            game,
            harness,
            model,
            controller,
            policy,
            notifications,
            secret_values,
            learning,
            onboarding,
        )


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    for key in (
        "M59_ACCOUNT_USERNAME",
        "M59_ACCOUNT_PASSWORD",
        "M59_BOT_CONTROL_TOKEN",
        "M59_LLM_API_KEY",
        "M59_VLLM_API_KEY",
        "M59_OBSIDIAN_VAULT_PATH",
    ):
        if key in os.environ:
            values[key] = os.environ[key]
    return values
