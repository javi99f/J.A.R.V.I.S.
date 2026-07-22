from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from omar_ai_core.settings import BASE_DIR as SETTINGS_BASE_DIR


BASE_DIR = SETTINGS_BASE_DIR
MEMORY_PATH = BASE_DIR / "memory" / "long_term.json"  # Legacy v1 storage.
MEMORY_DB_PATH = BASE_DIR / "memory" / "jarvis-memory.db"
VALID_CATEGORIES = {
    "identity",
    "preferences",
    "projects",
    "relationships",
    "wishes",
    "notes",
}
MAX_VALUE_LENGTH = 2000
MAX_PROMPT_CHARS = 6500
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_memory() -> dict:
    return {category: {} for category in VALID_CATEGORIES}


def _normalize_category(category: object) -> str:
    value = str(category or "notes").strip().casefold()
    return value if value in VALID_CATEGORIES else "notes"


def _normalize_key(key: object) -> str:
    value = unicodedata.normalize("NFKD", str(key or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value.casefold()).strip("_")
    return value[:100]


def _clean_value(value: object) -> str:
    return " ".join(str(value or "").strip().split())[:MAX_VALUE_LENGTH]


def _is_sensitive(category: str, key: str, value: str) -> bool:
    combined_key = f"{category} {key}".casefold()
    sensitive_keys = {
        "password",
        "passwd",
        "passphrase",
        "contrasena",
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "private_key",
        "secret",
        "cvv",
        "pin_bancario",
    }
    if any(marker in combined_key for marker in sensitive_keys):
        return True
    normalized_value = value.casefold()
    if re.search(r"\b(?:mi|my)\s+(?:contraseña|contrasena|password|pin|token)\s+(?:es|is)\b", normalized_value):
        return True
    if value.startswith("AIza") and len(value) >= 30:
        return True
    return False


def _connect() -> sqlite3.Connection:
    MEMORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(MEMORY_DB_PATH, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def _database():
    connection = _connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _ensure_database() -> None:
    with _lock, _database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                importance REAL NOT NULL DEFAULT 0.5,
                source TEXT NOT NULL DEFAULT 'conversation',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_accessed_at TEXT,
                access_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(category, key)
            );
            CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
            CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC);
            CREATE TABLE IF NOT EXISTS memory_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        migrated = connection.execute(
            "SELECT value FROM memory_metadata WHERE key='legacy_json_migrated'"
        ).fetchone()
        if migrated is None:
            _migrate_legacy_json(connection)
            connection.execute(
                "INSERT OR REPLACE INTO memory_metadata(key, value) VALUES('legacy_json_migrated', ?)",
                (_now(),),
            )


def _migrate_legacy_json(connection: sqlite3.Connection) -> None:
    try:
        data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(data, dict):
        return
    for category, items in data.items():
        if not isinstance(items, dict):
            continue
        for key, raw_entry in items.items():
            value = raw_entry.get("value", "") if isinstance(raw_entry, dict) else raw_entry
            updated = raw_entry.get("updated", _now()) if isinstance(raw_entry, dict) else _now()
            _upsert(
                connection,
                category,
                key,
                value,
                importance=0.55,
                source="legacy_import",
                updated_at=str(updated),
            )


def _upsert(
    connection: sqlite3.Connection,
    category: object,
    key: object,
    value: object,
    importance: float = 0.5,
    source: str = "conversation",
    updated_at: str | None = None,
) -> bool:
    category_text = _normalize_category(category)
    key_text = _normalize_key(key)
    value_text = _clean_value(value)
    if not key_text or not value_text or _is_sensitive(category_text, key_text, value_text):
        return False
    try:
        importance_value = min(1.0, max(0.0, float(importance)))
    except (TypeError, ValueError):
        importance_value = 0.5
    timestamp = updated_at or _now()
    connection.execute(
        """
        INSERT INTO memories(
            category, key, value, importance, source, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(category, key) DO UPDATE SET
            value=excluded.value,
            importance=excluded.importance,
            source=excluded.source,
            updated_at=excluded.updated_at
        """,
        (
            category_text,
            key_text,
            value_text,
            importance_value,
            _clean_value(source)[:80] or "conversation",
            timestamp,
            timestamp,
        ),
    )
    return True


