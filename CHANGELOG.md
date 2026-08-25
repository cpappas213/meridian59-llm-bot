# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and intends to use semantic versioning after its first public release.

## Unreleased

### Added

- An explicitly confirmed `X` safe-shutdown action on the TUI main screen,
  including live drain-stage, paused-goal, verified-safe-room, logout, and
  failure reporting while `Q` remains a detach-only operation.
- A coordinated runtime shutdown sequence that pauses all runnable goals,
  recovers and routes to fresh source-verified safety, releases the keeper, logs
  out without forgetting the character, and fails safe without terminating when
  any required boundary cannot be verified.
- First-run onboarding driven by an operator-defined name/persona and a
  configured OpenAI-compatible model.
- A local installer persona wizard and JSON-input path that do not require a
  supervising model or MCP host, including inline length, content, usage, and
  privacy guidance for the voice and identity concept.
- A first-run-aware launcher and authenticated terminal dashboard for live
  character statistics, abilities, goals, queue state, events, and typed goal
  management without coupling UI lifetime to the controller loop.
- A model-backed TUI goal review loop that translates plain-language operator
  intent into a validated structured draft, then supports approve, cancel, and
  iterative natural-language revision before submission.
- Color-coded terminal status and an on-demand `S` character view containing
  complete reported skills, spells, inventory quantities, carry capacity, and
  server-verified equipment without enlarging the routine polling payload.
- OpenAI-compatible model discovery, a numbered installer model picker, and
  explicit unauthenticated, OpenAI/Codex Bearer, and Anthropic/Claude API-key
  modes with manual fallback.
- A validated regional timezone picker with common Windows/US alias
  normalization and incomplete-install recovery on the next launch.
- Explicit preservation of established characters unless replacement is
  requested.
- Public-repository documentation, contribution, security, and CI scaffolding.

### Fixed

- Initialized the keeper's current vigor before its hostile-room provisioning
  refusal reports it, preventing a fresh field keeper from aborting with a
  JavaScript temporal-dead-zone error before combat can begin.
- Counted campaign phase time only while its goal is active. Operator pauses,
  coordinated restarts, and repeated/interleaved pause cycles now accumulate
  per-phase downtime without instantly exhausting a resumed farming phase.
- Let coordinated shutdown depart from a full-health proven wall even when
  harmless adjacent monsters remain camped there, instead of waiting forever
  for a threat-clear condition the survival keeper cannot create while holding.
- Made operator safe-spot verification an evidence checkpoint instead of
  permanent immunity: a later observed hit retires the square again. Selection,
  keeper status, and broker reporting now consistently reject failed verified
  spots rather than repeatedly resting on them as proven.
- Preferred collision-proven baked room rails over learned coordinate tracks and
  bounded the remaining learned replay by one deadline and movement budget. Tight
  forest crossings no longer expand one stale waypoint into repeated fine-walk
  searches and wall oscillation.
- Made deterministic post-death and low-health help pleas explicitly opt-in and
  default-off without disabling self-rearming, LLM replies, or LLM-decided
  greetings. Restart reconciliation now fails closed on missing/stale plea
  policy, and the legacy templates no longer emit a mangled em-dash control
  character when deliberately enabled.
- Made `tui.bat` start a stopped scheduled controller or a hidden standalone
  controller when no task is installed, and wait for the broker to join the
  game before opening the console instead of racing startup.
- Imported structured compendium item values into financial planning and split
  source-estimated liquidation value from exact live merchant quotes. A zero
  confirmed quote now means sale-eligible loot still needs quoting rather than
  incorrectly implying that the inventory is worthless.
- Made exhausted progression-research support phases complete only after a
  material equipment, ability, inventory, route, quarantine, max-health, or
  knowledge change. Read-only equipment/ability calls no longer manufacture
  progress or recreate the same phase loop.
- Added authenticated `status` and graceful `stop` CLI commands plus a supported
  scheduled-controller restart script, preventing raw Task Scheduler stops from
  orphaning the Python controller or its owned Node broker on Windows.
- Removed implicit Tos Inn/bar completion travel from purchase and training
  flows. Finish destinations now come only from approved goal criteria, while
  farm launch staging comes from source-verified safe-room flags and live state
  instead of mainland/Raza room-ID policy.
- Updated the bundled `m59-harness` pin to public integration revision
  `1485107a797e97b0d9dbcc8d9629fd719e20ef17`, based on official upstream revision
  `892d94d6b0361970100d39b1f6fb35eb4a9ea794`. Open-field farm policy now ignores
  exhausted wall-search denials while retaining spawn-cap and independent danger
  evidence.
- Kept operator-named farm prey binding across research, campaign phases, and
  keeper launches. Exhausting only a safe-wall search now retries the same room
  and prey in bounded open-field mode, while death, critical-health, room-hazard,
  and spawn-cap evidence continues to block unsafe tactics.
- Made `Esc` an immediate, consistent cancel/back control across character
  status, goal drafting/review, goal modification, goal management, priority
  editing, and destructive confirmations.
- Rendered nested harness vital and attribute records as labeled human-readable
  values, including percentages, vigor scale/rest state, display scale, and hard
  caps instead of shortened Python/JSON objects.
- Increased character-build output headroom for reasoning-capable models and
  added explicit diagnostics for reasoning-only responses with no final JSON,
  preventing a healthy vLLM endpoint from leaving onboarding degraded.
- Clarified in the goal console that priority 100 is highest, 0 is lowest, and
  50 is the default.
- Replaced broad `Set-Acl` secret-file persistence with a verified access-only
  user SID rule that does not require `SeSecurityPrivilege`.
- Pointed the harness submodule at a publicly fetchable, privacy-safe integration
  revision.
- Replaced repository URL placeholders and added canonical project metadata.
- Strengthened publication checks for repository URLs, submodule-pin alignment,
  private-key material, and known token formats.
- Clarified alpha commissioning status and unresolved third-party licensing.
- Declared the Windows `tzdata` fallback and made CI install the packaged runtime
  dependencies before testing.

## 0.2.0 - 2026-08-07

### Added

- Durable campaign, knowledge, learning, social, and supervision foundations.
