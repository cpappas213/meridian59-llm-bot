# Deployment and operations runbook

## 1. Scope

The supported deployment is a single Windows desktop controlling one account.
The controller, harness broker, LLM endpoint, and game server may run on separate
hosts, but mutation surfaces must remain loopback-local to the controller.

## 2. Prerequisites

- Windows 10/11 and PowerShell 5.1+.
- Python 3.11+ and Node.js 22+ on `PATH` (Node 24 LTS recommended), or explicit executable paths.
- Git with the pinned `vendor/m59-harness` submodule initialized.
- An authorized game account and a reachable server.
- A reachable OpenAI-compatible API exposing `/models`, or a known model ID for
  manual fallback.
- Optional Hermes CLI for MCP registration, Obsidian vault, and notifications.

## 3. Fresh installation

```powershell
git clone --recurse-submodules https://github.com/cpappas213/meridian59-llm-bot.git
Set-Location .\meridian59-llm-bot
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

The interactive installer asks for:

- game host, port, username, and password;
- timezone from a numbered regional picker; common aliases are normalized to an
  IANA timezone and advanced entries are validated before configuration is
  written;
- LLM base URL and authentication mode (`none`, Bearer for OpenAI/Codex, or
  Anthropic API key for Claude); the installer queries `/models` with the
  selected provider headers and offers a numbered model picker, with manual
  model-ID entry as a fallback;
- optional Obsidian vault path; and
- dashboard bind address (loopback by default).

For unattended setup, pass the non-secret parameters and a PowerShell
`PSCredential`; use `-SkipHermes` to suppress Hermes auto-discovery and
`-SkipScheduledTask` for a foreground development installation. A missing
Hermes CLI produces a warning and does not prevent controller installation.

The installer writes `%LOCALAPPDATA%\m59-llm-bot\bot.toml` and an ACL-restricted
`secrets.env`, optionally discovers installed Meridian resource strings,
registers one restart-on-failure logon task, starts it, and registers both MCP
servers when Hermes is present.

## 4. Configuration

The authoritative template is [`config/bot.example.toml`](../../config/bot.example.toml).
Configuration is TOML and unknown keys fail closed. Important sections are:

| Section | Purpose |
|---|---|
| `deployment` | Instance ID, timezone, private data/log/run paths, secret file. |
| `game` | Server endpoint, account alias, agent, and autojoin. |
| `harness` | Root, exact expected revision, broker endpoint, Node path, state file. |
| `model` | OpenAI-compatible base URL, exact model ID, explicit auth mode, timeouts, output budget, JSON-mode and optional thinking controls. |
| `controller` | Loopback API, read-only dashboard, cadence, conversation bounds. |
| `onboarding` | Non-mutating verification of selected-character/persona identity. |
| `policy` | Survival and consequence guidance. |
| `learning` | Failure budgets and deterministic retry cooldowns. |
| `notifications` | Optional Windows/Obsidian sinks. |

Keep `harness.lifecycle = "controller_managed"` for the supported deployment.
If an operator deliberately supplies an external broker at the pinned harness
revision, it must launch `m59-broker.mjs` with `--in-process`; the bot depends on
the direct synchronous session contract. That revision's `/health` response does
not report the keeper arrangement, so the bot cannot verify an external process's
flag after attachment. Do not use the harness dashboard's broker-restart control
for this deployment because it does not preserve `--in-process`.

`secrets.env` supports `M59_ACCOUNT_USERNAME`, `M59_ACCOUNT_PASSWORD`,
`M59_BOT_CONTROL_TOKEN`, `M59_LLM_API_KEY`, and
`M59_OBSIDIAN_VAULT_PATH`. Do not quote values, print the file, or commit it.
`M59_LLM_API_KEY` is a provider API credential. Do not copy a Codex, ChatGPT,
Claude, or Claude Code subscription-login token into it.

## 5. First-run onboarding

During installation and first launch:

1. The installer locally prompts for and persists the human-supplied character
   name and complete persona. This path does not require an MCP host or a
   supervising model. `-SkipPersonaSetup` deliberately leaves onboarding at
   `awaiting_persona`; `-PersonaFile` supports unattended setup.
2. Run `doctor` and correct dependency failures.
3. Read persona/status and verify that the intended persona is versioned and
   onboarding is waiting for or verifying the selected live identity.
4. Select or create the intended character outside the controller. The controller
   verifies only that its live name matches the persona.
5. If names differ, select the intended character externally or update the
   persona name. The controller permanently refuses all character replacement.
6. Wait for `onboarding.ready_for_goals=true`.
7. A human or higher-level agent supplies the first strategic goal.

The supported interactive entry point is first-run aware:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\launch.ps1
```

With no installed configuration it runs setup once and hands off to the terminal
dashboard. With an existing configuration it offers the live console or an
explicit reconfiguration path, starts the scheduled controller if necessary,
and does not repeat setup. The console uses color for rapid state scanning;
press `S` for complete character abilities, inventory, attributes, and verified
equipment, then Enter to return. Set `NO_COLOR` before launch for plain output.
Leaving the console does not stop the controller.

The same local wizard can be run manually:

```powershell
python -m meridian_bot.cli --config "$env:LOCALAPPDATA\m59-llm-bot\bot.toml" setup-persona
```

It preserves an existing persona by default. To change only the persona, use
`--update-existing` (and optionally `--reuse-current`). Character replacement is
not a supported command; the compatibility flag fails with a permanent-disabled
error and performs no write.

