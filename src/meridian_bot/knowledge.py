from __future__ import annotations

import copy
import hashlib
import html
import json
import os
import re
import sqlite3
import tempfile
import threading
from collections import defaultdict, deque
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from .config import BotConfig
from .contracts import parse_ability_metric
from .utils import canonical_json, deep_get, timestamp


KNOWLEDGE_TOOL_NAME = "knowledge_search"
INDEX_VERSION = 5
ENTITY_KINDS = (
    "location",
    "region",
    "spell",
    "skill",
    "creature",
    "npc",
    "merchant",
    "item",
    "weapon",
    "armor",
    "reagent",
    "guide",
)

NEW_PLAYER_DOCTRINE = {
    "progression": {
        "max_hp_is_level": True,
        "eligible_victim_rule": "A kill only rolls for max-HP progression when the victim's level exceeds current max HP.",
        "candidate_warning": "An eligible creature can grant progression; eligibility is not evidence the encounter is survivable.",
    },
    "pvp": {
        "guide_protection_until_max_hp": 30,
        "note": "Assume new-player PvP protection below 30 unless fresh ordinary-client evidence proves this server differs.",
    },
    "survival": {
        "death_drops_knapsack": True,
        "bank_or_storage_survives_death": True,
        "equipment_must_be_worn_or_wielded": True,
        "normal_armor_slots": ["hands", "pants", "shield", "body"],
        "underworld_recovery": "Inspect portal destinations; if the preferred portal is unreachable, use another functioning portal and travel overland.",
    },
    "sources": [
        "https://www.meridian59.com/guides/getting-started/",
        "https://www.meridian59.com/guides/skills-and-spells/",
        "https://www.meridian59.com/guides/your-goods/",
        "https://meridian59.wiki.gg/wiki/How_to_increase_your_Max_HP_%28i.e._how_to_level_up%29",
    ],
}


class KnowledgeValidationError(ValueError):
    code = "KNOWLEDGE_VALIDATION_FAILED"

    def __init__(self, result: dict[str, Any]):
        self.result = result
        errors = result.get("errors", [])
        summary = "; ".join(str(item.get("message", "invalid game reference")) for item in errors[:5])
        super().__init__(summary or "goal failed Meridian 59 knowledge validation")


def normalize(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = text.casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def camel_words(value: str) -> str:
    return " ".join(re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value)).replace("_", "-").split("-"))