def update_memory(memory_update: dict) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    _ensure_database()
    changed = []
    with _lock, _database() as connection:
        for category, items in memory_update.items():
            if not isinstance(items, dict):
                continue
            for key, raw_entry in items.items():
                if isinstance(raw_entry, dict):
                    value = raw_entry.get("value", "")
                    importance = raw_entry.get("importance", 0.5)
                    source = raw_entry.get("source", "conversation")
                else:
                    value = raw_entry
                    importance = 0.5
                    source = "conversation"
                if _upsert(connection, category, key, value, importance, source):
                    changed.append(f"{_normalize_category(category)}/{_normalize_key(key)}")
    if changed:
        print(f"[Memory] Saved: {changed}")
    return load_memory()


def load_memory() -> dict:
    _ensure_database()
    memory = _empty_memory()
    with _lock, _database() as connection:
        rows = connection.execute(
            "SELECT * FROM memories ORDER BY importance DESC, updated_at DESC"
        ).fetchall()
    for row in rows:
        memory[row["category"]][row["key"]] = {
            "id": row["id"],
            "value": row["value"],
            "importance": row["importance"],
            "source": row["source"],
            "updated": row["updated_at"],
        }
    return memory


def save_memory(memory: dict) -> None:
    """Replace stored memories while preserving the public v1 API."""
    if not isinstance(memory, dict):
        return
    _ensure_database()
    with _lock, _database() as connection:
        connection.execute("DELETE FROM memories")
        for category, items in memory.items():
            if not isinstance(items, dict):
                continue
            for key, raw_entry in items.items():
                entry = raw_entry if isinstance(raw_entry, dict) else {"value": raw_entry}
                _upsert(
                    connection,
                    category,
                    key,
                    entry.get("value", ""),
                    entry.get("importance", 0.5),
                    entry.get("source", "manual"),
                    entry.get("updated"),
                )


def list_memories(search: str = "", limit: int = 500) -> list[dict]:
    _ensure_database()
    limit = min(1000, max(1, int(limit)))
    query = _clean_value(search)
    sql = "SELECT * FROM memories"
    parameters: list[object] = []
    if query:
        sql += " WHERE category LIKE ? OR key LIKE ? OR value LIKE ?"
        like = f"%{query}%"
        parameters.extend((like, like, like))
    sql += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
    parameters.append(limit)
    with _lock, _database() as connection:
        rows = connection.execute(sql, parameters).fetchall()
    return [dict(row) for row in rows]


def _tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", str(text or "").casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return {token for token in re.findall(r"[a-z0-9]{2,}", normalized) if token not in {
        "que", "the", "and", "para", "con", "una", "uno", "del", "las", "los", "por",
    }}


def search_memories(query: str, limit: int = 8) -> list[dict]:
    query_text = _clean_value(query)
    query_tokens = _tokens(query_text)
    candidates = list_memories(limit=1000)
    scored = []
    for entry in candidates:
        haystack = f"{entry['category']} {entry['key']} {entry['value']}"
        entry_tokens = _tokens(haystack)
        overlap = len(query_tokens & entry_tokens)
        phrase = 1.0 if query_text and query_text.casefold() in haystack.casefold() else 0.0
        if query_tokens and overlap == 0 and not phrase:
            continue
        relevance = (overlap / max(1, len(query_tokens))) * 5.0 + phrase * 3.0
        relevance += float(entry.get("importance", 0.5))
        if entry["category"] in {"identity", "projects"}:
            relevance += 0.2
        scored.append((relevance, entry))
    scored.sort(key=lambda item: (item[0], item[1].get("updated_at", "")), reverse=True)
    selected = [entry for _score, entry in scored[: max(1, min(30, int(limit)))] ]
    if selected:
        ids = [entry["id"] for entry in selected]
        placeholders = ",".join("?" for _ in ids)
        with _lock, _database() as connection:
            connection.execute(
                f"UPDATE memories SET access_count=access_count+1, last_accessed_at=? WHERE id IN ({placeholders})",
                [_now(), *ids],
            )
    return selected


def delete_memory(memory_id: int | None = None, category: str = "", key: str = "") -> bool:
    _ensure_database()
    with _lock, _database() as connection:
        if memory_id is not None:
            cursor = connection.execute("DELETE FROM memories WHERE id=?", (int(memory_id),))
        else:
            cursor = connection.execute(
                "DELETE FROM memories WHERE category=? AND key=?",
                (_normalize_category(category), _normalize_key(key)),
            )
    return cursor.rowcount > 0


