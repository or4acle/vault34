"""SQLite storage for the Vault34 index.

The schema is normalised on purpose: tags live in their own table and are
joined through ``media_tags``. That makes tag search and autocomplete real SQL
operations instead of JSON scans over a blob column.

An FTS5 mirror of filename + tag text is maintained alongside the media table
so free-text queries stay fast as the library grows.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS media (
    id           INTEGER PRIMARY KEY,
    path         TEXT    NOT NULL UNIQUE,
    filename     TEXT    NOT NULL,
    ext          TEXT    NOT NULL DEFAULT '',
    kind         TEXT    NOT NULL DEFAULT 'image',
    animated     INTEGER NOT NULL DEFAULT 0,
    file_size    INTEGER NOT NULL DEFAULT 0,
    mtime        REAL    NOT NULL DEFAULT 0,
    width        INTEGER NOT NULL DEFAULT 0,
    height       INTEGER NOT NULL DEFAULT 0,
    duration     REAL,
    sha256       TEXT,
    phash        TEXT,
    dhash        TEXT,
    rating       TEXT,
    thumb        TEXT,
    status       TEXT    NOT NULL DEFAULT 'pending',
    duplicate_of INTEGER REFERENCES media(id) ON DELETE SET NULL,
    error        TEXT,
    added_at     REAL    NOT NULL,
    indexed_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_media_sha    ON media(sha256);
CREATE INDEX IF NOT EXISTS idx_media_phash  ON media(phash);
CREATE INDEX IF NOT EXISTS idx_media_status ON media(status);
CREATE INDEX IF NOT EXISTS idx_media_kind   ON media(kind);
CREATE INDEX IF NOT EXISTS idx_media_name   ON media(filename);

CREATE TABLE IF NOT EXISTS tags (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL DEFAULT 'general'
);

CREATE TABLE IF NOT EXISTS media_tags (
    media_id   INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id)   ON DELETE CASCADE,
    confidence REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (media_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_media_tags_tag ON media_tags(tag_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS queue (
    id       INTEGER PRIMARY KEY,
    path     TEXT NOT NULL,
    added_at REAL NOT NULL,
    tries    INTEGER NOT NULL DEFAULT 0,
    error    TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_id ON queue(id);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS media_fts
USING fts5(text, media_id UNINDEXED, tokenize='unicode61');
"""


