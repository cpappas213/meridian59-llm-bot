# Public release checklist

Use this checklist before making the GitHub repository public.

## Repository checks

- [x] Remove private deployment defaults, identities, paths, model IDs, and
  standing campaign policy from public text/configuration.
- [x] Ignore local credentials, runtime data, incident exports/backfills, and the
  legacy deployment-specific infographic.
- [x] Align the committed harness gitlink, example configuration, installer, and
  implementation documentation to one tested revision.
- [x] Add README, license, third-party notice, contribution guide, security
  policy, changelog, editor settings, CI, and publication checks.
- [x] Verify compilation, unit tests, wheel construction, PowerShell parsing,
  Markdown links, metadata versions, and common privacy patterns.
- [ ] Confirm that MIT is the intended license and that the copyright attribution
  is acceptable.
- [ ] Replace `<repository-url>` examples after the GitHub owner/repository name
  is chosen.
- [ ] Review and stage every intended root file. Do not include ignored local
  incident material or the dirty contents of the harness submodule.
- [ ] Review the pinned harness's own license before distributing submodule
  contents.

## Commissioning checks

- [ ] Confirm that the target server permits automated play and that the account
  is dedicated/authorized for this deployment.
- [ ] Install from a fresh clone on a clean Windows user profile.
- [ ] Run `doctor` and verify the exact configured LLM model is advertised.
- [ ] Verify first status is `awaiting_persona` and that premature goal/proposal
  activation returns `ONBOARDING_REQUIRED`.
- [ ] Set a test persona. Verify a generated placeholder is replaced using the
  LLM-selected supported build, while an established identity is preserved
  without the explicit replacement flag.
- [ ] Verify onboarding becomes `ready_for_goals` with no auto-created goal, then
  submit one reversible human-authored goal.
- [ ] Exercise restart, model outage, broker outage, dashboard redaction, event
  pagination, and notification behavior with no secret leakage.
- [ ] Complete the documented 24-hour soak before declaring a stable release.

## GitHub settings

- [ ] Enable private vulnerability reporting/security advisories.
- [ ] Add repository topics, description, and the final source URL to package
  metadata after the repository name is known.
- [ ] Protect the default branch and require the CI workflow for pull requests.
- [ ] Publish the initial release as a prerelease until live commissioning and
  soak criteria are complete.