No standing goal is installed and no goal is inferred from persona text.

## 6. Routine commands

```powershell
# Offline verification
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test.ps1

# Installed dependency check
$env:PYTHONPATH = "$PWD\src"
python -m meridian_bot.cli --config "$env:LOCALAPPDATA\m59-llm-bot\bot.toml" doctor

# Scheduled controller
Get-ScheduledTask -TaskName "Meridian59 LLM Bot"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restart-controller.ps1

# Reattach the interactive goal/status console
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\launch.ps1

# Remove task and MCP registrations; retain runtime state
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1
```

Routine supervision should use compact `status(detail="supervision",
include_recent_events=0)` and fetch detailed events only for a specific
ambiguity.

## 7. Backup and restore

Use `scripts/restart-controller.ps1` for routine maintenance. It immediately
asks the controller to begin its coordinated shutdown: pause all runnable goals,
let an in-flight mutation settle, recover and route to source-verified safety,
stop the keeper, log out with `forget=false`, and stop itself and its owned
broker. The script waits for completion before starting the scheduled task
again. Paused goals are deliberately not resumed by the restart. A raw
`Stop-ScheduledTask` can terminate only the PowerShell wrapper and leave its
Python and Node children holding the instance lock and ports—or strand the
character in game.

Gracefully stop the controller before copying runtime state. Back up `bot.toml`,
`secrets.env`, `data/controller.sqlite3`, its WAL/SHM files when present, and
the harness fleet-state file to an encrypted private location. Restore them only
to a trusted machine, verify ACLs, then run `doctor` before starting the task.

To leave the controller stopped for a backup, run the authenticated stop command
and wait for the task state to become `Ready`. The controller itself establishes
and verifies the safe boundary before logging out:

```powershell
python -m meridian_bot.cli --config "$env:LOCALAPPDATA\m59-llm-bot\bot.toml" status
python -m meridian_bot.cli --config "$env:LOCALAPPDATA\m59-llm-bot\bot.toml" stop
Get-ScheduledTask -TaskName "Meridian59 LLM Bot"
```

Use `stop --safe-room <room-id>` only to require an exact destination whose
source knowledge has sanctuary or no-combat flags. Without it, an already-safe
current room is retained; otherwise the controller tries bounded verified safe
candidates. If the task does not become `Ready`, inspect
`controller.shutdown`: shutdown intentionally fails open at the process level
but safe at the gameplay level, keeping the controller alive, every goal paused,
and survival mode running when the character remains joined. Resolve that error
before retrying; never force-stop an exposed character.

Never publish runtime state or diagnostics; both may identify accounts, private
servers, players, inventory, or tokens.

## 8. Updating

1. Run `m59-bot --config <path> stop`; the coordinated shutdown establishes a
   verified safe boundary and logs out before the scheduled task becomes
   `Ready`. Do not use `Stop-ScheduledTask`.
2. Back up private state.
3. Pull the root repository and update submodules to the committed gitlink.
4. Confirm the submodule has no local modifications.
5. Run `scripts/test.ps1`.
6. Re-run `scripts/install.ps1` with the intended parameters to refresh paths,
   configuration, task registration, and MCP entries.
7. Run `doctor`, restart the MCP host, and inspect onboarding/goal status.

Do not advance the harness independently of the root gitlink and
`harness.expected_revision`.

## 9. Troubleshooting

- **Model unavailable/model missing:** verify base URL, API key, and exact model
  returned from `/models`.
- **Planner retry suppressed:** inspect `planner.retry_circuit.opened`,
  `.suppressed`, and `.reopened` events. A deterministic protocol,
  response-format, prompt-budget, or plan-verification failure remains suppressed
  across restart while the goal, phase, operation, plan, inventory, equipment,
  abilities, room, and visible-object fingerprint is unchanged. Correct the
  cause or allow material state to change; restarting alone is not a retry.
- **Observation reports `freshness.mode=tactical_cache`:** expected while the
  keeper is fighting, pulling, travelling, retreating, or recovering. Room and
  vitals come from pushed look state; inventory and spells may be marked cached
  or unknown until urgent keeper ownership ends and the next full refresh runs.
- **Death occurred during `survive`:** inspect `character.died` and the durable
  `character_death_cursor_v1` runtime receipt. Mode handoff does not suppress
  accounting, and a repeated death ID is intentionally ignored. Do not manually
  quarantine a verified wall solely because the already engaged monster damaged
  or killed the character there.
- **Harness revision mismatch:** initialize/update the submodule to the committed
  revision; never bypass the expected-revision check.
- **Character/persona name mismatch:** the selected character is intentionally
  preserved and gameplay goals remain withheld. Select or create the intended
  character outside the controller, or update the persona name to match it.
- **No active goal after setup:** expected. A human or supervisor must supply it.
- **Control API rejected:** verify loopback URL and control token without logging
  the token.
- **Dashboard unavailable on another device:** LAN binding is opt-in; use a
  trusted network and authenticated reverse proxy for broader exposure.
- **Obsidian disabled:** configure an explicit vault path. The controller never
  scrapes another application's environment.

## 10. Uninstall

`scripts/uninstall.ps1` removes the scheduled task and both MCP registrations but
retains state and credentials by default. After confirming no recovery is
needed, remove `%LOCALAPPDATA%\m59-llm-bot` manually using a recoverable method.
