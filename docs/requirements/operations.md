# Deployment and operations runbook

## 1. Scope

The supported deployment is a single Windows desktop controlling one account.
The controller, harness broker, LLM endpoint, and game server may run on separate
hosts, but mutation surfaces must remain loopback-local to the controller.

## 2. Prerequisites

- Windows 10/11 and PowerShell 5.1+.
- Python 3.11+ and a compatible Node.js on `PATH`, or explicit executable paths.
- Git with the pinned `vendor/m59-harness` submodule initialized.
- An authorized game account and a reachable server.
- A reachable OpenAI-compatible API with a known model ID.
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
- IANA timezone;
- LLM base URL, model ID, and optional API key;
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
| `model` | OpenAI-compatible base URL, exact model ID, timeouts, output budget, JSON-mode and optional thinking controls. |
| `controller` | Loopback API, read-only dashboard, cadence, conversation bounds. |
| `onboarding` | Persona-driven creation and established-character preservation. |
| `policy` | Survival and consequence guidance. |
| `learning` | Failure budgets and deterministic retry cooldowns. |
| `notifications` | Optional Windows/Obsidian sinks. |

`secrets.env` supports `M59_ACCOUNT_USERNAME`, `M59_ACCOUNT_PASSWORD`,
`M59_BOT_CONTROL_TOKEN`, `M59_LLM_API_KEY`, and
`M59_OBSIDIAN_VAULT_PATH`. Do not quote values, print the file, or commit it.

## 5. First-run onboarding

After installation and MCP-host restart:

1. Run `doctor` and correct dependency failures.
2. Read persona/status. Status should initially say `awaiting_persona`.
3. A human supplies the character name and persona. The supervisor sets the full
   versioned persona.
4. The configured LLM chooses a supported build; the controller previews,
   audits, creates, and verifies the character.
5. An established differently named character is not replaced without an
   explicit `replace_existing_character=true` persona update.
6. Wait for `onboarding.ready_for_goals=true`.
7. A human or higher-level agent supplies the first strategic goal.

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
Stop-ScheduledTask -TaskName "Meridian59 LLM Bot"
Start-ScheduledTask -TaskName "Meridian59 LLM Bot"

# Remove task and MCP registrations; retain runtime state
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1
```

Routine supervision should use compact `status(detail="supervision",
include_recent_events=0)` and fetch detailed events only for a specific
ambiguity.

## 7. Backup and restore

Stop the scheduled task before copying runtime state. Back up `bot.toml`,
`secrets.env`, `data/controller.sqlite3`, its WAL/SHM files when present, and
the harness fleet-state file to an encrypted private location. Restore them only
to a trusted machine, verify ACLs, then run `doctor` before starting the task.

Never publish runtime state or diagnostics; both may identify accounts, private
servers, players, inventory, or tokens.

## 8. Updating

1. Stop the scheduled task.
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
- **Harness revision mismatch:** initialize/update the submodule to the committed
  revision; never bypass the expected-revision check.
- **Character preserved:** this is intentional for an established identity. Set
  the persona again with the explicit replacement flag only after operator
  confirmation.
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