def clear_memories() -> int:
    _ensure_database()
    with _lock, _database() as connection:
        count = int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        connection.execute("DELETE FROM memories")
    return count


def format_memory_for_prompt(
    memory: dict | None = None,
    query: str = "",
    limit: int = 30,
) -> str:
    if query:
        entries = search_memories(query, limit=min(limit, 12))
    elif memory is not None:
        entries = []
        for category, items in memory.items():
            if not isinstance(items, dict):
                continue
            for key, raw_entry in items.items():
                entry = raw_entry if isinstance(raw_entry, dict) else {"value": raw_entry}
                entries.append(
                    {
                        "category": category,
                        "key": key,
                        "value": entry.get("value", ""),
                        "importance": entry.get("importance", 0.5),
                        "updated_at": entry.get("updated", ""),
                    }
                )
        entries.sort(
            key=lambda entry: (float(entry.get("importance", 0.5)), entry.get("updated_at", "")),
            reverse=True,
        )
        entries = entries[:limit]
    else:
        entries = list_memories(limit=limit)
    if not entries:
        return ""
    title = "[RELEVANT LONG-TERM MEMORY]" if query else "[LONG-TERM MEMORY OVERVIEW]"
    lines = [title, "Use only when relevant. Never reveal that this is an internal memory store."]
    for entry in entries:
        lines.append(
            f"- {entry['category']}/{entry['key']}: {_clean_value(entry['value'])}"
        )
        if len("\n".join(lines)) >= MAX_PROMPT_CHARS:
            break
    return "\n".join(lines)[:MAX_PROMPT_CHARS] + "\n"


def remember(
    key: str,
    value: str,
    category: str = "notes",
    importance: float = 0.5,
) -> str:
    normalized_category = _normalize_category(category)
    normalized_key = _normalize_key(key)
    if _is_sensitive(normalized_category, normalized_key, _clean_value(value)):
        return "Sensitive information was not stored."
    before = list_memories(limit=1000)
    update_memory(
        {
            normalized_category: {
                normalized_key: {
                    "value": value,
                    "importance": importance,
                    "source": "manual",
                }
            }
        }
    )
    after = list_memories(limit=1000)
    return "Remembered." if len(after) >= len(before) else "Memory was not stored."


def forget(key: str, category: str = "notes") -> str:
    if delete_memory(category=category, key=key):
        return f"Forgotten: {_normalize_category(category)}/{_normalize_key(key)}"
    return f"Not found: {_normalize_category(category)}/{_normalize_key(key)}"


forget_memory = forget


def should_extract_memory(user_text: str, jarvis_text: str, api_key: str = "") -> bool:
    try:
        from omar_ai_core.ai.openrouter_gateway import client

        combined = f"User: {user_text[:300]}\nJarvis: {jarvis_text[:1000]}"
        result = client.chat(
            "Does this conversation contain a durable personal preference, identity fact, "
            "ongoing project, relationship, goal, or useful long-term note? Reply only YES or NO.\n\n"
            f"Conversation:\n{combined}",
            system="You are a memory relevance checker. Reply only YES or NO.",
            max_tokens=5,
            temperature=0.0,
        )
        return "YES" in result.upper()
    except Exception as exc:
        print(f"[Memory] Relevance check failed: {exc}")
        return False


def extract_memory(user_text: str, jarvis_text: str, api_key: str = "") -> dict:
    try:
        from omar_ai_core.ai.openrouter_gateway import client

        combined = f"User: {user_text[:600]}\nJarvis: {jarvis_text[:500]}"
        raw = client.chat(
            "Extract only durable, non-sensitive memories from this conversation. "
            "Never store passwords, API keys, authentication tokens, payment details, or one-time commands. "
            "Return JSON grouped into identity, preferences, projects, relationships, wishes, and notes. "
            "Each item must contain value and importance from 0.0 to 1.0. Return {} when there is nothing useful.\n\n"
            f"Conversation:\n{combined}",
            system="Return only valid JSON without markdown.",
            max_tokens=1200,
            temperature=0.1,
        )
        clean = re.sub(r"```(?:json)?", "", raw.strip()).strip().rstrip("`").strip()
        data = json.loads(clean or "{}")
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}
    except Exception as exc:
        if "429" not in str(exc):
            print(f"[Memory] Extraction failed: {exc}")
        return {}
