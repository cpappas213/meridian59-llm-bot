# Public release checklist

This is the live release gate for the current alpha. A checked item requires
repeatable evidence; source visibility alone does not complete commissioning.
Unchecked items are intentional blockers to a stable release.

**Current status (2026-08-07): alpha / commissioning incomplete.** The source may
be reviewed publicly, but no stable deployment or unattended-operation claim is
made. The initial GitHub release must remain a prerelease until every applicable
gate below is complete or explicitly waived with a recorded rationale.

## Source publication gates

- [x] Remove private deployment defaults, identities, paths, model IDs, and
  standing campaign policy from public text and configuration.
- [x] Ignore local credentials, runtime data, incident exports/backfills,
  compiled caches, and the legacy deployment-specific infographic.
- [x] Add the README, root MIT license, third-party notice, contribution guide,
  security policy, changelog, editor settings, CI, and publication checks.
- [x] Replace repository URL placeholders and add canonical package URLs.
- [x] Document that the harness has no tracked license at the pinned revision;
  do not claim that the root MIT license covers it.
- [x] Verify from a fresh recursive clone that the configured harness URL,
  committed gitlink, example configuration, installer, and documentation all
  resolve to the same public revision.
- [x] Run compilation, all unit tests, wheel construction, PowerShell parsing,
  Markdown-link checks, metadata-version checks, privacy checks, and the GitHub
  Actions matrix against the final commit.
- [x] Review and stage every intended root change without including ignored
  local material or dirty harness working-tree contents.
- [ ] Confirm that MIT is the intended root-project license and that
  `Meridian 59 LLM Bot contributors` is the intended copyright attribution.
- [ ] Obtain or verify upstream permission before redistributing
  `m59-harness` contents; no tracked harness license has been found.

## Live commissioning gates

- [ ] Confirm that the target server permits automated play and that the account
  is dedicated or otherwise authorized for this deployment.
- [ ] Install from a fresh clone on a clean Windows user profile.
- [ ] Run `doctor` and verify the exact configured LLM model is advertised.
- [ ] Verify first status is `awaiting_persona` and that premature goal or
  proposal activation returns `ONBOARDING_REQUIRED`.
- [ ] Set a test persona. Verify a generated placeholder is replaced using the
  LLM-selected supported build, while an established identity is preserved
  without the explicit replacement flag.
- [ ] Verify onboarding becomes `ready_for_goals` with no auto-created goal,
  then submit one reversible human-authored goal.
- [ ] Exercise restart, model outage, broker outage, dashboard redaction, event
  pagination, and notification behavior with no secret leakage.
- [ ] Complete the documented 24-hour soak before declaring a stable release.

## GitHub publication gates

- [ ] Require a green CI workflow on the published `main` commit.
- [ ] Enable private vulnerability reporting.
- [ ] Add repository topics and a concise public description.
- [ ] Protect `main`, require pull requests, and require the CI matrix checks.
- [ ] Publish the initial GitHub release as a prerelease.
