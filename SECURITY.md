# Security policy

## Supported versions

This project is pre-release. Security fixes are applied to the latest revision
only.

## Reporting a vulnerability

Do not open a public issue containing credentials, access tokens, private server
addresses, account data, or exploitable details. Use GitHub's private security
advisory feature for the repository. If that feature is unavailable, contact the
maintainer privately before disclosure.

Include the affected revision, impact, reproduction steps, and suggested
mitigation. Remove secrets and personal game history from logs.

## Deployment expectations

- Keep the controller mutation API and harness broker bound to loopback.
- Treat `secrets.env`, SQLite state, fleet state, logs, diagnostics, and journal
  output as private account data.
- Use a distinct control token and an LLM endpoint you trust.
- Do not expose the read-only dashboard beyond trusted networks without an
  authenticated reverse proxy.
- Review local server rules and obtain permission before automated play.
