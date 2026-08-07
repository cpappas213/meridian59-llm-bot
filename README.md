# Meridian 59 LLM Bot

An experimental Windows control plane for an LLM-driven Meridian 59 character.
This project supplies durable goals, policy, supervision, and an
OpenAI-compatible model loop while the separately maintained
[`m59-harness`](https://github.com/tpeppers/m59-harness) owns the ordinary game
protocol and mechanical actions.

> [!WARNING]
> This is pre-release software. Run it on an account and server where automated
> play is permitted, review the fair-play policy, and keep all control services
> on loopback unless you understand the exposure.

## Onboarding model

A fresh installation intentionally has no character policy or built-in gameplay
goal. Setup proceeds in this order:

1. A human configures the Meridian 59 server, account, and an
   OpenAI-compatible LLM endpoint/model during installation.
2. The human or supervising agent sets the desired character name and complete
   conversation persona through the `persona` MCP tool.
3. The configured LLM selects a supported build. The controller previews,
   audits, creates, and verifies the named character through the harness.
4. Once status reports `onboarding.ready_for_goals=true`, a human or higher-level
   agent submits the first strategic goal.

Generated first-run names such as `User123456789` may be replaced automatically.
An established, differently named character is preserved unless the persona is
set again with `replace_existing_character=true`. Character creation never
invents a gameplay goal.

## Capabilities

- Durable SQLite goals, proposals, persona versions, events, action attempts,
  consequence assessments, and cross-goal lessons.
- One-action-at-a-time planning through a configurable OpenAI-compatible API.
- A deterministic authority layer that prevents model access to controller and
  account-lifecycle tools.
- Survival handoff, isolated model-generated social replies, and audited
  controller-owned tactical composites.
- An authenticated loopback control API and a separate read-only dashboard.
- Six supervision MCP tools plus a read-only knowledge MCP server backed by the
  pinned harness compendium.
- Optional Windows notifications and sparse Obsidian milestone journals.
- A deterministic simulator and standard-library test suite.

The behavioral boundary is no cheating. Server rules and permission to automate
remain the operator's responsibility.

## Requirements

- Windows 10 or 11 and PowerShell 5.1 or later.
- Python 3.11 or later.
- Node.js compatible with the pinned harness.
- Git with submodule support.
- A reachable Meridian 59 server and authorized account.
- An OpenAI-compatible chat-completions endpoint and model ID.
- Optional: Hermes/Codex-compatible MCP host, Obsidian, and Windows notifications.

## Install

```powershell
git clone --recurse-submodules <repository-url>
Set-Location .\meridian59-llm-bot
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

If the repository was cloned without submodules, run:

```powershell
git submodule update --init --recursive
```

The installer prompts for the game endpoint and credentials, LLM base URL and
model ID, timezone, optional Obsidian path, and dashboard bind address. It stores
secrets in an ACL-restricted file below `%LOCALAPPDATA%\m59-llm-bot`, writes the
runtime TOML configuration, registers a restart-on-failure logon task, and adds
the controller and knowledge MCP servers when the `hermes` command is available.
Restart the MCP host after installation.

The default request contract uses OpenAI JSON response mode. If an otherwise
compatible endpoint does not implement `response_format`, set
`model.json_mode=false`. `model.disable_thinking` is off by default and should be
enabled only for servers/models that support Qwen-style `enable_thinking`.

The default dashboard is <http://127.0.0.1:8904/>. The mutation API and harness
broker remain loopback-only.

## First run

1. Run a dependency check without printing credentials:

   ```powershell
   $env:PYTHONPATH = "$PWD\src"
   python -m meridian_bot.cli --config "$env:LOCALAPPDATA\m59-llm-bot\bot.toml" doctor
   ```

2. Read the `persona` tool and set a complete persona with the desired name.
3. Poll `status` until onboarding is ready. If an established character should
   be replaced, repeat the persona update with an explicit replacement flag.
4. Submit one high-level goal with deterministic completion criteria.

The controller does not embed a default character, PvP quota, destination, or
progression target. Those are operator policy and belong in explicit goals.

## Development

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test.ps1
```

The runtime package uses only the Python standard library. Tests use the local
simulator and do not connect to a game account, model server, or Obsidian vault.

## Documentation

- [Implementation and operations](docs/implementation.md)
- [Requirements package](docs/requirements/README.md)
- [Knowledge system](docs/knowledge.md)
- [Durable goal learning](docs/goal-learning.md)
- [Long-horizon execution architecture](LONG_HORIZON_EXECUTION_ARCHITECTURE.md)
- [Public release checklist](docs/publication-checklist.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## License and third-party code

Project-authored code is available under the [MIT License](LICENSE). The
`vendor/m59-harness` submodule is a separate project governed by its own license;
see [third-party notices](THIRD_PARTY_NOTICES.md) before redistribution.
