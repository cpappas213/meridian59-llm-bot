from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SECRET_KEY_RE = re.compile(r"password|secret|token|api[_-]?key|credential", re.I)


def now_utc() -> datetime:
    return datetime.now(UTC)


def timestamp() -> str:
    return now_utc().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def uuid7() -> str:
    """Return an RFC 9562 UUIDv7 without requiring Python 3.14."""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = random.SystemRandom().getrandbits(12)
    rand_b = random.SystemRandom().getrandbits(62)
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return str(uuid.UUID(int=value))


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if SECRET_KEY_RE.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


def contains_secret(text: str, secrets: dict[str, str]) -> bool:
    lowered = text.casefold()
    for key, value in secrets.items():
        if SECRET_KEY_RE.search(key) and len(value) >= 4 and value.casefold() in lowered:
            return True
    return False


def deep_get(value: Any, dotted: str, default: Any = None) -> Any:
    current = value
    for part in dotted.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return default
    return current


def expand_path(raw: str, *, base: Path | None = None) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(raw))
    # Python's expandvars does not expand Windows %NAME% notation on every host.
    expanded = re.sub(
        r"%([^%]+)%",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        expanded,
    )
    path = Path(expanded)
    if not path.is_absolute() and base is not None:
        path = base / path
    return path.resolve()


def ensure_under(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes configured root: {candidate}") from exc
    return candidate


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("model response must be one JSON object")
    return value