def _unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = " ".join(str(value or "").split()).strip()
        key = normalize(clean)
        if clean and key and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def _effective_class_field(
    classes: dict[str, Any],
    class_name: str,
    field_name: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve a KOD field through the class chain, most-derived first."""

    current = classes.get(str(class_name).casefold())
    if not isinstance(current, dict):
        return None, None
    chain = current.get("chain")
    ancestors = chain if isinstance(chain, list) and chain else [current.get("name") or class_name]
    wanted = field_name.casefold()
    for ancestor_name in ancestors:
        ancestor = classes.get(str(ancestor_name).casefold())
        if not isinstance(ancestor, dict):
            continue
        for section in ("properties", "classvars"):
            values = ancestor.get(section)
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if str(key).casefold() != wanted or not isinstance(value, dict):
                    continue
                evidence = {
                    "declaring_class": ancestor.get("name") or ancestor_name,
                    "source_ref": (
                        f"{ancestor.get('file')}:{value.get('line')}"
                        if ancestor.get("file") and value.get("line")
                        else ancestor.get("file")
                    ),
                    "expression": value.get("expr"),
                    "value": value.get("value"),
                }
                return value, evidence
    return None, None


def _symbols(expression: Any, prefix: str) -> list[str]:
    return sorted(set(re.findall(rf"\b{re.escape(prefix)}[A-Z0-9_]+\b", str(expression or ""))))


class _PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.heading = ""
        self.description = ""
        self.text: list[str] = []
        self.citations: list[str] = []
        self._main = 0
        self._ignored = 0
        self._capture_title = False
        self._capture_h1 = False
        self._capture_cite = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "description":
            self.description = values.get("content") or ""
        if tag == "main":
            self._main += 1
        if tag in {"script", "style", "svg", "pre", "nav", "header", "footer"}:
            self._ignored += 1
        if tag == "title":
            self._capture_title = True
            self._buffer = []
        if tag == "h1" and self._main and not self._ignored:
            self._capture_h1 = True
            self._buffer = []
        classes = set((values.get("class") or "").split())
        if "cite" in classes and self._main and not self._ignored:
            self._capture_cite = True
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._capture_title:
            self.title = " ".join("".join(self._buffer).split())
            self._capture_title = False
        if tag == "h1" and self._capture_h1:
            self.heading = " ".join("".join(self._buffer).split())
            self._capture_h1 = False
        if self._capture_cite and tag in {"p", "div", "span"}:
            value = " ".join("".join(self._buffer).split())
            if value:
                self.citations.append(value)
            self._capture_cite = False
        if tag in {"script", "style", "svg", "pre", "nav", "header", "footer"} and self._ignored:
            self._ignored -= 1
        if tag == "main" and self._main:
            self._main -= 1

    def handle_data(self, data: str) -> None:
        if self._capture_title or self._capture_h1 or self._capture_cite:
            self._buffer.append(data)
        if self._main and not self._ignored:
            clean = " ".join(data.split())
            if clean:
                self.text.append(clean)


class KnowledgeBase:
    """Versioned, read-only-at-runtime Meridian 59 fact index.

    The index is built atomically from the pinned harness's generated compendium.
    It never reads a live server filesystem or an administrative endpoint.
    """

    METRIC_ALIASES = {
        "vitals.health.value": "status.vitals.health.value",
        "vitals.health.max": "status.vitals.health.max",
        "vitals.mana.value": "status.vitals.mana.value",
        "vitals.mana.max": "status.vitals.mana.max",
        "vitals.vigor.value": "status.vitals.vigor.value",
        "health.value": "status.vitals.health.value",
        "health.max": "status.vitals.health.max",
        "mana.value": "status.vitals.mana.value",
        "mana.max": "status.vitals.mana.max",
    }

    def __init__(self, config: BotConfig):
        self.config = config
        self.path = config.deployment.data_dir / "knowledge.sqlite3"
        self.root = config.harness.root / "compendium"
        self._build_lock = threading.Lock()
        self._metadata: dict[str, Any] = {}
        self.ensure_ready()

    @property
    def available(self) -> bool:
        return bool(int(self._metadata.get("entity_count", 0) or 0))

    @property
    def corpus_version(self) -> str:
        return str(self._metadata.get("corpus_version", "unavailable"))

    def planner_tool(self) -> dict[str, Any]:
        return {
            "name": KNOWLEDGE_TOOL_NAME,
            "description": (
                "Search the source-derived Meridian 59 knowledge index without moving the character. "
                "Use this before relying on an ungrounded room, spell, skill, creature, NPC, item, or mechanic name. "
                "A zero-match result is authoritative negative evidence for this pinned corpus; do not repeat it unchanged."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "description": "Fact or exact game name to find."},
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(ENTITY_KINDS)},
                        "uniqueItems": True,
                        "description": "Optional entity-type filter.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }

    def validate_tool_arguments(self, arguments: dict[str, Any]) -> None:
        unknown = set(arguments) - {"query", "kinds", "limit"}
        if unknown:
            raise ValueError("unknown knowledge_search argument(s): " + ", ".join(sorted(unknown)))
        if not isinstance(arguments.get("query"), str) or not arguments["query"].strip():
            raise ValueError("knowledge_search.query is required")
        kinds = arguments.get("kinds")
        if kinds is not None and (
            not isinstance(kinds, list) or any(kind not in ENTITY_KINDS for kind in kinds)
        ):
            raise ValueError("knowledge_search.kinds contains an unsupported entity kind")
        limit = arguments.get("limit", 5)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            raise ValueError("knowledge_search.limit must be an integer from 1 to 10")

    def ensure_ready(self, *, force: bool = False) -> dict[str, Any]:
        with self._build_lock:
            files = self._source_files()
            manifest = self._manifest(files)
            current = self._read_metadata(self.path)
            if not force and current.get("manifest") == manifest and self.path.is_file():
                self._metadata = current
                return current
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, raw_temp = tempfile.mkstemp(prefix="knowledge-", suffix=".sqlite3", dir=self.path.parent)
            os.close(fd)
            temporary = Path(raw_temp)
            try:
                metadata = self._build(temporary, files, manifest)
                os.replace(temporary, self.path)
                self._metadata = metadata
                return metadata
            finally:
                if temporary.exists():
                    temporary.unlink()

    def _source_files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        files: list[Path] = []
        for relative in ("data/zones.json", "data/koddb.json", "data/spawns.json", "creatures.json"):
            candidate = self.root / relative
            if candidate.is_file():
                files.append(candidate)
        for directory in (
            "zones",
            "spells",
            "skills",
            "items",
            "weapons",
            "armor",
            "creatures",
            "npcs",
            "reagents",
            "guides",
            "content",
        ):
            root = self.root / directory
            if root.is_dir():
                files.extend(path for path in root.glob("*.html") if path.name.casefold() != "index.html")
        merchant_catalogue = self.config.harness.root / "substrate" / "m59-merchants.json"
        if merchant_catalogue.is_file():
            files.append(merchant_catalogue)
        return sorted(set(files), key=lambda path: str(path).casefold())

    def _manifest(self, files: list[Path]) -> str:
        digest = hashlib.sha256()
        digest.update(f"knowledge-index-v{INDEX_VERSION}:".encode("ascii"))
        digest.update(self.config.harness.expected_revision.encode("utf-8"))
        for path in files:
            try:
                source_name = str(path.relative_to(self.root)).replace("\\", "/")
            except ValueError:
                source_name = "harness/" + str(
                    path.relative_to(self.config.harness.root)
                ).replace("\\", "/")
            digest.update(source_name.encode("utf-8"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()

    @staticmethod
    def _connect_path(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=20)
        connection.row_factory = sqlite3.Row
        return connection

    def _connect(self) -> sqlite3.Connection:
        return self._connect_path(self.path)

    @staticmethod
    def _read_metadata(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            connection = KnowledgeBase._connect_path(path)
            try:
                return {row["key"]: json.loads(row["value"]) for row in connection.execute("SELECT key,value FROM metadata")}
            finally:
                connection.close()
        except (OSError, sqlite3.Error, json.JSONDecodeError):
            return {}

    def _build(self, path: Path, files: list[Path], manifest: str) -> dict[str, Any]:
        connection = self._connect_path(path)
        try:
            connection.executescript(
                """
                PRAGMA journal_mode=OFF;
                PRAGMA synchronous=OFF;
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE entities(
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    canonical_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    aliases_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_tier TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    source_hash TEXT NOT NULL
                );
                CREATE INDEX entities_kind_name ON entities(kind, normalized_name);
                CREATE TABLE aliases(
                    alias_norm TEXT NOT NULL,
                    entity_id TEXT NOT NULL REFERENCES entities(id),
                    alias TEXT NOT NULL,
                    UNIQUE(alias_norm, entity_id)
                );
                CREATE INDEX aliases_lookup ON aliases(alias_norm);
                CREATE TABLE relations(
                    subject_id TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(subject_id, predicate, object_id)
                );
                CREATE INDEX relations_subject ON relations(subject_id, predicate);
                CREATE INDEX relations_object ON relations(object_id, predicate);
                """
            )
            fts = True
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE entity_fts USING fts5(entity_id UNINDEXED, canonical_name, aliases, summary, content)"
                )
            except sqlite3.OperationalError:
                fts = False

            slug_index: dict[tuple[str, str], str] = {}
            class_index: dict[str, str] = {}
            zones_path = self.root / "data" / "zones.json"
            if zones_path.is_file():
                self._load_zones(connection, zones_path, slug_index, class_index)
            creatures_path = self.root / "creatures.json"
            if creatures_path.is_file():
                self._load_creatures(connection, creatures_path, slug_index)
            for page in (file for file in files if file.suffix.casefold() == ".html"):
                self._load_page(connection, page, slug_index)
            merchants_path = self.config.harness.root / "substrate" / "m59-merchants.json"
            if merchants_path.is_file():
                self._load_merchants(connection, merchants_path)
            spawns_path = self.root / "data" / "spawns.json"
            if spawns_path.is_file():
                self._load_spawns(connection, spawns_path)

            if fts:
                connection.execute(
                    """INSERT INTO entity_fts(entity_id,canonical_name,aliases,summary,content)
                       SELECT id,canonical_name,aliases_json,summary,content FROM entities"""
                )
            entity_count = int(connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0])
            corpus = f"{self.config.harness.expected_revision[:12] or 'unversioned'}-{manifest[:12]}"
            metadata: dict[str, Any] = {
                "manifest": manifest,
                "index_version": INDEX_VERSION,
                "corpus_version": corpus,
                "harness_revision": self.config.harness.expected_revision or None,
                "built_at": timestamp(),
                "source_root": str(self.root),
                "source_files": len(files),
                "entity_count": entity_count,
                "fts_enabled": fts,
                "source_policy": "public source-derived harness compendium; live ordinary-client observations override current state",
            }
            connection.executemany(
                "INSERT INTO metadata(key,value) VALUES(?,?)",
                [(key, canonical_json(value)) for key, value in metadata.items()],
            )
            connection.commit()
            return metadata
        finally:
            connection.close()

    def _upsert_entity(
        self,
        connection: sqlite3.Connection,
        *,
        entity_id: str,
        kind: str,
        name: str,
        aliases: Iterable[Any],
        summary: str,
        content: str,
        payload: dict[str, Any],
        source_ref: str,
        source_hash: str,
        source_tier: str = "harness_source",
    ) -> None:
        aliases_clean = _unique([name, *aliases])
        prior = connection.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        if prior:
            aliases_clean = _unique([*json.loads(prior["aliases_json"]), *aliases_clean])
            old_payload = json.loads(prior["payload_json"])
            old_payload.update(payload)
            payload = old_payload
            summary = summary or prior["summary"]
            content = " ".join(part for part in (prior["content"], content) if part)[:60_000]
            source_ref = " | ".join(_unique([prior["source_ref"], source_ref]))
            connection.execute("DELETE FROM aliases WHERE entity_id=?", (entity_id,))
        connection.execute(
            """INSERT INTO entities(id,kind,canonical_name,normalized_name,aliases_json,summary,content,payload_json,source_tier,source_ref,source_hash)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 kind=excluded.kind, canonical_name=excluded.canonical_name, normalized_name=excluded.normalized_name,
                 aliases_json=excluded.aliases_json, summary=excluded.summary, content=excluded.content,
                 payload_json=excluded.payload_json, source_tier=excluded.source_tier,
                 source_ref=excluded.source_ref, source_hash=excluded.source_hash""",
            (
                entity_id,
                kind,
                name,
                normalize(name),
                canonical_json(aliases_clean),
                " ".join(summary.split())[:2_000],
                " ".join(content.split())[:60_000],
                canonical_json(payload),
                source_tier,
                source_ref[:4_000],
                source_hash,
            ),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO aliases(alias_norm,entity_id,alias) VALUES(?,?,?)",
            [(normalize(alias), entity_id, alias) for alias in aliases_clean if normalize(alias)],
        )

    def _load_zones(
        self,
        connection: sqlite3.Connection,
        path: Path,
        slug_index: dict[tuple[str, str], str],
        class_index: dict[str, str],
    ) -> None:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8-sig"))
        data = document.get("rooms", document) if isinstance(document, dict) else {}
        koddb_path = path.with_name("koddb.json")
        koddb_raw = b""
        classes: dict[str, Any] = {}
        if koddb_path.is_file():
            koddb_raw = koddb_path.read_bytes()
            koddb_document = json.loads(koddb_raw.decode("utf-8-sig"))
            raw_classes = koddb_document.get("classes", {}) if isinstance(koddb_document, dict) else {}
            if isinstance(raw_classes, dict):
                classes = {str(key).casefold(): value for key, value in raw_classes.items()}
        combined_hash = hashlib.sha256(raw + b"\0" + koddb_raw).hexdigest()
        pending_relations: list[tuple[str, str]] = []
        regions: set[str] = set()
        for class_name, room in data.items():
            if not isinstance(room, dict):
                continue
            room_id = room.get("ridValue")
            slug = str(room.get("slug") or class_name).casefold()
            name = str(room.get("disp") or room.get("name") or camel_words(class_name))
            entity_id = f"location:{room_id}" if room_id is not None else f"location:{slug}"
            aliases = [
                room.get("name"),
                room.get("disp"),
                class_name,
                camel_words(class_name),
                slug,
                room.get("rid"),
                room_id,
            ]
            exits = [
                {
                    key: exit_value.get(key)
                    for key in ("kind", "to", "toRid", "fromRow", "fromCol", "row", "col", "dir")
                    if exit_value.get(key) is not None
                }
                for exit_value in room.get("exits", [])
                if isinstance(exit_value, dict)
            ]
            source = str(room.get("file") or "compendium/data/zones.json")
            if room.get("line"):
                source += f":{room['line']}"
            permanent_field, flag_evidence = _effective_class_field(
                classes, class_name, "viPermanent_flags"
            )
            terrain_field, terrain_evidence = _effective_class_field(
                classes, class_name, "viTerrain_type"
            )
            declared_flags = [str(value) for value in room.get("flags", []) if str(value)]
            effective_permanent_flags = _symbols(
                permanent_field.get("expr") if permanent_field else None,
                "ROOM_",
            )
            flags = sorted(set(declared_flags) | set(effective_permanent_flags))
            declared_terrain = [str(value) for value in room.get("terrain", []) if str(value)]
            effective_terrain = _symbols(
                terrain_field.get("expr") if terrain_field else None,
                "TERRAIN_",
            )
            terrain = sorted(set(declared_terrain) | set(effective_terrain))
            source_refs = [source]
            for field_evidence in (flag_evidence, terrain_evidence):
                if field_evidence and field_evidence.get("source_ref"):
                    source_refs.append(str(field_evidence["source_ref"]))
            source = " | ".join(_unique(source_refs))
            summary_parts = [name]
            if room.get("region"):
                summary_parts.append(f"Region: {room['region']}.")
                regions.add(str(room["region"]))
            summary_parts.append(f"Room id: {room_id}." if room_id is not None else "")
            if exits:
                summary_parts.append(f"Connects through {len(exits)} declared exits.")
            if flags:
                summary_parts.append("Permanent room rules: " + ", ".join(flags) + ".")
            payload = {
                "room_id": room_id,
                "class": class_name,
                "slug": slug,
                "name": room.get("name"),
                "display_name": room.get("disp"),
                "region": room.get("region"),
                "rid": room.get("rid"),
                "terrain": terrain,
                "flags": flags,
                "declared_flags": declared_flags,
                "effective_permanent_flags": effective_permanent_flags,
                "flag_evidence": flag_evidence,
                "terrain_evidence": terrain_evidence,
                "dimensions": room.get("dims", {}),
                "teleport": room.get("teleport", {}),
                "monsters": room.get("monsters", []),
                "exits": exits,
            }
            self._upsert_entity(
                connection,
                entity_id=entity_id,
                kind="location",
                name=name,
                aliases=aliases,
                summary=" ".join(part for part in summary_parts if part),
                content=" ".join(
                    str(value)
                    for value in [*aliases, *flags, *terrain]
                    if value is not None
                ),
                payload=payload,
                source_ref=source,
                source_hash=combined_hash,
            )
            slug_index[("location", slug)] = entity_id
            class_index[class_name.casefold()] = entity_id
            if room.get("region"):
                pending_relations.append((entity_id, f"region:{normalize(room['region']).replace(' ', '-') }"))
            for exit_value in exits:
                if exit_value.get("to"):
                    pending_relations.append((entity_id, str(exit_value["to"])))

        for region in regions:
            region_id = f"region:{normalize(region).replace(' ', '-')}"
            self._upsert_entity(
                connection,
                entity_id=region_id,
                kind="region",
                name=region,
                aliases=[region],
                summary=f"Meridian 59 region: {region}.",
                content=region,
                payload={"region": region},
                source_ref="compendium/data/zones.json",
                source_hash=hashlib.sha256(raw).hexdigest(),
            )
        for subject, raw_object in pending_relations:
            if raw_object.startswith("region:"):
                object_id = raw_object
                predicate = "located_in"
            else:
                object_id = class_index.get(raw_object.casefold())
                predicate = "connects_to"
            if object_id:
                connection.execute(
                    "INSERT OR IGNORE INTO relations(subject_id,predicate,object_id,payload_json) VALUES(?,?,?,?)",
                    (subject, predicate, object_id, "{}"),
                )

    def _load_creatures(
        self,
        connection: sqlite3.Connection,
        path: Path,
        slug_index: dict[tuple[str, str], str],
    ) -> None:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
        source_hash = hashlib.sha256(raw).hexdigest()
        for creature in data.get("beasts", []) if isinstance(data, dict) else []:
            if not isinstance(creature, dict):
                continue
            slug = str(creature.get("slug") or normalize(creature.get("name")).replace(" ", "-"))
            name = str(creature.get("name") or camel_words(slug))
            entity_id = f"creature:{slug}"
            where = creature.get("where", []) if isinstance(creature.get("where"), list) else []
            summary = f"{name}; level {creature.get('level')}; role {creature.get('role') or 'unknown'}"
            if where:
                summary += "; found in " + ", ".join(str(value) for value in where[:8])
            payload = {
                key: creature.get(key)
                for key in ("slug", "name", "level", "difficulty", "karma", "speed", "treasure", "faction", "role", "where")
            }
            self._upsert_entity(
                connection,
                entity_id=entity_id,
                kind="creature",
                name=name,
                aliases=[slug, creature.get("koc")],
                summary=summary,
                content=summary,
                payload=payload,
                source_ref="compendium/creatures.json",
                source_hash=source_hash,
            )
            slug_index[("creature", slug.casefold())] = entity_id
        for weapon in data.get("weapons", []) if isinstance(data, dict) else []:
            if isinstance(weapon, dict):
                self._load_equipment(connection, weapon, "weapon", source_hash, slug_index)
        armour = data.get("armour", {}) if isinstance(data, dict) else {}
        if isinstance(armour, dict):
            for slot, rows in armour.items():
                for item in rows if isinstance(rows, list) else []:
                    if isinstance(item, dict):
                        self._load_equipment(connection, {**item, "slot": slot}, "armor", source_hash, slug_index)

    def _load_equipment(
        self,
        connection: sqlite3.Connection,
        item: dict[str, Any],
        kind: str,
        source_hash: str,
        slug_index: dict[tuple[str, str], str],
    ) -> None:
        slug = str(item.get("slug") or normalize(item.get("name")).replace(" ", "-"))
        name = str(item.get("name") or camel_words(slug))
        entity_id = f"{kind}:{slug}"
        summary = f"{name}; {kind}; value {item.get('value', 'unknown')}"
        self._upsert_entity(
            connection,
            entity_id=entity_id,
            kind=kind,
            name=name,
            aliases=[slug, item.get("cls"), camel_words(str(item.get("cls") or ""))],
            summary=summary,
            content=summary,
            payload=item,
            source_ref="compendium/creatures.json",
            source_hash=source_hash,
        )
        slug_index[(kind, slug.casefold())] = entity_id

    def _load_page(
        self,
        connection: sqlite3.Connection,
        path: Path,
        slug_index: dict[tuple[str, str], str],
    ) -> None:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig", errors="replace")
        parser = _PageText()
        parser.feed(text)
        folder = path.parent.name.casefold()
        kind_by_folder = {
            "zones": "location",
            "spells": "spell",
            "skills": "skill",
            "items": "item",
            "weapons": "weapon",
            "armor": "armor",
            "creatures": "creature",
            "npcs": "npc",
            "reagents": "reagent",
            "guides": "guide",
            "content": "guide",
        }
        kind = kind_by_folder.get(folder)
        if not kind:
            return
        slug = path.stem.casefold()
        name = parser.heading or parser.title.split("—", 1)[0].strip() or camel_words(slug)
        entity_id = slug_index.get((kind, slug), f"{kind}:{slug}")
        aliases = [slug, camel_words(slug)]
        content = " ".join(parser.text)
        summary = parser.description or content[:800]
        source_ref = str(path.relative_to(self.root)).replace("\\", "/")
        if parser.citations:
            source_ref += " | " + " | ".join(parser.citations[:8])
        self._upsert_entity(
            connection,
            entity_id=entity_id,
            kind=kind,
            name=name,
            aliases=aliases,
            summary=summary,
            content=content,
            payload={"slug": slug, "page": str(path.relative_to(self.root)).replace("\\", "/")},
            source_ref=source_ref,
            source_hash=hashlib.sha256(raw).hexdigest(),
        )
        slug_index[(kind, slug)] = entity_id

    def _load_merchants(self, connection: sqlite3.Connection, path: Path) -> None:
        """Index merchant placement and stock as a verifiable relation.

        The generated catalogue deliberately includes source-only merchant
        classes with null ids/rooms.  Keeping those entries is important: they
        are authoritative negative evidence, not destinations for the planner
        to guess from a city name.
        """
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8-sig"))
        rows = document.get("merchants", []) if isinstance(document, dict) else []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and str(row.get("cls") or "").strip():
                grouped.setdefault(str(row["cls"]).strip(), []).append(row)
        source_hash = hashlib.sha256(raw).hexdigest()
        for merchant_class, records in sorted(grouped.items(), key=lambda item: item[0].casefold()):
            entity_id = "merchant:" + normalize(merchant_class).replace(" ", "-")
            instances: list[dict[str, Any]] = []
            room_ids: set[int] = set()
            stock_by_class: dict[str, dict[str, Any]] = {}
            teaches: list[str] = []
            source_refs = ["harness/substrate/m59-merchants.json"]
            notes: list[str] = []
            markups: list[Any] = []
            buying_rules: list[dict[str, Any]] = []
            buying_categories: set[str] = set()
            buys_anything = False
            for record in records:
                room_id = record.get("room")
                seen = bool(record.get("seen"))
                placed = seen and isinstance(room_id, int) and not isinstance(room_id, bool)
                room_name = None
                if placed:
                    location = connection.execute(
                        "SELECT canonical_name FROM entities WHERE id=?",
                        (f"location:{room_id}",),
                    ).fetchone()
                    room_name = location["canonical_name"] if location else None
                    room_ids.add(room_id)
                instances.append(
                    {
                        "seller_id_at_build": record.get("id"),
                        "room_id": room_id,
                        "room_name": room_name,
                        "seen": seen,
                        "placed": placed,
                    }
                )
                if record.get("markup") is not None:
                    markups.append(record.get("markup"))
                if record.get("note"):
                    notes.append(str(record["note"]))
                if record.get("source"):
                    source_refs.append(str(record["source"]))
                buying_rule = record.get("buying_rule")
                if isinstance(buying_rule, dict):
                    if buying_rule.get("source"):
                        source_refs.append(str(buying_rule["source"]))
                    normalized_rule = {
                        "source": buying_rule.get("source"),
                        "kod": str(buying_rule.get("kod") or "")[:4000] or None,
                    }
                    if canonical_json(normalized_rule) not in {
                        canonical_json(value) for value in buying_rules
                    }:
                        buying_rules.append(normalized_rule)
                    for category in re.findall(
                        r"@IsObject([A-Za-z0-9_]+)",
                        str(buying_rule.get("kod") or ""),
                        flags=re.IGNORECASE,
                    ):
                        buying_categories.add(camel_words(category))
                buys_anything = buys_anything or record.get("buys_anything") is True
                for taught in record.get("teaches", []) if isinstance(record.get("teaches"), list) else []:
                    if isinstance(taught, dict):
                        teaches.extend(
                            str(taught[key]) for key in ("spell", "skill") if taught.get(key)
                        )
                for stock in record.get("sells", []) if isinstance(record.get("sells"), list) else []:
                    if not isinstance(stock, dict) or not str(stock.get("cls") or "").strip():
                        continue
                    stock_class = str(stock["cls"]).strip()
                    item_row = self._preferred_item_row(connection, stock_class)
                    item_id = item_row["id"] if item_row else None
                    item_name = camel_words(stock_class)
                    base_value = None
                    if item_row:
                        item_name = item_row["canonical_name"]
                        item_payload = json.loads(item_row["payload_json"])
                        base_value = item_payload.get("value")
                    current = stock_by_class.setdefault(
                        normalize(stock_class),
                        {
                            "class": stock_class,
                            "name": item_name,
                            "quantity": stock.get("quantity"),
                            "base_value": base_value,
                            "estimated_price": None,
                            "price_verification": "fresh in-room shop quote required",
                            "entity_id": item_id,
                        },
                    )
                    if current.get("quantity") is None and stock.get("quantity") is not None:
                        current["quantity"] = stock.get("quantity")
            available = bool(room_ids)
            placement = "instantiated" if available else "source_only_unplaced"
            name = camel_words(merchant_class)
            summary = (
                f"{name}; merchant class {merchant_class}; {placement}; "
                + (
                    "rooms " + ", ".join(str(value) for value in sorted(room_ids))
                    if available
                    else "UNAVAILABLE: no live-world instance or room in the generated catalogue"
                )
                + "; sells "
                + (", ".join(value["name"] for value in stock_by_class.values()) or "nothing indexed")
                + "; buys "
                + (
                    "anything the NPC accepts"
                    if buys_anything
                    else (
                        ", ".join(sorted(buying_categories))
                        if buying_categories
                        else (
                            "items governed by an unclassified source rule; verify with a live quote"
                            if buying_rules
                            else "no indexed categories"
                        )
                    )
                )
            )
            payload = {
                "merchant_class": merchant_class,
                "merchant": True,
                "available": available,
                "placed": available,
                "placement_status": placement,
                "unavailable_reason": None
                if available
                else "defined in source but no merchant instance is placed in the world",
                "instances": instances,
                "room_ids": sorted(room_ids),
                "sells": list(stock_by_class.values()),
                "stock_classes": [value["class"] for value in stock_by_class.values()],
                "teaches": _unique(teaches),
                "catalog_markup": _unique(markups),
                "buys_anything": buys_anything,
                "buying_categories": sorted(buying_categories),
                "buying_rules": buying_rules,
                "sale_verification": (
                    "Use a fresh in-room sell quote with confirm=false; the source rule narrows candidates but does not prove a specific carried item will be bought."
                ),
                "price_verification": "fresh in-room shop quote required",
                "notes": _unique(notes),
            }
            self._upsert_entity(
                connection,
                entity_id=entity_id,
                kind="merchant",
                name=name,
                aliases=[merchant_class, camel_words(merchant_class)],
                summary=summary,
                content=" ".join([summary, *notes]),
                payload=payload,
                source_ref=" | ".join(_unique(source_refs)),
                source_hash=source_hash,
            )
            for room_id in sorted(room_ids):
                location_id = f"location:{room_id}"
                if connection.execute("SELECT 1 FROM entities WHERE id=?", (location_id,)).fetchone():
                    connection.execute(
                        "INSERT OR IGNORE INTO relations(subject_id,predicate,object_id,payload_json) VALUES(?,?,?,?)",
                        (entity_id, "located_in", location_id, "{}"),
                    )
            for stock in stock_by_class.values():
                if stock.get("entity_id"):
                    connection.execute(
                        "INSERT OR IGNORE INTO relations(subject_id,predicate,object_id,payload_json) VALUES(?,?,?,?)",
                        (
                            entity_id,
                            "sells",
                            stock["entity_id"],
                            canonical_json(
                                {
                                    key: stock.get(key)
                                    for key in (
                                        "class",
                                        "quantity",
                                        "base_value",
                                        "estimated_price",
                                        "price_verification",
                                    )
                                }
                            ),
                        ),
                    )

    def _load_spawns(self, connection: sqlite3.Connection, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        by_monster = data.get("byMonster", {}) if isinstance(data, dict) else {}
        if not isinstance(by_monster, dict):
            return
        for monster, rows in by_monster.items():
            # Code class names are CamelCase while many compendium slugs are
            # the same text collapsed to lowercase (SpiderBaby -> spiderbaby).
            # The human-facing canonical name may reverse those words (baby
            # spider), so try the literal class and then its collapsed slug.
            # Both lookups remain exact and must resolve uniquely.
            subject = self._exact_entity_id(connection, str(monster), {"creature"})
            if not subject:
                subject = self._exact_entity_id(
                    connection, str(monster).casefold(), {"creature"}
                )
            if not subject:
                continue
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                room_value = row.get("room", row.get("roomNum", row.get("rid")))
                object_id = self._exact_entity_id(connection, str(room_value), {"location"}) if room_value is not None else None
                if object_id:
                    connection.execute(
                        "INSERT OR IGNORE INTO relations(subject_id,predicate,object_id,payload_json) VALUES(?,?,?,?)",
                        (subject, "spawns_in", object_id, canonical_json(row)),
                    )

    @staticmethod
    def _kind_filter(kinds: Iterable[str] | None) -> tuple[list[str], str, list[Any]]:
        clean = [str(kind) for kind in (kinds or []) if str(kind) in ENTITY_KINDS]
        if not clean:
            return [], "", []
        placeholders = ",".join("?" for _ in clean)
        return clean, f" AND e.kind IN ({placeholders})", clean

    def _entity(self, row: sqlite3.Row, *, include_content: bool = False) -> dict[str, Any]:
        value = {
            "id": row["id"],
            "kind": row["kind"],
            "canonical_name": row["canonical_name"],
            "aliases": json.loads(row["aliases_json"]),
            "summary": row["summary"],
            "facts": json.loads(row["payload_json"]),
            "evidence": {
                "source_tier": row["source_tier"],
                "source_ref": row["source_ref"],
                "source_hash": row["source_hash"],
                "corpus_version": self.corpus_version,
            },
        }
        if include_content:
            value["content"] = row["content"]
        return value

    def _exact_entity_id(self, connection: sqlite3.Connection, query: str, kinds: set[str] | None = None) -> str | None:
        _, clause, params = self._kind_filter(kinds)
        rows = connection.execute(
            f"""SELECT DISTINCT e.id FROM aliases a JOIN entities e ON e.id=a.entity_id
                WHERE a.alias_norm=?{clause} LIMIT 2""",
            [normalize(query), *params],
        ).fetchall()
        return rows[0]["id"] if len(rows) == 1 else None

    @staticmethod
    def _preferred_item_row(
        connection: sqlite3.Connection, query: str
    ) -> sqlite3.Row | None:
        """Collapse duplicate generic-item/equipment pages for one game class.

        The compendium intentionally has both ``item:leatherarmor`` and the
        richer ``armor:leatherarmor`` page with the same canonical object.  That
        is not a meaningful ambiguity for a merchant stock relation; prefer the
        typed equipment/reagent entity while preserving real name conflicts.
        """
        rows = connection.execute(
            """SELECT DISTINCT e.* FROM aliases a JOIN entities e ON e.id=a.entity_id
               WHERE a.alias_norm=? AND e.kind IN ('armor','weapon','reagent','item')""",
            (normalize(query),),
        ).fetchall()
        if not rows or len({normalize(row["canonical_name"]) for row in rows}) != 1:
            return None
        priority = {"armor": 0, "weapon": 1, "reagent": 2, "item": 3}
        return min(rows, key=lambda row: (priority.get(row["kind"], 9), row["id"]))

    def resolve(
        self,
        query: str,
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 8,
        allow_fuzzy: bool = False,
    ) -> dict[str, Any]:
        query = " ".join(str(query).split())
        if not query:
            raise ValueError("query is required")
        if not self.available:
            return {"status": "unavailable", "query": query, "matches": [], "corpus": self.metadata()}
        _, clause, params = self._kind_filter(kinds)
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""SELECT DISTINCT e.* FROM aliases a JOIN entities e ON e.id=a.entity_id
                    WHERE a.alias_norm=?{clause} ORDER BY e.kind,e.canonical_name LIMIT ?""",
                [normalize(query), *params, max(2, min(int(limit), 20))],
            ).fetchall()
            if len(rows) == 1:
                return {"status": "found", "query": query, "entity": self._entity(rows[0]), "matches": [self._entity(rows[0])], "corpus": self.metadata()}
            if len(rows) > 1:
                return {"status": "ambiguous", "query": query, "matches": [self._entity(row) for row in rows], "corpus": self.metadata()}
        finally:
            connection.close()
        suggestions = self.search(query, kinds=kinds, limit=limit).get("matches", [])
        if allow_fuzzy and len(suggestions) == 1:
            candidate = suggestions[0]
            ratio = SequenceMatcher(None, normalize(query), normalize(candidate["canonical_name"])).ratio()
            if ratio >= 0.88:
                return {"status": "found_fuzzy", "query": query, "entity": candidate, "matches": suggestions, "corpus": self.metadata()}
        return {"status": "not_found", "query": query, "matches": suggestions, "corpus": self.metadata()}

    def item_valuation(self, query: str) -> dict[str, Any]:
        """Return a source-derived base value for one carried item name.

        Equipment and generic item pages can share the same alias. Reuse the
        catalogue's preferred typed row so valuation does not become falsely
        ambiguous merely because both pages exist.
        """
        query = " ".join(str(query).split())
        if not query or not self.available:
            return {
                "status": "unavailable" if not self.available else "not_found",
                "query": query,
                "unit_value": None,
            }
        connection = self._connect()
        try:
            row = self._preferred_item_row(connection, query)
            if row is None:
                return {"status": "not_found", "query": query, "unit_value": None}
            payload = json.loads(row["payload_json"])
            value = payload.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return {
                    "status": "value_unknown",
                    "query": query,
                    "entity_id": row["id"],
                    "canonical_name": row["canonical_name"],
                    "unit_value": None,
                    "source_ref": row["source_ref"],
                }
            return {
                "status": "valued",
                "query": query,
                "entity_id": row["id"],
                "canonical_name": row["canonical_name"],
                "unit_value": value,
                "source_ref": row["source_ref"],
                "basis": "source-derived base item value; live resale value may differ",
            }
        finally:
            connection.close()

    def buyer_candidates(
        self, items: list[dict[str, Any]], *, per_item_limit: int = 5
    ) -> list[dict[str, Any]]:
        """Return source-grounded candidate buyers without claiming a live sale."""
        if not self.available:
            return []
        connection = self._connect()
        try:
            merchant_rows = connection.execute(
                "SELECT * FROM entities WHERE kind='merchant' ORDER BY canonical_name"
            ).fetchall()
            merchants = []
            for row in merchant_rows:
                facts = json.loads(row["payload_json"])
                if facts.get("available") is not True:
                    continue
                merchants.append((row, facts))
            values: list[dict[str, Any]] = []
            for raw_item in items[:30]:
                name = " ".join(str(raw_item.get("name") or "").split())
                if not name or "shilling" in name.casefold():
                    continue
                item_row = self._preferred_item_row(connection, name)
                item_kind = str(item_row["kind"]) if item_row is not None else None
                category = {
                    "armor": "Wearable",
                    "weapon": "Weapon",
                    "reagent": "Reagent",
                }.get(item_kind or "")
                candidates: list[dict[str, Any]] = []
                for merchant_row, facts in merchants:
                    categories = [
                        str(value)
                        for value in facts.get("buying_categories", [])
                        if str(value)
                    ]
                    accepts_by_source = facts.get("buys_anything") is True or (
                        category is not None
                        and category.casefold()
                        in {value.casefold() for value in categories}
                    )
                    if not accepts_by_source:
                        continue
                    candidates.append(
                        {
                            "merchant": facts.get("merchant_class")
                            or merchant_row["canonical_name"],
                            "room_ids": facts.get("room_ids", []),
                            "buys_anything": facts.get("buys_anything") is True,
                            "matched_category": category,
                            "buying_categories": categories,
                            "verification": facts.get("sale_verification"),
                            "entity_id": merchant_row["id"],
                        }
                    )
                values.append(
                    {
                        "item": name,
                        "item_kind": item_kind,
                        "inferred_source_category": category,
                        "candidates": candidates[: max(1, min(int(per_item_limit), 10))],
                        "next_evidence": (
                            "Use merchants with buys=<exact carried item> and then sell confirm=false. "
                            "Source categories narrow candidates; only the live quote proves acceptance."
                        ),
                    }
                )
            return values
        finally:
            connection.close()

    def search(self, query: str, *, kinds: Iterable[str] | None = None, limit: int = 8) -> dict[str, Any]:
        query = " ".join(str(query).split())
        if not query:
            raise ValueError("query is required")
        limit = max(1, min(int(limit), 20))
        if not self.available:
            return {"query": query, "matches": [], "count": 0, "corpus": self.metadata(), "status": "unavailable"}
        _, clause, params = self._kind_filter(kinds)
        connection = self._connect()
        try:
            rows: list[sqlite3.Row] = []
            exact = connection.execute(
                f"""SELECT DISTINCT e.* FROM aliases a JOIN entities e ON e.id=a.entity_id
                    WHERE a.alias_norm=?{clause} ORDER BY e.kind,e.canonical_name LIMIT ?""",
                [normalize(query), *params, limit],
            ).fetchall()
            rows.extend(exact)
            seen = {row["id"] for row in rows}
            remaining = limit - len(rows)
            if remaining > 0 and self._metadata.get("fts_enabled"):
                tokens = [token for token in normalize(query).split() if len(token) > 1][:12]
                if tokens:
                    match = " OR ".join(f'"{token}"*' for token in tokens)
                    try:
                        fts_rows = connection.execute(
                            f"""SELECT e.* FROM entity_fts f JOIN entities e ON e.id=f.entity_id
                                WHERE entity_fts MATCH ?{clause}
                                ORDER BY bm25(entity_fts), e.kind, e.canonical_name LIMIT ?""",
                            [match, *params, remaining * 3],
                        ).fetchall()
                        rows.extend(row for row in fts_rows if row["id"] not in seen)
                    except sqlite3.OperationalError:
                        pass
            if len(rows) < limit:
                like = f"%{normalize(query)}%"
                fallback = connection.execute(
                    f"""SELECT e.* FROM entities e WHERE
                        (e.normalized_name LIKE ? OR e.aliases_json LIKE ? OR lower(e.summary) LIKE ?){clause}
                        ORDER BY e.kind,e.canonical_name LIMIT ?""",
                    [like, f"%{query.casefold()}%", f"%{query.casefold()}%", *params, limit * 2],
                ).fetchall()
                rows.extend(row for row in fallback if row["id"] not in {value["id"] for value in rows})
            values = [self._entity(row) for row in rows[:limit]]
            return {"query": query, "matches": values, "count": len(values), "corpus": self.metadata(), "status": "ok"}
        finally:
            connection.close()

    def get(self, entity_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM entities WHERE id=?", (str(entity_id),)).fetchone()
            if row is None:
                return {"status": "not_found", "entity_id": entity_id, "corpus": self.metadata()}
            entity = self._entity(row, include_content=True)
            relations = connection.execute(
                """SELECT r.predicate,r.payload_json,e.id,e.kind,e.canonical_name
                   FROM relations r JOIN entities e ON e.id=r.object_id WHERE r.subject_id=?
                   ORDER BY r.predicate,e.canonical_name LIMIT 100""",
                (entity_id,),
            ).fetchall()
            entity["relations"] = [
                {
                    "predicate": relation["predicate"],
                    "entity": {"id": relation["id"], "kind": relation["kind"], "canonical_name": relation["canonical_name"]},
                    "facts": json.loads(relation["payload_json"]),
                }
                for relation in relations
            ]
            if entity.get("kind") == "location":
                # Spawn relations point from creature -> location, so they are
                # invisible in the ordinary outgoing relation list above.
                # Surface the complete room mix explicitly; this is the static
                # map a planner needs to distinguish a target drought from a
                # broken farm and to account for nuisance or dangerous spawns.
                entity["spawn_table"] = self._room_spawn_table(
                    connection, row, include_empty=True
                )
            return {"status": "found", "entity": entity, "corpus": self.metadata()}
        finally:
            connection.close()

    def nearest_safe_location(
        self, room_id: int, *, preferred_room_id: int | None = None
    ) -> dict[str, Any]:
        """Find a source-verified staging room without encoding a home city.

        A live observation of a safe room remains stronger evidence and is
        remembered by the controller. This lookup is the restart/failure
        fallback: it ranks safe rooms connected in the pinned source graph,
        then safe rooms in the same source region when a zone has no declared
        exit graph (as happens for some isolated areas).
        """

        try:
            numeric_room_id = int(room_id)
        except (TypeError, ValueError):
            return {"status": "invalid_room", "room_id": room_id}
        if not self.available:
            return {
                "status": "unavailable",
                "room_id": numeric_room_id,
                "corpus": self.metadata(),
            }

        safe_flags = {"ROOM_SANCTUARY", "ROOM_NO_COMBAT"}
        start_id = f"location:{numeric_room_id}"
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id,canonical_name,payload_json,source_ref FROM entities WHERE kind='location'"
            ).fetchall()
            locations: dict[str, dict[str, Any]] = {}
            for row in rows:
                facts = json.loads(row["payload_json"])
                flags = {
                    str(value)
                    for value in facts.get("flags", [])
                    if str(value)
                }
                locations[str(row["id"])] = {
                    "room_id": facts.get("room_id"),
                    "name": row["canonical_name"],
                    "region": facts.get("region"),
                    "flags": sorted(flags),
                    "source_ref": row["source_ref"],
                }
            start = locations.get(start_id)
            if start is None:
                return {
                    "status": "unknown_room",
                    "room_id": numeric_room_id,
                    "corpus": self.metadata(),
                }

            adjacency: dict[str, set[str]] = defaultdict(set)
            for relation in connection.execute(
                "SELECT subject_id,object_id FROM relations WHERE predicate='connects_to'"
            ):
                subject = str(relation["subject_id"])
                target = str(relation["object_id"])
                # The source describes a room connection, while the live
                # travel tool remains authoritative about usable direction.
                adjacency[subject].add(target)
                adjacency[target].add(subject)

            distances = {start_id: 0}
            queue = deque([start_id])
            while queue:
                current = queue.popleft()
                for neighbor in adjacency.get(current, set()):
                    if neighbor in distances:
                        continue
                    distances[neighbor] = distances[current] + 1
                    queue.append(neighbor)

            candidates: list[tuple[tuple[Any, ...], str, str]] = []
            for entity_id, candidate in locations.items():
                flags = set(candidate["flags"])
                if not flags.intersection(safe_flags):
                    continue
                distance = distances.get(entity_id)
                same_region = bool(start.get("region")) and (
                    normalize(candidate.get("region"))
                    == normalize(start.get("region"))
                )
                if distance is None and not same_region:
                    continue
                safety_score = (
                    8 * ("ROOM_SANCTUARY" in flags)
                    + 4 * ("ROOM_SAFELOGOFF" in flags)
                    + 2 * ("ROOM_TRIPLE_HEAL" in flags)
                    + ("ROOM_HOMETOWN" in flags)
                    + ("ROOM_NO_COMBAT" in flags)
                )
                basis = "source_connection_graph" if distance is not None else "source_region"
                rank = (
                    0 if distance is not None else 1,
                    distance if distance is not None else 0,
                    0 if str(candidate.get("room_id")) == str(preferred_room_id) else 1,
                    -safety_score,
                    normalize(candidate.get("name")),
                )
                candidates.append((rank, entity_id, basis))

            if not candidates:
                return {
                    "status": "not_found",
                    "room_id": numeric_room_id,
                    "corpus": self.metadata(),
                }
            _rank, entity_id, basis = min(candidates, key=lambda value: value[0])
            selected = locations[entity_id]
            return {
                "status": "found",
                **selected,
                "distance": distances.get(entity_id),
                "basis": basis,
                "from_room_id": numeric_room_id,
                "evidence": {
                    "source_tier": "source-derived",
                    "source_ref": selected.get("source_ref"),
                    "corpus_version": self.corpus_version,
                },
            }
        finally:
            connection.close()

    def _room_spawn_table(
        self,
        connection: sqlite3.Connection,
        location_row: sqlite3.Row,
        *,
        include_empty: bool = False,
    ) -> dict[str, Any] | None:
        """Return every indexed spawn source that can populate one room.

        The compendium stores this relationship by monster.  Turning it around
        here keeps the indexed provenance while giving the decision layers the
        room-shaped view that is useful during play.
        """
        room_facts = json.loads(location_row["payload_json"])
        rows = connection.execute(
            """SELECT r.payload_json,e.id,e.canonical_name,e.payload_json AS creature_payload_json,
                      e.source_ref,e.source_tier
               FROM relations r JOIN entities e ON e.id=r.subject_id
               WHERE r.object_id=? AND r.predicate='spawns_in'
               ORDER BY e.canonical_name""",
            (location_row["id"],),
        ).fetchall()
        spawns: list[dict[str, Any]] = []
        for row in rows:
            relation = json.loads(row["payload_json"])
            creature = json.loads(row["creature_payload_json"])
            spawn = {
                "creature_id": row["id"],
                "creature": row["canonical_name"],
                "level": creature.get("level"),
                "difficulty": creature.get("difficulty"),
                "karma": creature.get("karma"),
                # Level alone is not an aggression signal. Merchants, teachers,
                # and other people have levels too, so decision layers need the
                # source-derived role before classifying a room as hazardous.
                "role": creature.get("role"),
                "faction": creature.get("faction"),
                "chance": relation.get("chance"),
                "cap": relation.get("cap"),
                "count": relation.get("count"),
                "how": relation.get("how"),
                "citation": relation.get("cite") or row["source_ref"],
            }
            spawns.append(
                {key: value for key, value in spawn.items() if value is not None}
            )
        spawns.sort(
            key=lambda item: (
                -(float(item["chance"]) if isinstance(item.get("chance"), (int, float)) else -1),
                str(item.get("creature", "")),
            )
        )
        if not spawns and not include_empty:
            return None
        generator_chances = [
            float(item["chance"])
            for item in spawns
            if item.get("how") == "generator"
            and isinstance(item.get("chance"), (int, float))
        ]
        caps = [
            int(item["cap"])
            for item in spawns
            if isinstance(item.get("cap"), (int, float))
        ]
        result = {
            "room": {
                "id": location_row["id"],
                "room_id": room_facts.get("room_id"),
                "name": location_row["canonical_name"],
            },
            "generator_chance_total": sum(generator_chances) if generator_chances else None,
            "population_cap": max(caps) if caps else None,
            "spawns": spawns,
            "interpretation": (
                "Static source-derived generators and scripted spawns; live look is authoritative for the current population."
            ),
        }
        safe_spot_evidence = self._safe_spot_summary(room_facts.get("room_id"))
        if safe_spot_evidence is not None:
            result["safe_spot_evidence"] = safe_spot_evidence
        return result

    def _safe_spot_summary(self, room_id: Any) -> dict[str, Any] | None:
        """Summarize ordinary-client wall tests shared by the harness fleet.

        This evidence is intentionally kept outside the immutable fact index: it
        grows as normal clients test squares.  It is decision context, not a
        server truth, and a failed square remains discredited even if it once
        held.  The keeper consumes the detailed book directly; planners only
        need enough evidence to compare candidate rooms.
        """
        if room_id in (None, ""):
            return None
        path = self.config.harness.root / "substrate" / "m59-safespots.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        rooms = raw.get("rooms", {}) if isinstance(raw, dict) else {}
        records = rooms.get(str(room_id), {}) if isinstance(rooms, dict) else {}
        if not isinstance(records, dict) or not records:
            return None
        entries = [value for value in records.values() if isinstance(value, dict)]
        clean = [
            value
            for value in entries
            if int(value.get("held", 0) or 0) > 0
            and int(value.get("failed", 0) or 0) == 0
        ]
        discredited = [
            value for value in entries if int(value.get("failed", 0) or 0) > 0
        ]
        clean.sort(
            key=lambda value: (
                -int(value.get("held", 0) or 0),
                -float(value.get("held_seconds", 0) or 0),
                int(value.get("col", 0) or 0),
                int(value.get("row", 0) or 0),
            )
        )
        best = []
        for value in clean[:3]:
            spot = {
                key: value.get(key)
                for key in (
                    "col",
                    "row",
                    "held",
                    "failed",
                    "held_seconds",
                    "most_attackers",
                )
                if value.get(key) is not None
            }
            best.append(spot)
        return {
            "source": "shared ordinary-client combat tests",
            "tested_squares": len(entries),
            "proven_clean_squares": len(clean),
            "discredited_squares": len(discredited),
            "clean_hold_seconds": round(
                sum(float(value.get("held_seconds", 0) or 0) for value in clean), 1
            ),
            "best_clean_spots": best,
            "interpretation": (
                "A clean hold is stronger than geometry but remains historical; the keeper must verify reachability and current combat. "
                "Any square with a recorded failure is discredited even if it held before."
            ),
        }

    def hunting_room_options(
        self,
        creature_query: str,
        *,
        current_max_health: int | None = None,
        preferred_room_ids: Iterable[Any] | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Return target-bearing rooms with their *complete* spawn tables."""
        resolved = self.resolve(str(creature_query), kinds=["creature"])
        if resolved.get("status") != "found":
            return {
                "status": resolved.get("status", "not_found"),
                "query": creature_query,
                "rooms": [],
                "matches": resolved.get("matches", []),
                "corpus": self.metadata(),
            }
        target = resolved["entity"]
        preferred = {
            str(value)
            for value in (preferred_room_ids or [])
            if value not in (None, "")
        }
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT l.*,r.payload_json AS target_spawn_payload_json
                   FROM relations r JOIN entities l ON l.id=r.object_id
                   WHERE r.subject_id=? AND r.predicate='spawns_in' AND l.kind='location'""",
                (target["id"],),
            ).fetchall()
            options: list[dict[str, Any]] = []
            for location_row in rows:
                table = self._room_spawn_table(
                    connection, location_row, include_empty=True
                )
                if table is None:
                    continue
                location_facts = json.loads(location_row["payload_json"])
                target_spawn = json.loads(location_row["target_spawn_payload_json"])
                target_level = target.get("facts", {}).get("level")
                option = {
                    **table,
                    "target": target["canonical_name"],
                    "target_level": target_level,
                    "target_chance": target_spawn.get("chance"),
                    "target_how": target_spawn.get("how"),
                    "target_eligible_for_hp": (
                        bool(target_level > current_max_health)
                        if isinstance(target_level, (int, float))
                        and isinstance(current_max_health, int)
                        else None
                    ),
                    "preferred": (
                        str(location_facts.get("room_id")) in preferred
                        or str(location_row["id"]) in preferred
                    ),
                }
                options.append(option)
            options.sort(
                key=lambda item: (
                    not bool(item.get("preferred")),
                    -(float(item["target_chance"]) if isinstance(item.get("target_chance"), (int, float)) else -1),
                    int(item.get("population_cap") or 1_000_000),
                    int(item.get("room", {}).get("room_id") or 1_000_000),
                )
            )
            bounded = max(1, min(int(limit), 20))
            return {
                "status": "found",
                "target": self._compact_entity(target),
                "rooms": options[:bounded],
                "selection_note": (
                "Compare the full spawn mix and empirical farm scorecard; target chance alone is not a safety or throughput guarantee."
                " Compare safe_spot_evidence between rooms when using the wall strategy; shared clean holds are historical evidence, not a guarantee."
            ),
                "corpus": self.metadata(),
            }
        finally:
            connection.close()

    def metadata(self) -> dict[str, Any]:
        return {
            key: self._metadata.get(key)
            for key in ("corpus_version", "index_version", "harness_revision", "built_at", "entity_count", "source_files", "fts_enabled", "source_policy")
        }

    @staticmethod
    def _ability_acquisition_intent(goal: dict[str, Any]) -> bool:
        constraints = goal.get("constraints") if isinstance(goal.get("constraints"), dict) else {}
        plan = constraints.get("purchase_plan") if isinstance(constraints, dict) else None
        if isinstance(plan, dict) and plan.get("offering_kind") in {"skill", "spell"}:
            return True
        title = str(goal.get("title") or "")
        objective = str(goal.get("objective") or "")
        acquisition = (
            r"(?:learn|learns|learning|acquire|acquires|acquiring|unlock|unlocks|unlocking)"
        )
        ability = (
            r"(?:skill|skills|spell|spells|ability|abilities|training|weapon|weapons|"
            r"fighting|slash|punch|dodge)"
        )
        declared_in_title = bool(
            re.search(rf"\b{acquisition}\b", title, re.IGNORECASE)
            and re.search(rf"\b{ability}\b", title, re.IGNORECASE)
        )
        declared_at_objective_start = bool(
            re.match(rf"^\s*(?:to\s+)?{acquisition}\b", objective, re.IGNORECASE)
            and re.search(rf"\b{ability}\b", objective, re.IGNORECASE)
        )
        return declared_in_title or declared_at_objective_start

    @staticmethod
    def _explicit_purchase_intent(goal: dict[str, Any]) -> bool:
        constraints = goal.get("constraints") if isinstance(goal.get("constraints"), dict) else {}
        if isinstance(constraints, dict) and constraints.get("purchase_plan") is not None:
            return True
        title = str(goal.get("title") or "")
        objective = str(goal.get("objective") or "")
        purchase = r"(?:buy|buys|buying|purchase|purchases|purchasing|shop|shopping)"
        purchased_result = any(
            isinstance(criterion, dict) and criterion.get("kind") == "inventory_contains"
            for criterion in goal.get("success_criteria", [])
        )
        return bool(
            re.search(rf"\b{purchase}\b", title, re.IGNORECASE)
            or re.match(rf"^\s*(?:to\s+)?{purchase}\b", objective, re.IGNORECASE)
            or (
                purchased_result
                and re.search(rf"\b{purchase}\b", objective, re.IGNORECASE)
            )
            or KnowledgeBase._ability_acquisition_intent(goal)
        )

    def _validate_purchase_goal(
        self,
        canonical: dict[str, Any],
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        resolved: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not self._explicit_purchase_intent(canonical):
            return None
        acquisition_intent = self._ability_acquisition_intent(canonical)
        constraints = canonical.get("constraints")
        plan = constraints.get("purchase_plan") if isinstance(constraints, dict) else None
        if not isinstance(plan, dict):
            errors.append(
                {
                    "code": "PURCHASE_PLAN_REQUIRED",
                    "message": (
                        "A goal whose declared outcome is buying an item or learning a paid skill/spell requires "
                        "constraints.purchase_plan with exact offering, offering_kind, merchant_class, "
                        "room_id, and a training budget when applicable."
                    ),
                }
            )
            if acquisition_intent and not any(
                isinstance(criterion, dict)
                and criterion.get("kind") == "numeric_threshold"
                and parse_ability_metric(criterion.get("metric")) is not None
                for criterion in canonical.get("success_criteria", [])
            ):
                errors.append(
                    {
                        "code": "ABILITY_RESULT_CRITERION_REQUIRED",
                        "message": (
                            "A learning goal must verify acquisition with numeric_threshold on the exact "
                            "ability.skill.<name> or ability.spell.<name> metric at >= 1. Conversation or "
                            "teacher-room criteria do not prove that training succeeded."
                        ),
                    }
                )
            return {"status": "missing", "static_verified": False, "live_quote_required": True}
        offering_kind = str(plan.get("offering_kind", "item")).casefold()
        verification: dict[str, Any] = {
            "status": "invalid",
            "static_verified": False,
            "live_quote_required": True,
            "offering_kind": offering_kind,
            "item": plan.get("item"),
            "merchant_class": plan.get("merchant_class"),
            "room_id": plan.get("room_id"),
            "maximum_price": plan.get("maximum_price"),
        }
        if not self.available:
            warnings.append(
                {
                    "code": "PURCHASE_FEASIBILITY_SKIPPED",
                    "message": "Merchant placement and stock could not be verified because the knowledge corpus is unavailable.",
                }
            )
            return verification

        entity_kinds = {
            "item": ["item", "weapon", "armor", "reagent"],
            "skill": ["skill"],
            "spell": ["spell"],
        }[offering_kind]
        item_result = self.resolve(
            str(plan.get("item") or ""),
            kinds=entity_kinds,
        )
        item_entity = item_result.get("entity") if item_result.get("status") == "found" else None
        if offering_kind == "item" and item_result.get("status") == "ambiguous":
            matches = [
                value
                for value in item_result.get("matches", [])
                if isinstance(value, dict)
            ]
            if matches and len(
                {normalize(value.get("canonical_name")) for value in matches}
            ) == 1:
                priority = {"armor": 0, "weapon": 1, "reagent": 2, "item": 3}
                item_entity = min(
                    matches,
                    key=lambda value: (
                        priority.get(str(value.get("kind")), 9),
                        str(value.get("id")),
                    ),
                )
                warnings.append(
                    {
                        "code": "PURCHASE_ITEM_CANONICALIZED",
                        "message": (
                            "Collapsed duplicate generic-item and typed-equipment pages for "
                            f"{item_entity.get('canonical_name')}."
                        ),
                    }
                )
        if not isinstance(item_entity, dict):
            errors.append(
                {
                    "code": "UNKNOWN_PURCHASE_ITEM"
                    if item_result.get("status") == "not_found"
                    else "AMBIGUOUS_PURCHASE_ITEM",
                    "value": plan.get("item"),
                    "message": (
                        f"Purchase offering must resolve to one exact canonical {offering_kind} entity."
                    ),
                    "suggestions": [
                        value.get("canonical_name") for value in item_result.get("matches", [])[:5]
                    ],
                }
            )
        merchant_result = self.resolve(
            str(plan.get("merchant_class") or ""), kinds=["merchant"]
        )
        merchant_entity = (
            merchant_result.get("entity")
            if merchant_result.get("status") == "found"
            else None
        )
        if not isinstance(merchant_entity, dict):
            errors.append(
                {
                    "code": "UNKNOWN_MERCHANT_CLASS"
                    if merchant_result.get("status") == "not_found"
                    else "AMBIGUOUS_MERCHANT_CLASS",
                    "value": plan.get("merchant_class"),
                    "message": "merchant_class must match one exact class in the generated merchant catalogue.",
                    "suggestions": [
                        value.get("facts", {}).get("merchant_class")
                        or value.get("canonical_name")
                        for value in merchant_result.get("matches", [])[:5]
                    ],
                }
            )
        if not isinstance(item_entity, dict) or not isinstance(merchant_entity, dict):
            return verification

        item_name = str(item_entity.get("canonical_name") or "")
        merchant_facts = merchant_entity.get("facts", {})
        merchant_class = str(merchant_facts.get("merchant_class") or plan.get("merchant_class") or "")
        if offering_kind == "item":
            stock = merchant_facts.get("sells", [])
            stock = stock if isinstance(stock, list) else []
            stock_match = next(
                (
                    value
                    for value in stock
                    if isinstance(value, dict)
                    and normalize(plan.get("item"))
                    in {normalize(value.get("class")), normalize(value.get("name"))}
                ),
                None,
            )
        else:
            stock = merchant_facts.get("teaches", [])
            stock = stock if isinstance(stock, list) else []
            taught_name = next(
                (
                    str(value)
                    for value in stock
                    if normalize(value) in {normalize(plan.get("item")), normalize(item_name)}
                ),
                None,
            )
            stock_match = {"name": taught_name, "kind": offering_kind} if taught_name else None
        room_ids = {
            int(value)
            for value in merchant_facts.get("room_ids", [])
            if isinstance(value, int) and not isinstance(value, bool)
        }
        if not merchant_facts.get("available") or not room_ids:
            errors.append(
                {
                    "code": "MERCHANT_UNAVAILABLE",
                    "value": merchant_class,
                    "message": (
                        f"Merchant class {merchant_class} is source-only/unplaced and cannot be used as a destination."
                    ),
                    "unavailable_reason": merchant_facts.get("unavailable_reason"),
                }
            )
        if offering_kind == "item" and not isinstance(stock_match, dict):
            errors.append(
                {
                    "code": "MERCHANT_ITEM_UNAVAILABLE",
                    "item": item_name,
                    "merchant_class": merchant_class,
                    "message": f"{merchant_class} does not stock the exact item {item_name}.",
                    "stock": [value.get("name") for value in stock if isinstance(value, dict)],
                }
            )
        elif offering_kind in {"skill", "spell"} and stock and not isinstance(stock_match, dict):
            errors.append(
                {
                    "code": "MERCHANT_ABILITY_UNAVAILABLE",
                    "item": item_name,
                    "offering_kind": offering_kind,
                    "merchant_class": merchant_class,
                    "message": f"{merchant_class} does not teach the exact {offering_kind} {item_name}.",
                    "teaches": [str(value) for value in stock],
                }
            )
        elif offering_kind in {"skill", "spell"} and not stock:
            # Older harness catalogues omit teacher offerings even though the
            # ordinary client's shop quote exposes them. Preserve the grounded
            # ability plus instantiated teacher/room, but make the fresh quote
            # the authoritative offering check before any money can move.
            warnings.append(
                {
                    "code": "TEACHER_STOCK_LIVE_VERIFICATION_REQUIRED",
                    "message": (
                        f"The generated catalogue has no teacher-offering rows for {merchant_class}; "
                        f"the controller must see {item_name} in a fresh in-room shop quote before purchase."
                    ),
                }
            )
            stock_match = {
                "name": item_name,
                "kind": offering_kind,
                "verification": "fresh live quote required",
            }
        room_id = plan.get("room_id")
        if isinstance(room_id, int) and room_ids and room_id not in room_ids:
            errors.append(
                {
                    "code": "MERCHANT_ROOM_MISMATCH",
                    "merchant_class": merchant_class,
                    "room_id": room_id,
                    "message": f"{merchant_class} is not instantiated in room {room_id}.",
                    "valid_room_ids": sorted(room_ids),
                }
            )
        if offering_kind == "item":
            exact_inventory_criteria = [
                criterion
                for criterion in canonical.get("success_criteria", [])
                if isinstance(criterion, dict)
                and criterion.get("kind") == "inventory_contains"
                and normalize(criterion.get("item")) == normalize(item_name)
            ]
            if not exact_inventory_criteria:
                errors.append(
                    {
                        "code": "PURCHASE_RESULT_CRITERION_REQUIRED",
                        "item": item_name,
                        "message": (
                            f"The purchase goal must include inventory_contains for the exact item {item_name}; "
                            "a broad category such as armor cannot prove the planned transaction succeeded."
                        ),
                    }
                )
        else:
            exact_metric = f"ability.{offering_kind}.{item_name}"
            exact_ability_criteria = []
            for criterion in canonical.get("success_criteria", []):
                if not isinstance(criterion, dict) or criterion.get("kind") != "numeric_threshold":
                    continue
                parsed = parse_ability_metric(criterion.get("metric"))
                if parsed is None:
                    continue
                metric_kind, metric_name = parsed
                if (
                    metric_kind == offering_kind
                    and normalize(metric_name) == normalize(item_name)
                    and criterion.get("operator", ">=") == ">="
                    and isinstance(criterion.get("value"), (int, float))
                    and not isinstance(criterion.get("value"), bool)
                    and criterion["value"] >= 1
                ):
                    exact_ability_criteria.append(criterion)
            if not exact_ability_criteria:
                errors.append(
                    {
                        "code": "ABILITY_RESULT_CRITERION_REQUIRED",
                        "item": item_name,
                        "offering_kind": offering_kind,
                        "message": (
                            f"Training must include numeric_threshold metric {exact_metric}, "
                            "operator >=, value at least 1. Conversation or location criteria do not prove acquisition."
                        ),
                    }
                )
            maximum_price = plan.get("maximum_price")
            if (
                not isinstance(maximum_price, int)
                or isinstance(maximum_price, bool)
                or maximum_price <= 0
            ):
                errors.append(
                    {
                        "code": "ABILITY_PURCHASE_BUDGET_REQUIRED",
                        "item": item_name,
                        "message": (
                            "Paid training requires a positive purchase_plan.maximum_price so the controller "
                            "can verify funds and withdraw the bounded amount before traveling to the teacher."
                        ),
                    }
                )
            for criterion in exact_ability_criteria:
                criterion["metric"] = exact_metric
        plan["offering_kind"] = offering_kind
        plan["item"] = item_name
        plan["merchant_class"] = merchant_class
        if isinstance(room_id, int):
            plan["room_id"] = room_id
        verification.update(
            {
                "item": item_name,
                "offering_kind": offering_kind,
                "item_entity_id": item_entity.get("id"),
                "merchant_class": merchant_class,
                "merchant_entity_id": merchant_entity.get("id"),
                "merchant_available": bool(merchant_facts.get("available")),
                "valid_room_ids": sorted(room_ids),
                "stock_match": stock_match,
            }
        )
        resolved.extend(
            [
                {"source": f"purchase_plan.{offering_kind}", "entity": item_entity},
                {"source": "purchase_plan.merchant", "entity": merchant_entity},
            ]
        )
        if not any(
            error.get("code", "").startswith(("PURCHASE_", "MERCHANT_", "ABILITY_"))
            or error.get("code") in {"UNKNOWN_PURCHASE_ITEM", "AMBIGUOUS_PURCHASE_ITEM", "UNKNOWN_MERCHANT_CLASS", "AMBIGUOUS_MERCHANT_CLASS"}
            for error in errors
            if isinstance(error, dict)
        ):
            verification["status"] = "static_verified"
            verification["static_verified"] = True
            warnings.append(
                {
                    "code": "LIVE_SHOP_QUOTE_REQUIRED",
                    "message": (
                        "Static offering identity and merchant placement are verified. The controller will require "
                        "a fresh in-room merchant observation and quote-only shop response before authorizing buy_ids."
                    ),
                }
            )
        return verification

    def validate_goal(self, goal: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(goal, dict):
            raise ValueError("goal must be an object")
        allowed = {"request_id", "title", "objective", "success_criteria", "constraints", "priority", "activation"}
        internal = {
            "id",
            "version",
            "status",
            "source",
            "created_at",
            "updated_at",
            "activated_at",
            "terminal_at",
            "blocked_reason",
            "completion",
            "retry_of_goal_id",
        }
        canonical = copy.deepcopy({key: value for key, value in goal.items() if key in allowed})
        criteria = canonical.get("success_criteria", [])
        unknown = sorted(set(goal) - allowed - internal)
        errors: list[dict[str, Any]] = [
            {
                "code": "UNKNOWN_GOAL_FIELDS",
                "fields": unknown,
                "message": f"Unknown goal field(s): {', '.join(unknown)}.",
            }
        ] if unknown else []
        warnings: list[dict[str, Any]] = []
        resolved: list[dict[str, Any]] = []
        purchase_verification: dict[str, Any] | None = None
        if not self.available:
            warnings.append({"code": "KNOWLEDGE_UNAVAILABLE", "message": "Meridian knowledge corpus is unavailable; static entity validation was skipped."})
        if not isinstance(criteria, list):
            return {
                "valid": False,
                "canonical_goal": canonical,
                "errors": [{"code": "INVALID_CRITERIA", "message": "success_criteria must be an array"}],
                "warnings": warnings,
                "resolved_entities": resolved,
                "corpus": self.metadata(),
            }
        for index, criterion in enumerate(criteria):
            if not isinstance(criterion, dict):
                continue
            kind = criterion.get("kind")
            if kind in {"numeric_threshold", "numeric_delta"}:
                metric = str(criterion.get("metric", ""))
                if metric in self.METRIC_ALIASES:
                    criterion["metric"] = self.METRIC_ALIASES[metric]
                    warnings.append(
                        {
                            "code": "METRIC_CANONICALIZED",
                            "criterion": index,
                            "message": f"Canonicalized metric {metric} to {criterion['metric']}.",
                        }
                    )
                metric = str(criterion.get("metric", ""))
                ability_metric = parse_ability_metric(metric)
                if metric.casefold().startswith("ability.") and ability_metric is None:
                    errors.append(
                        {
                            "code": "INVALID_ABILITY_METRIC",
                            "criterion": index,
                            "value": metric,
                            "message": (
                                "Named ability metrics must use ability.skill.<canonical name> "
                                "or ability.spell.<canonical name>."
                            ),
                        }
                    )
                elif ability_metric is not None and self.available:
                    ability_kind, ability_name = ability_metric
                    ability_result = self.resolve(ability_name, kinds=[ability_kind])
                    ability_entity = (
                        ability_result.get("entity")
                        if ability_result.get("status") == "found"
                        else None
                    )
                    if isinstance(ability_entity, dict):
                        canonical_metric = (
                            f"ability.{ability_kind}.{ability_entity['canonical_name']}"
                        )
                        criterion["metric"] = canonical_metric
                        resolved.append({"criterion": index, "entity": ability_entity})
                        if canonical_metric != metric:
                            warnings.append(
                                {
                                    "code": "ABILITY_METRIC_CANONICALIZED",
                                    "criterion": index,
                                    "message": (
                                        f"Canonicalized named ability metric {metric} "
                                        f"to {canonical_metric}."
                                    ),
                                }
                            )
                    else:
                        errors.append(
                            {
                                "code": (
                                    "UNKNOWN_ABILITY"
                                    if ability_result.get("status") == "not_found"
                                    else "AMBIGUOUS_ABILITY"
                                ),
                                "criterion": index,
                                "value": ability_name,
                                "message": (
                                    f"{ability_name!r} is not one exact canonical "
                                    f"Meridian {ability_kind}."
                                ),
                                "suggestions": [
                                    item.get("canonical_name")
                                    for item in ability_result.get("matches", [])[:5]
                                ],
                            }
                        )
            if kind != "location_reached":
                continue
            if not self.available:
                continue
            raw_name = criterion.get("location", criterion.get("room"))
            raw_id = criterion.get("room_id")
            by_id = self.resolve(str(raw_id), kinds=["location"]) if raw_id is not None else None
            by_name = self.resolve(str(raw_name), kinds=["location"]) if isinstance(raw_name, str) and raw_name.strip() else None
            chosen = None
            if by_id and by_id["status"] == "found":
                chosen = by_id["entity"]
            elif by_id:
                errors.append(
                    {
                        "code": "UNKNOWN_ROOM_ID" if by_id["status"] == "not_found" else "AMBIGUOUS_ROOM_ID",
                        "criterion": index,
                        "value": raw_id,
                        "message": f"Room id {raw_id!r} is not a unique room in the pinned Meridian corpus.",
                        "suggestions": [item["canonical_name"] for item in by_id.get("matches", [])[:5]],
                    }
                )
            if by_name and by_name["status"] == "found":
                if chosen and chosen["id"] != by_name["entity"]["id"]:
                    errors.append(
                        {
                            "code": "LOCATION_CONFLICT",
                            "criterion": index,
                            "value": raw_name,
                            "message": f"Location name {raw_name!r} conflicts with room id {raw_id!r}.",
                        }
                    )
                else:
                    chosen = by_name["entity"]
            elif by_name:
                code = "AMBIGUOUS_LOCATION" if by_name["status"] == "ambiguous" else "UNKNOWN_LOCATION"
                errors.append(
                    {
                        "code": code,
                        "criterion": index,
                        "value": raw_name,
                        "message": (
                            f"Location {raw_name!r} is ambiguous; use an exact numeric room id."
                            if code == "AMBIGUOUS_LOCATION"
                            else f"Location {raw_name!r} does not exist in the pinned Meridian corpus."
                        ),
                        "suggestions": [
                            {"name": item["canonical_name"], "room_id": item.get("facts", {}).get("room_id")}
                            for item in by_name.get("matches", [])[:5]
                        ],
                    }
                )
            if chosen:
                facts = chosen.get("facts", {})
                criterion["location"] = chosen["canonical_name"]
                criterion.pop("room", None)
                if facts.get("room_id") is not None:
                    criterion["room_id"] = facts["room_id"]
                resolved.append({"criterion": index, "entity": chosen})
        try:
            # Reuse the controller's durable goal contract so the read-only
            # validator cannot say valid and then have submit_goal reject the
            # same canonical draft for a missing or mistyped criterion field.
            from .storage import Storage

            normalized = Storage._validate_goal(canonical)
            activation = normalized["activation"]
            if activation not in {"queue", "replace_active_pause", "replace_active_cancel"}:
                raise ValueError("invalid activation")
            canonical = {
                **({"request_id": canonical["request_id"]} if "request_id" in canonical else {}),
                "title": normalized["title"],
                "objective": normalized["objective"],
                "success_criteria": normalized["success_criteria"],
                "constraints": normalized["constraints"],
                "priority": normalized["priority"],
                "activation": activation,
            }
            constraints = canonical.get("constraints", {})
            operator_notes = (
                str(constraints.get("operator_notes") or "")
                if isinstance(constraints, dict)
                else ""
            )
            malformed_farm_fields = []
            farm_note_patterns = {
                "assigned_room": r"\bassigned_room\s*=\s*\d+\b",
                "hunt": r"\bhunt\s*=\s*[a-z][a-z ]*?(?=[,;]|\s+(?:assigned_room|max_carry|use_safe_spots|flee_below|hold_resume_above|rest_below|fight_above_vigor|bank_above|pull_within|break_out_via_logoff)\s*=|$)",
                "use_safe_spots": r"\buse_safe_spots\s*=\s*(?:true|false)\b",
            }
            for field, pattern in farm_note_patterns.items():
                if re.search(rf"\b{field}\b", operator_notes, re.IGNORECASE) and not re.search(
                    pattern, operator_notes, re.IGNORECASE
                ):
                    malformed_farm_fields.append(field)
            if malformed_farm_fields:
                errors.append(
                    {
                        "code": "INVALID_FARM_OPERATOR_NOTES",
                        "fields": malformed_farm_fields,
                        "message": (
                            "Farm execution fields in operator_notes must use exact key=value syntax. "
                            "Example: hunt=groundworm larva; assigned_room=567; "
                            "use_safe_spots=true; flee_below=0.60; hold_resume_above=0.90; "
                            "fight_above_vigor=100; bank_above=0; "
                            "break_out_via_logoff=false."
                        ),
                    }
                )
            purchase_verification = self._validate_purchase_goal(
                canonical, errors, warnings, resolved
            )
        except ValueError as exc:
            errors.append({"code": "INVALID_GOAL_SCHEMA", "message": str(exc)})
        return {
            "valid": not errors,
            "canonical_goal": canonical,
            "errors": errors,
            "warnings": warnings,
            "resolved_entities": resolved,
            "purchase_verification": purchase_verification,
            "corpus": self.metadata(),
        }

    def require_valid_goal(self, goal: dict[str, Any]) -> dict[str, Any]:
        result = self.validate_goal(goal)
        if not result["valid"]:
            raise KnowledgeValidationError(result)
        return result

    def context_for(self, goal: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        validation = self.validate_goal(goal)
        entities: list[dict[str, Any]] = [item["entity"] for item in validation.get("resolved_entities", [])]
        room_id = deep_get(observation, "look.room.num", deep_get(observation, "look.room_id"))
        room_name = deep_get(observation, "look.room.name", deep_get(observation, "look.room"))
        for query in (room_id, room_name):
            if query is None:
                continue
            result = self.resolve(str(query), kinds=["location"])
            if result.get("status") == "found" and result["entity"]["id"] not in {entity["id"] for entity in entities}:
                entities.append(result["entity"])
        objective = str(goal.get("objective", ""))
        if objective and self.available:
            for entity in self.search(objective, limit=5).get("matches", []):
                if entity["id"] not in {value["id"] for value in entities}:
                    entities.append(entity)
        constraints = goal.get("constraints", {})
        operator_notes = (
            str(constraints.get("operator_notes") or "")
            if isinstance(constraints, dict)
            else ""
        )
        assigned_match = re.search(
            r"\bassigned_room\s*=\s*(\d+)\b", operator_notes, re.IGNORECASE
        )
        if assigned_match:
            assigned = self.resolve(assigned_match.group(1), kinds=["location"])
            if assigned.get("status") == "found" and assigned["entity"]["id"] not in {
                value["id"] for value in entities
            }:
                entities.append(assigned["entity"])
        hunt_match = re.search(
            r"\bhunt\s*=\s*[\"']?([a-z][a-z ]*?)(?=[\"',;]|\s+(?:assigned_room|max_carry|use_safe_spots|flee_below|hold_resume_above|rest_below|fight_above_vigor|bank_above|pull_within|break_out_via_logoff)\s*=|$)",
            operator_notes,
            re.IGNORECASE,
        )
        hunt = " ".join(hunt_match.group(1).casefold().split()) if hunt_match else None

        room_spawn_tables: list[dict[str, Any]] = []
        if self.available:
            connection = self._connect()
            try:
                for entity in entities[:8]:
                    if entity.get("kind") != "location":
                        continue
                    location_row = connection.execute(
                        "SELECT * FROM entities WHERE id=?", (entity["id"],)
                    ).fetchone()
                    if location_row is None:
                        continue
                    table = self._room_spawn_table(connection, location_row)
                    if table is not None:
                        room_spawn_tables.append(table)
            finally:
                connection.close()

        max_health = deep_get(
            observation,
            "status.vitals.health.max",
            deep_get(observation, "look.vitals.health.max"),
        )
        try:
            max_health = int(max_health) if max_health is not None else None
        except (TypeError, ValueError):
            max_health = None
        hunt_room_options = (
            self.hunting_room_options(
                hunt,
                current_max_health=max_health,
                preferred_room_ids=[
                    room_id,
                    int(assigned_match.group(1)) if assigned_match else None,
                ],
                limit=8,
            )
            if hunt and self.available
            else None
        )
        context = {
            "corpus": self.metadata(),
            "goal_validation": {
                "valid": validation["valid"],
                "errors": validation["errors"],
                "warnings": validation["warnings"],
            },
            "relevant_entities": [self._compact_entity(entity) for entity in entities[:8]],
            "room_spawn_tables": room_spawn_tables,
            "new_player_doctrine": copy.deepcopy(NEW_PLAYER_DOCTRINE),
            "rules": [
                "Use canonical names and numeric room ids from this context or broker results.",
                "Do not invent a game entity. Query knowledge when a necessary named fact is absent.",
                "A zero-match lookup is negative evidence; choose a different target instead of repeating it.",
                "Live ordinary-client observation overrides static reference data for current state.",
                "For farming, use complete room_spawn_tables and hunt_room_options: a room cap occupied by nuisance creatures can prevent the desired prey from spawning.",
            ],
        }
        if hunt_room_options is not None:
            context["hunt_room_options"] = hunt_room_options
        return context

    @staticmethod
    def _compact_entity(entity: dict[str, Any]) -> dict[str, Any]:
        facts = entity.get("facts", {}) if isinstance(entity.get("facts"), dict) else {}
        keep_by_kind = {
            "location": ("room_id", "region", "terrain", "flags", "teleport", "exits", "monsters"),
            "creature": ("level", "difficulty", "karma", "speed", "role", "where", "vulnerabilities", "resistances", "attack_type"),
            "npc": ("level", "karma", "role", "where"),
            "merchant": (
                "merchant_class",
                "available",
                "placed",
                "placement_status",
                "unavailable_reason",
                "instances",
                "room_ids",
                "sells",
                "teaches",
                "catalog_markup",
                "price_verification",
            ),
            "spell": ("slug", "school", "level", "mana", "reagents", "required_karma", "page"),
            "skill": ("slug", "level", "discipline", "teacher", "page"),
            "item": ("slug", "value", "page"),
            "weapon": ("slug", "value", "type", "quality", "ranged", "prof"),
            "armor": ("slug", "value", "slot", "defenseBonus", "damageReduce", "resist"),
        }
        keys = keep_by_kind.get(str(entity.get("kind")), ("slug", "page"))
        compact_facts: dict[str, Any] = {}
        for key in keys:
            value = facts.get(key)
            if key == "exits" and isinstance(value, list):
                value = [
                    {name: item.get(name) for name in ("to", "toRid", "kind") if item.get(name) is not None}
                    for item in value[:25]
                    if isinstance(item, dict)
                ]
            if value not in (None, "", [], {}):
                compact_facts[key] = value
        evidence = entity.get("evidence", {}) if isinstance(entity.get("evidence"), dict) else {}
        return {
            "id": entity.get("id"),
            "kind": entity.get("kind"),
            "canonical_name": entity.get("canonical_name"),
            "summary": entity.get("summary"),
            "facts": compact_facts,
            "evidence": {
                "source_tier": evidence.get("source_tier"),
                "source_ref": str(evidence.get("source_ref", ""))[:600],
                "corpus_version": evidence.get("corpus_version"),
            },
        }

    def progression_context(self, state: dict[str, Any] | None = None, *, limit: int = 8) -> dict[str, Any]:
        state = state if isinstance(state, dict) else {}
        hp = deep_get(state, "status.vitals.health.max", deep_get(state, "vitals.health.max", state.get("max_health")))
        try:
            level = int(hp) if hp is not None else None
        except (TypeError, ValueError):
            level = None
        candidates: list[dict[str, Any]] = []
        seen_candidates: set[tuple[str, int, tuple[str, ...]]] = set()
        if self.available and level is not None:
            connection = self._connect()
            try:
                rows = connection.execute("SELECT * FROM entities WHERE kind='creature' ORDER BY canonical_name").fetchall()
                for row in rows:
                    entity = self._entity(row)
                    facts = entity.get("facts", {})
                    if normalize(facts.get("role")) != "monster":
                        continue
                    monster_level = facts.get("level")
                    if not isinstance(monster_level, (int, float)) or monster_level <= level:
                        continue
                    locations = facts.get("where", [])[:8] if isinstance(facts.get("where"), list) else []
                    signature = (
                        normalize(entity["canonical_name"]),
                        int(monster_level),
                        tuple(normalize(location) for location in locations),
                    )
                    if signature in seen_candidates:
                        continue
                    seen_candidates.add(signature)
                    candidates.append(
                        {
                            "id": entity["id"],
                            "name": entity["canonical_name"],
                            "slug": facts.get("slug"),
                            "level": monster_level,
                            "level_above": monster_level - level,
                            "karma": facts.get("karma"),
                            "difficulty": facts.get("difficulty"),
                            "vulnerabilities": facts.get("vulnerabilities", facts.get("weaknesses", [])),
                            "resistances": facts.get("resistances", []),
                            "attack_type": facts.get("attack_type"),
                            "locations": locations,
                            "evidence": entity["evidence"],
                        }
                    )
            finally:
                connection.close()
            candidates.sort(key=lambda item: (item["level_above"] > 10, item["level_above"], item["difficulty"] or 0, item["name"]))
        selected = candidates[: max(1, min(int(limit), 20))]
        room_options = [
            self.hunting_room_options(
                candidate["name"],
                current_max_health=level,
                limit=6,
            )
            for candidate in selected[:3]
        ]
        return {
            "character": {"max_health": level},
            "new_player_doctrine": copy.deepcopy(NEW_PLAYER_DOCTRINE),
            "verified_rule": (
                "Max health is character level. A kill only rolls for a max-health gain when victim level is above current max health."
            ),
            "candidates": selected,
            "room_options_by_candidate": [
                value for value in room_options if value.get("rooms")
            ],
            "guidance": [
                "Compare each room's complete static spawn table with live hunting_grounds and current observations before travel.",
                "A nuisance population can fill the room cap and suppress the desired prey; safely clearing it may improve target throughput.",
                "Treat every candidate as progression-eligible, not proven safe; combine matchup, equipment, and empirical outcomes.",
                "When selecting a wall strategy, compare safe_spot_evidence across rooms and require the keeper to re-verify the chosen square live.",
                "Use live advancement and character observations to override static candidates.",
                "Choose small milestones and satisfy only finish criteria explicitly requested by the human.",
            ],
            "corpus": self.metadata(),
        }
