"""Fail CI when public repository metadata or documentation regresses."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".ps1", ".toml", ".yml", ".yaml", ".txt"}
EXCLUDED_PREFIXES = (
    ".git/",
    ".venv/",
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

    absolute_user_path = re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE)
    private_ipv4 = re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|"
        r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
    )
    secret_assignment = re.compile(
        r"(?im)^\s*(?:M59_ACCOUNT_PASSWORD|M59_BOT_CONTROL_TOKEN|M59_LLM_API_KEY|M59_VLLM_API_KEY)\s*=\s*\S+"
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