class Database:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        try:
            self._conn.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError:
            pass  # FTS5 unavailable: free-text search degrades to LIKE
        self._conn.execute(
            "INSERT INTO settings(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    # -- plumbing -------------------------------------------------------
    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _script(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)
            self._conn.commit()

    # -- settings -------------------------------------------------------
    def get_setting(self, key: str, default=None):
        rows = self._query("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key: str, value: str) -> None:
        self._execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    # -- media ----------------------------------------------------------
    def upsert_media(self, **fields) -> int:
        """Insert or update a media row; returns its id.

        ``tags`` may be supplied as a list of ``(name, category, confidence)``
        which is written to the normalised tag tables in the same transaction.
        """
        tags = fields.pop("tags", None)
        allowed = {
            "path", "filename", "ext", "kind", "animated", "file_size", "mtime",
            "width", "height", "duration", "sha256", "phash", "dhash", "rating",
            "thumb", "status", "duplicate_of", "error", "added_at", "indexed_at",
        }
        data = {k: v for k, v in fields.items() if k in allowed}
        # Normalise once so both the INSERT and UPDATE branches see the same
        # types; sqlite3 cannot bind a Path.
        if data.get("path") is not None:
            data["path"] = str(data["path"])
        if data.get("thumb") is not None:
            data["thumb"] = str(data["thumb"])
        data.setdefault("added_at", time.time())
        if data.get("status") == "ready" and "indexed_at" not in data:
            data["indexed_at"] = time.time()

        with self._lock:
            path_value = data.get("path", "")
            existing = self._conn.execute("SELECT id FROM media WHERE path=?",
                                          (path_value,)).fetchone()
            if existing:
                media_id = existing["id"]
                if data:
                    cols = ", ".join(f"{k}=?" for k in data)
                    self._conn.execute(f"UPDATE media SET {cols} WHERE id=?",
                                       (*data.values(), media_id))
            else:
                # `path` is already a key of `data`; listing it twice makes
                # SQLite silently keep the first binding, so build the row once.
                row = {"path": path_value, **{k: v for k, v in data.items() if k != "path"}}
                cols = ", ".join(row)
                marks = ", ".join("?" * len(row))
                cur = self._conn.execute(f"INSERT INTO media({cols}) VALUES({marks})",
                                         tuple(row.values()))
                media_id = cur.lastrowid

            if tags is not None:
                self._write_tags(media_id, tags)
            self._conn.commit()
        return media_id

    def _write_tags(self, media_id: int, tags) -> None:
        self._conn.execute("DELETE FROM media_tags WHERE media_id=?", (media_id,))
        if not tags:
            self._conn.execute("DELETE FROM media_fts WHERE media_id=?", (media_id,))
            return

        names: list[str] = []
        for tag in tags:
            name, category, confidence = tag
            cur = self._conn.execute(
                "INSERT INTO tags(name, category) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET category=excluded.category "
                "RETURNING id",
                (name, category),
            ).fetchone()
            tag_id = cur[0] if cur else self._conn.execute(
                "SELECT id FROM tags WHERE name=?", (name,)).fetchone()[0]
            self._conn.execute(
                "INSERT INTO media_tags(media_id, tag_id, confidence) VALUES(?, ?, ?) "
                "ON CONFLICT(media_id, tag_id) DO UPDATE SET confidence=excluded.confidence",
                (media_id, tag_id, float(confidence)),
            )
            names.append(name.replace("_", " "))

        self._conn.execute("DELETE FROM media_fts WHERE media_id=?", (media_id,))
        filename = self._conn.execute("SELECT filename FROM media WHERE id=?",
                                      (media_id,)).fetchone()[0]
        self._conn.execute(
            "INSERT INTO media_fts(text, media_id) VALUES(?, ?)",
            (" ".join([filename.replace("_", " ")] + names), media_id),
        )

    def get_media(self, media_id: int) -> dict | None:
        rows = self._query("SELECT * FROM media WHERE id=?", (media_id,))
        return self._hydrate(rows[0]) if rows else None

    def get_by_path(self, path: str | Path) -> dict | None:
        rows = self._query("SELECT * FROM media WHERE path=?", (str(path),))
        return self._hydrate(rows[0]) if rows else None

    def get_by_sha256(self, sha: str) -> dict | None:
        rows = self._query(
            "SELECT * FROM media WHERE sha256=? AND status='ready' LIMIT 1", (sha,))
        return self._hydrate(rows[0]) if rows else None

    def iter_hashes(self):
        """Rows the duplicate matcher needs, for rebuilding it on startup.

        ``sha256`` is required or exact-duplicate detection silently degrades
        to perceptual-only after a restart. Rows are kept when either hash is
        present, so a video whose frame could not be decoded is still matched
        byte-for-byte.
        """
        return self._query("SELECT id, path, sha256, phash, dhash FROM media "
                           "WHERE phash IS NOT NULL OR sha256 IS NOT NULL")

    def _hydrate(self, row: sqlite3.Row) -> dict:
        item = dict(row)
        item["animated"] = bool(item.get("animated"))
        rows = self._query(
            "SELECT t.name, t.category, mt.confidence "
            "FROM media_tags mt JOIN tags t ON t.id = mt.tag_id "
            "WHERE mt.media_id=? ORDER BY mt.confidence DESC",
            (item["id"],),
        )
        item["tags"] = [{"name": r["name"], "category": r["category"],
                         "confidence": round(r["confidence"], 4)} for r in rows]
        item["tag_names"] = [t["name"] for t in item["tags"]]
        return item

    def delete_media(self, media_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM media WHERE id=?", (media_id,))
            self._conn.execute("DELETE FROM media_fts WHERE media_id=?", (media_id,))
            self._conn.execute("UPDATE media SET duplicate_of=NULL WHERE duplicate_of=?",
                               (media_id,))
            self._conn.commit()

    # -- search ---------------------------------------------------------
    def search(self, query: str = "", tags: list | None = None, kinds: list | None = None,
               limit: int = 60, offset: int = 0, min_confidence: float = 0.0,
               sort: str = "recent") -> dict:
        """Search by free text and/or explicit tag filters.

        Every requested tag must be present (AND semantics), which is what users
        expect from a tag browser. Results are ranked by tag relevance then
        recency.
        """
        where: list[str] = ["m.status='ready'"]
        params: list = []
        score_expr = "0.0"
        score_params: list = []

        if query:
            like = f"%{query.lower()}%"
            where.append(
                "(lower(m.filename) LIKE ? OR EXISTS("
                "  SELECT 1 FROM media_fts f WHERE f.media_id = m.id AND media_fts MATCH ?))"
            )
            params.extend([like, self._fts_query(query)])

        for tag in tags or []:
            where.append(
                "EXISTS(SELECT 1 FROM media_tags mt JOIN tags t ON t.id=mt.tag_id "
                "WHERE mt.media_id=m.id AND t.name=?)"
            )
            params.append(tag)
            score_expr += " + 1.0"
            score_params.append(0)

        if kinds:
            where.append(f"m.kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)

        if min_confidence > 0:
            where.append("EXISTS(SELECT 1 FROM media_tags mt WHERE mt.media_id=m.id "
                         "AND mt.confidence >= ?)")
            params.append(min_confidence)

        order = {
            "recent": "m.indexed_at DESC, m.id DESC",
            "added": "m.added_at DESC, m.id DESC",
            "name": "m.filename COLLATE NOCASE ASC",
            "size": "m.file_size DESC",
            "relevance": "score DESC, m.indexed_at DESC",
        }.get(sort, "m.indexed_at DESC")

        sql = (
            f"SELECT m.*, ({score_expr}) AS score FROM media m "
            f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?"
        )
        rows = self._query(sql, (*params, int(limit), int(offset)))
        total = self._count(" AND ".join(where), params)
        return {
            "items": [self._hydrate(r) for r in rows],
            "total": total,
            "limit": int(limit),
            "offset": int(offset),
        }

    def _count(self, where: str, params: list) -> int:
        rows = self._query(f"SELECT COUNT(*) FROM media m WHERE {where}", tuple(params))
        return rows[0][0] if rows else 0

    @staticmethod
    def _fts_query(text: str) -> str:
        tokens = [t for t in "".join(c if c.isalnum() else " " for c in text).split() if t]
        return " OR ".join(f'"{t}"' for t in tokens) if tokens else '""'

    def autocomplete(self, prefix: str = "", limit: int = 25) -> list[dict]:
        """Tag completion ranked by usage frequency, then alphabetically."""
        prefix = prefix.strip().lower()
        if prefix:
            rows = self._query(
                "SELECT t.name, t.category, COUNT(mt.media_id) AS uses "
                "FROM tags t LEFT JOIN media_tags mt ON mt.tag_id=t.id "
                "WHERE lower(t.name) LIKE ? "
                "GROUP BY t.name ORDER BY uses DESC, t.name ASC LIMIT ?",
                (f"{prefix}%", int(limit)),
            )
        else:
            rows = self._query(
                "SELECT t.name, t.category, COUNT(mt.media_id) AS uses "
                "FROM tags t LEFT JOIN media_tags mt ON mt.tag_id=t.id "
                "GROUP BY t.name ORDER BY uses DESC, t.name ASC LIMIT ?",
                (int(limit),),
            )
        return [{"name": r["name"], "category": r["category"], "uses": r["uses"]}
                for r in rows]

    def similar_tags(self, tag: str, limit: int = 12) -> list[dict]:
        """Tags that co-occur most often with ``tag`` - powers 'related' search."""
        rows = self._query(
            "SELECT t2.name, t2.category, COUNT(*) AS n "
            "FROM media_tags m1 "
            "JOIN tags t1 ON t1.id = m1.tag_id "
            "JOIN media_tags m2 ON m2.media_id = m1.media_id AND m2.tag_id != m1.tag_id "
            "JOIN tags t2 ON t2.id = m2.tag_id "
            "WHERE t1.name = ? "
            "GROUP BY t2.name ORDER BY n DESC, t2.name ASC LIMIT ?",
            (tag, int(limit)),
        )
        return [{"name": r["name"], "category": r["category"], "count": r["n"]}
                for r in rows]

    def top_tags(self, limit: int = 40, category: str | None = None) -> list[dict]:
        if category:
            rows = self._query(
                "SELECT t.name, t.category, COUNT(mt.media_id) AS uses "
                "FROM media_tags mt JOIN tags t ON t.id=mt.tag_id "
                "WHERE t.category=? GROUP BY t.name ORDER BY uses DESC, t.name ASC LIMIT ?",
                (category, int(limit)))
        else:
            rows = self._query(
                "SELECT t.name, t.category, COUNT(mt.media_id) AS uses "
                "FROM media_tags mt JOIN tags t ON t.id=mt.tag_id "
                "GROUP BY t.name ORDER BY uses DESC, t.name ASC LIMIT ?",
                (int(limit),))
        return [{"name": r["name"], "category": r["category"], "uses": r["uses"]}
                for r in rows]

    # -- duplicates -----------------------------------------------------
    def duplicate_groups(self) -> list[list[dict]]:
        """Group visually similar media using Hamming distance on perceptual hashes."""
        candidates = [dict(r) for r in self.iter_hashes()]
        buckets: dict[str, list[dict]] = {}
        for item in candidates:
            if item.get("phash"):
                buckets.setdefault(item["phash"][:8], []).append(item)

        seen: set[int] = set()
        groups: list[list[dict]] = []
        for bucket in buckets.values():
            for i, left in enumerate(bucket):
                if left["id"] in seen:
                    continue
                cluster = [left]
                for right in bucket[i + 1:]:
                    if right["id"] in seen:
                        continue
                    if _hamming(left.get("phash"), right.get("phash"), 8) <= 8:
                        cluster.append(right)
                        seen.add(right["id"])
                if len(cluster) > 1:
                    seen.add(left["id"])
                    groups.append([self._hydrate(
                        self._query("SELECT * FROM media WHERE id=?", (c["id"],))[0])
                        for c in cluster])
        return groups

    def mark_duplicate(self, media_id: int, original_id: int | None) -> None:
        self._execute("UPDATE media SET duplicate_of=?, status=? WHERE id=?",
                      (original_id, "duplicate" if original_id else "ready", media_id))

    # -- stats ----------------------------------------------------------
    def stats(self) -> dict:
        def scalar(sql: str, params: tuple = ()) -> int:
            rows = self._query(sql, params)
            return int(rows[0][0]) if rows else 0

        return {
            "total": scalar("SELECT COUNT(*) FROM media WHERE status='ready'"),
            "images": scalar("SELECT COUNT(*) FROM media WHERE status='ready' AND kind='image'"),
            "videos": scalar("SELECT COUNT(*) FROM media WHERE status='ready' AND kind='video'"),
            "animated": scalar("SELECT COUNT(*) FROM media WHERE status='ready' AND animated=1"),
            "duplicates": scalar("SELECT COUNT(*) FROM media WHERE status='duplicate'"),
            "failed": scalar("SELECT COUNT(*) FROM media WHERE status='error'"),
            "pending": scalar("SELECT COUNT(*) FROM media WHERE status='pending'"),
            "tagged": scalar("SELECT COUNT(DISTINCT media_id) FROM media_tags"),
            "tags": scalar("SELECT COUNT(*) FROM tags"),
            "bytes": scalar("SELECT COALESCE(SUM(file_size),0) FROM media"),
        }

    # -- queue ----------------------------------------------------------
    def enqueue(self, path: str | Path) -> None:
        self._execute(
            "INSERT INTO queue(path, added_at) VALUES(?, ?) "
            "ON CONFLICT DO NOTHING",
            (str(path), time.time()),
        )

    def claim_next(self) -> sqlite3.Row | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM queue ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM queue WHERE id=?", (row["id"],))
            self._conn.commit()
            return row

    def queue_depth(self) -> int:
        return scalar_count(self, "SELECT COUNT(*) FROM queue")

    def requeue(self, path: str | Path, error: str = "") -> None:
        self._execute(
            "INSERT INTO queue(path, added_at, tries, error) VALUES(?, ?, 1, ?) "
            "ON CONFLICT DO NOTHING",
            (str(path), time.time(), error[:500]),
        )

    def clear_queue(self) -> None:
        self._execute("DELETE FROM queue")


def scalar_count(db: Database, sql: str) -> int:
    rows = db._query(sql)
    return int(rows[0][0]) if rows else 0


def _hamming(a: str | None, b: str | None, limit: int) -> int:
    if not a or not b or len(a) != len(b):
        return 999
    return sum(bin(int(x, 16) ^ int(y, 16)).count("1")
               for x, y in zip(a[:limit], b[:limit]))


def dump_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)
