# Meridian 59 LLM Bot

[![CI](https://github.com/cpappas213/meridian59-llm-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/cpappas213/meridian59-llm-bot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An experimental Windows control plane for an LLM-driven Meridian 59 character.
This project supplies durable goals, policy, supervision, and an
OpenAI-compatible model loop while the separately maintained
[`m59-harness`](https://github.com/tpeppers/m59-harness) project owns the
ordinary game protocol and mechanical actions.

> [!IMPORTANT]
> **Alpha status:** source publication and automated validation do not mean the
> bot has completed live commissioning. Clean-profile installation, live
> onboarding, outage exercises, and the 24-hour soak remain release gates. No
> stable release is declared; see the
> [public release checklist](docs/publication-checklist.md).

> [!WARNING]
> This is pre-release software. Run it on an account and server where automated
> play is permitted, review the fair-play policy, and keep all control services
> on loopback unless you understand the exposure.

## Onboarding model

A fresh installation intentionally has no character policy or built-in gameplay
goal. Setup proceeds in this order:

1. A human configures the Meridian 59 server, account, and an
   OpenAI-compatible LLM endpoint/model during installation.
2. The installer locally collects the desired character name and complete
   conversation persona. It can also be revised later through the `persona` MCP
   tool or the local `setup-persona` command.
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
- Node.js 22 or later; Node 24 LTS is recommended by the pinned harness.
- Git with submodule support.
- A reachable Meridian 59 server and authorized account.
- An OpenAI-compatible chat-completions endpoint and model ID.
- Optional: Hermes/Codex-compatible MCP host, Obsidian, and Windows notifications.

## Install

```powershell
git clone --recurse-submodules https://github.com/cpappas213/meridian59-llm-bot.git
Set-Location .\meridian59-llm-bot
python -m pip install --editable .
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\launch.ps1
```

If the repository was cloned without submodules, run:

```powershell
git submodule update --init --recursive
```

The installer prompts for the game endpoint and credentials, LLM base URL,
timezone, and a complete operator-authored character persona. Timezone is a
numbered regional picker that stores valid IANA names; common inputs such as
`PST` are normalized to `America/Los_Angeles`, and advanced entries are
validated immediately. After the LLM URL
is entered, setup queries its OpenAI-compatible `/models` endpoint and presents
the returned model IDs as a numbered menu. Setup explicitly supports no
authentication, Bearer API keys for OpenAI/Codex and compatible hosts, and
Anthropic API keys for Claude (`x-api-key` plus the Anthropic API-version
header). Manual model-ID entry remains available as a fallback. These choices
use provider API credentials; setup does not read or reuse a ChatGPT/Codex or
Claude/Claude Code subscription-login session. Persona entry is local and
deterministic; it does not call a supervising model. The
installer stores secrets in an ACL-restricted file below
`%LOCALAPPDATA%\m59-llm-bot`, persists the persona before launch, writes the
runtime TOML configuration, registers a restart-on-failure logon task, and adds
the controller and knowledge MCP servers when the `hermes` command is available.
Restart the MCP host after installation. The launcher opens a live terminal
dashboard after setup. On later runs it detects the existing installation,
starts the controller task if needed, and returns directly to the dashboard.
Press `Q` to leave the dashboard without stopping the bot. Direct installer users
may pass `-SkipPersonaSetup`, `-PersonaFile .\persona.json`, or `-SkipTui`.

The package installs `tzdata` on Windows so Python can resolve configured IANA
timezone names. Unix-like systems continue to use their system timezone data.

The default request contract uses OpenAI JSON response mode. If an otherwise
compatible endpoint does not implement `response_format`, set
`model.json_mode=false`. Setup asks whether to disable model thinking and
recommends doing so for Qwen models because reasoning tokens count against the
completion budget and can delay controller actions. `model.disable_thinking`
should be enabled only for servers/models that support Qwen-style
`enable_thinking`; generic endpoints default to keeping their normal behavior.
`model.auth_mode` accepts `none`, `bearer`, or `anthropic`; legacy configurations
without it use `auto`, which sends Bearer auth only when a model key exists.

The default dashboard is <http://127.0.0.1:8904/>. The mutation API and harness
broker remain loopback-only.

## First run

1. Run a dependency check without printing credentials:

   ```powershell
   $env:PYTHONPATH = "$PWD\src"
   python -m meridian_bot.cli --config "$env:LOCALAPPDATA\m59-llm-bot\bot.toml" doctor
   ```

2. The installer has already stored the desired name and persona. The terminal
   dashboard shows onboarding, connection, character vitals, skills/spells,
   current goal, queue, and recent events while onboarding completes.
3. If an established differently named character should be replaced, explicitly
   confirm that decision from the terminal without a supervising agent:

   ```powershell
   python -m meridian_bot.cli --config "$env:LOCALAPPDATA\m59-llm-bot\bot.toml" `
     setup-persona --update-existing --reuse-current --replace-existing-character
   ```

4. Press `N` in the terminal dashboard and describe the first high-level goal in
   plain language. The configured model constructs a validated structured draft
   for approval. Press `M` to pause, resume, cancel, reprioritize, or confirm an
   operator criterion. To provide that confirmation, press `M`, select the goal,
   press `F`, and type `CONFIRM`. The controller accepts it only after every
   observable criterion in that goal is already verified. Confirmation latches
   the outcome; the goal becomes terminal only after the character reaches the
   model-selected, source-verified safe ending in its execution plan.

To run or revise persona setup independently, use `setup-persona`. With no
arguments it prompts for the name, voice, traits, speech style, values, taboos,
relationship defaults, and reply limit. An existing persona is preserved unless
`--update-existing` is explicit.

The complete active persona is supplied to both long-horizon campaign planning
and tactical planning. It can shape choices among equally safe, goal-compatible
strategies—including which source-verified safe location ends a plan—but cannot
override the operator's goal or controller policy.

Every accepted tactical plan must end with exact travel to a source-verified
safe location selected by the model. When an internal campaign phase or the
public goal becomes complete, the controller latches that result, permits only
the declared safe-ending step, and advances only after fresh room and safety
verification.

## Controller maintenance

Use the supported restart command for upgrades or routine maintenance:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restart-controller.ps1
```

It authenticates to the running controller and immediately starts its coordinated
shutdown sequence: pause every runnable goal, let any in-flight mutation settle,
recover and route to a source-verified safe room when needed, stop the keeper,
log the character out without forgetting it, and only then stop the controller
and its owned broker. The script waits for that sequence to finish, starts the
scheduled action again, and verifies a joined game session. Paused goals remain
paused after restart until an operator explicitly resumes one. Do not substitute
a raw `Stop-ScheduledTask`; Windows can stop only the PowerShell wrapper and
leave Python and Node holding the instance lock and network ports.

To shut down and leave the character logged out, use the authenticated command:

```powershell
python -m meridian_bot.cli --config "$env:LOCALAPPDATA\m59-llm-bot\bot.toml" stop
```

Add `--safe-room 52` only when you want an exact source-verified safe
destination. If recovery, travel, safety verification, keeper release, or logout
fails, the controller stays alive with goals paused and survival mode retained
instead of terminating while the character may still be exposed.

For the voice and identity concept, write one paragraph of roughly 2-4 sentences
or 40-100 words. Describe the broad archetype/background impression, emotional
tone, social presence, and a useful tension or flaw. Setup explains that this
context informs the initial build, roleplay-aware planning, and dialogue but
cannot create goals or override policy; focused traits and speech rules are
collected separately.

## Goal monitoring console

The terminal dashboard is an authenticated local client; it never starts a
second game loop or takes direct control of the harness. The scheduled controller
continues independently if the console is closed. Reopen it at any time with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\launch.ps1
```

The goal workflow accepts one plain-language outcome and asks the configured vLLM
to construct the typed location, inventory, numeric threshold/delta, durable
event, exact observed-state, or operator-confirmation criteria. The complete JSON
draft is shown before any mutation. Approve submits it, cancel stores nothing,
and modify sends the displayed object plus new instructions back to the model for
another review cycle. Model drafts still pass the controller's normal schema and
knowledge validation, policy, versioning, and idempotency checks. Goal priority
ranges from 0 (lowest) to 100 (highest), with 50 as the default; higher-priority
queued goals run first.

The live dashboard uses color to distinguish healthy, warning, failure, goal,
vital, priority, and event states. Press `S` for the complete read-only character
view: every reported skill and spell with its 0-100 ability, spell readiness,
inventory quantities and carry capacity, server-verified equipped items and
wielded weapons, attributes, vitals, and location. Press `Esc` or Enter to return;
`Esc` also cancels goal creation or management from any nested prompt without
submitting a partial change. Set the standard `NO_COLOR` environment variable
before launch to disable ANSI colors.

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
`vendor/m59-harness` submodule is a separate project. No tracked license file was
found at the pinned revision, so availability on GitHub must not be interpreted
as permission to redistribute it. See
[third-party notices](THIRD_PARTY_NOTICES.md) before redistribution.
