# Contributing

Thanks for helping improve the project. Keep changes narrow, reviewable, and
compatible with the pinned public harness contract.

## Development setup

```powershell
git clone --recurse-submodules <repository-url>
Set-Location .\meridian59-llm-bot
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test.ps1
```

Python 3.11 or later is required. Runtime code should remain standard-library
only unless a dependency is justified in the pull request.

## Pull requests

- Describe user-visible behavior and verification performed.
- Add or update tests for behavior changes.
- Update requirements, interfaces, configuration examples, and acceptance tests
  when a contract changes.
- Keep secrets, account identifiers, private hosts, incident exports, runtime
  databases, and generated logs out of commits.
- Do not modify or commit a dirty submodule worktree. Update the gitlink and the
  documented expected revision together when intentionally upgrading it.
- Preserve the separation between operator goals, LLM tactics, controller
  policy, and ordinary harness actions.

Run `scripts/test.ps1` before submitting. The CI workflow runs the same compile
and unit-test checks on supported Python versions, then runs
`python scripts/check_publication.py` to check release metadata and common
privacy regressions.
