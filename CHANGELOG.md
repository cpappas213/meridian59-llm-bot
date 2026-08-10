# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and intends to use semantic versioning after its first public release.

## Unreleased

### Added

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

- Updated the bundled `m59-harness` pin to the latest tested official upstream
  revision and removed the stale integration-fork dependency.
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
