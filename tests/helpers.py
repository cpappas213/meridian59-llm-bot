from __future__ import annotations

from pathlib import Path

from meridian_bot.config import (
    BotConfig,
    ControllerConfig,
    DeploymentConfig,
    GameConfig,
    HarnessConfig,
    ModelConfig,
    NotificationConfig,
    OnboardingConfig,
    PolicyConfig,
)


def config(root: Path, *, obsidian: bool = False) -> BotConfig:
    data = root / "data"
    logs = root / "logs"
    run = root / "run"
    for directory in (data, logs, run):
        directory.mkdir(parents=True, exist_ok=True)
    return BotConfig(
        source_path=root / "bot.toml",
        deployment=DeploymentConfig("test", "UTC", data, logs, run, root / "secrets.env"),
        game=GameConfig("192.0.2.1", 5959, "primary", "primary", None, True),
        harness=HarnessConfig(root, "test", "http://127.0.0.1:8901", 8902, "external", "node", data / "fleet.json"),
        model=ModelConfig("http://127.0.0.1:8000/v1", "test-model", 5, 5, 100, 0),
        controller=ControllerConfig("127.0.0.1", 0, "127.0.0.1", 0, 0.01, 0.01, 1, "survive", True),
        policy=PolicyConfig(
            True,
            True,
            0.7,
            0.4,
            500,
            5000,
            ("artifact",),
            "strongly_avoid_unnecessary_loss",
            automated_help_pleas=False,
        ),
        notifications=NotificationConfig(False, "notice", obsidian, root / "vault" if obsidian else None, "01 Projects/Meridian 59 Bot", "Meridian 59 Bot.md", "Journal"),
        secrets={"M59_BOT_CONTROL_TOKEN": "test-token"},
        # Most controller tests target goal execution. Onboarding behavior has
        # dedicated tests and is disabled in the common fixture to keep those
        # tests independent from persona setup.
        onboarding=OnboardingConfig(enabled=False),
    )


def goal_payload(request_id: str = "request-1", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "request_id": request_id,
        "title": "Drop a rusty sword",
        "objective": "Drop the rusty sword.",
        "success_criteria": [{"id": "gone", "kind": "state_equals", "path": "inventory.items", "value": []}],
        "constraints": {},
        "priority": 50,
        "activation": "queue",
    }
    payload.update(overrides)
    return payload
