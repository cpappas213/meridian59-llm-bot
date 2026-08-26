"""Fail CI when public repository metadata or documentation regresses."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_URL = "https://github.com/cpappas213/meridian59-llm-bot"
HARNESS_URL = "https://github.com/cpappas213/m59-harness.git"
TEXT_SUFFIXES = {".md", ".py", ".ps1", ".toml", ".yml", ".yaml", ".txt"}
EXCLUDED_PREFIXES = (
    ".git/",
    ".venv/",
    ".worktrees/",
    "build/",
    "runtime/",
    "vendor/",
    "__pycache__/",
)
EXCLUDED_FILES = {
    "_extract_events.py",
    "_fetch_status_events.py",
    "scripts/backfill-room535-farm-incident.py",
    "scripts/backfill-room603-safe-spot-failure.py",
}


def public_text_files() -> list[Path]:
    result: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative in EXCLUDED_FILES or any(
            relative.startswith(prefix) or f"/{prefix}" in relative
            for prefix in EXCLUDED_PREFIXES
        ):
            continue
        result.append(path)
    return result


def git_output(*arguments: str) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    return completed.returncode, completed.stdout.strip()


def main() -> int:
    errors: list[str] = []
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_text = (ROOT / "src" / "meridian_bot" / "__init__.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', package_text, re.MULTILINE)
    package_version = match.group(1) if match else None
    project_version = project["project"]["version"]
    if package_version != project_version:
        errors.append(
            f"version mismatch: pyproject={project_version}, package={package_version}"
        )

    expected_project_urls = {
        "Homepage": REPOSITORY_URL,
        "Repository": f"{REPOSITORY_URL}.git",
        "Issues": f"{REPOSITORY_URL}/issues",
        "Documentation": f"{REPOSITORY_URL}/tree/main/docs",
    }
    if project["project"].get("urls") != expected_project_urls:
        errors.append("pyproject.toml: canonical project URLs are missing or incorrect")

    bot_config = tomllib.loads(
        (ROOT / "config" / "bot.example.toml").read_text(encoding="utf-8")
    )
    expected_revision = bot_config["harness"]["expected_revision"]
    if not re.fullmatch(r"[0-9a-f]{40}", expected_revision):
        errors.append("config/bot.example.toml: harness revision is not a full SHA-1")

    gitmodules = (ROOT / ".gitmodules").read_text(encoding="utf-8")
    if f"url = {HARNESS_URL}" not in gitmodules:
        errors.append(".gitmodules: harness URL is not the authoritative public upstream")

    returncode, staged_gitlink = git_output(
        "ls-files", "--stage", "--", "vendor/m59-harness"
    )
    gitlink_match = re.fullmatch(
        r"160000\s+([0-9a-f]{40})\s+\d+\s+vendor/m59-harness", staged_gitlink
    )
    if returncode or not gitlink_match:
        errors.append("vendor/m59-harness: missing committed Git submodule entry")
    elif gitlink_match.group(1) != expected_revision:
        errors.append(
            "vendor/m59-harness: gitlink does not match config harness revision"
        )

    pin_consumers = {
        "scripts/install.ps1": (ROOT / "scripts" / "install.ps1").read_text(
            encoding="utf-8"
        ),
        "docs/implementation.md": (ROOT / "docs" / "implementation.md").read_text(
            encoding="utf-8"
        ),
    }
    for relative, text in pin_consumers.items():
        if expected_revision not in text:
            errors.append(f"{relative}: does not reference the configured harness revision")

    absolute_user_path = re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE)
    private_ipv4 = re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|"
        r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
    )
    secret_assignment = re.compile(
        r"(?im)^\s*(?:M59_ACCOUNT_PASSWORD|M59_BOT_CONTROL_TOKEN|M59_LLM_API_KEY|"
        r"M59_VLLM_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|AZURE_OPENAI_API_KEY|"
        r"ACCESS_TOKEN|SECRET_KEY|CLIENT_SECRET)\s*=\s*[^#\s]+"
    )
    credential_token = re.compile(
        r"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
        r"github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})"
    )
    private_key = re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    )
    url_userinfo = re.compile(r"https?://[^/\s@]+:[^/\s@]+@", re.IGNORECASE)
    forbidden_placeholders = (
        "<" + "repository-url>",
        "YOUR_" + "REPOSITORY_URL",
    )

    for path in public_text_files():
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if absolute_user_path.search(text):
            errors.append(f"{relative}: contains an absolute Windows user path")
        if private_ipv4.search(text):
            errors.append(f"{relative}: contains a private IPv4 address")
        if relative != "config/secrets.example.env" and secret_assignment.search(text):
            errors.append(f"{relative}: contains a non-empty secret assignment")
        if credential_token.search(text):
            errors.append(f"{relative}: contains a recognized credential-token shape")
        if private_key.search(text):
            errors.append(f"{relative}: contains private-key material")
        if url_userinfo.search(text):
            errors.append(f"{relative}: contains credentials embedded in a URL")
        for placeholder in forbidden_placeholders:
            if placeholder in text:
                errors.append(f"{relative}: contains publication placeholder {placeholder}")
        if path.suffix.lower() == ".md":
            for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
                target = target.strip().strip("<>").split(" ", 1)[0]
                parsed = urlparse(target)
                if parsed.scheme or target.startswith("#"):
                    continue
                local = unquote(target.split("#", 1)[0])
                if local and not (path.parent / local).resolve().exists():
                    errors.append(f"{relative}: broken local Markdown link: {target}")

    for required in (
        "README.md",
        "LICENSE",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CHANGELOG.md",
        "THIRD_PARTY_NOTICES.md",
        "docs/publication-checklist.md",
        ".github/workflows/ci.yml",
    ):
        if not (ROOT / required).exists():
            errors.append(f"missing public repository file: {required}")

    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        return 1
    print("Publication checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
