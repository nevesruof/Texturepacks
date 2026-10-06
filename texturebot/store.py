from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .config import Section
from .models import Pack


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS packs (
                key TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS aliases (
                identity TEXT PRIMARY KEY, pack_key TEXT NOT NULL REFERENCES packs(key)
            );
            CREATE TABLE IF NOT EXISTS sections (
                name TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
                cooldown INTEGER NOT NULL, last_sent REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS jobs (
                section TEXT NOT NULL REFERENCES sections(name),
                pack_key TEXT NOT NULL REFERENCES packs(key),
                status TEXT NOT NULL DEFAULT 'pending',
                queued REAL NOT NULL, available REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, started REAL NOT NULL DEFAULT 0,
                channel_id TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '', PRIMARY KEY(section, pack_key)
            );
            CREATE INDEX IF NOT EXISTS job_status ON jobs(status, section, available);
            CREATE TABLE IF NOT EXISTS cursors (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS health (
                source TEXT PRIMARY KEY, success REAL NOT NULL DEFAULT 0,
                checked REAL NOT NULL, error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS requests (source TEXT NOT NULL, timestamp REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS request_time ON requests(source, timestamp);
            CREATE TABLE IF NOT EXISTS detail_cache (
                source TEXT NOT NULL, source_id TEXT NOT NULL, payload TEXT NOT NULL,
                refreshed REAL NOT NULL, PRIMARY KEY(source, source_id)
            );
        """)

    def close(self):
        self.db.close()

    def configure(self, sections: dict[str, Section]):
        with self.db:
            for section in sections.values():
                self.db.execute(
                    "INSERT OR IGNORE INTO sections(name, cooldown) VALUES (?, ?)",
                    (section.name, section.cooldown_minutes * 60),
                )
        # Existing runtime cooldowns and pause flags intentionally survive restarts.

    def offer(self, section: str, pack: Pack, limit: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.pending_count(section) >= limit:
                return False
            placeholders = ",".join("?" for _ in pack.identities)
            row = self.db.execute(
                f"SELECT pack_key FROM aliases WHERE identity IN ({placeholders}) LIMIT 1", pack.identities,
            ).fetchone()
            key = row["pack_key"] if row else pack.key
            if self.db.execute("SELECT 1 FROM jobs WHERE pack_key=? AND status='posted'", (key,)).fetchone():
                return False
            self.db.execute("INSERT OR IGNORE INTO packs VALUES (?, ?, ?)", (key, json.dumps(pack.to_dict()), now))
            # Keep the first source and creator credit when the same pack appears elsewhere.
            for identity in pack.identities:
                self.db.execute("INSERT OR IGNORE INTO aliases VALUES (?, ?)", (identity, key))
            result = self.db.execute(
                "INSERT OR IGNORE INTO jobs(section, pack_key, queued) VALUES (?, ?, ?)", (section, key, now),
            )
            return result.rowcount == 1

    def pending_count(self, section: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE section=? AND status IN ('pending','sending','uncertain')", (section,),
        ).fetchone()[0]

    def reserve(self, section: Section, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            state = self.db.execute("SELECT * FROM sections WHERE name=?", (section.name,)).fetchone()
            if state["paused"] or (state["last_sent"] and now < state["last_sent"] + state["cooldown"]):
                return None
            # An unresolved send blocks this section until it can be reconciled.
            if self.db.execute(
                "SELECT 1 FROM jobs WHERE section=? AND status IN ('sending','uncertain')", (section.name,),
            ).fetchone():
                return None
            row = self.db.execute("""
                SELECT j.*, p.payload FROM jobs j JOIN packs p ON p.key=j.pack_key
                WHERE j.section=? AND j.status='pending' AND j.available<=?
                AND NOT EXISTS (
                    SELECT 1 FROM jobs other WHERE other.pack_key=j.pack_key
                    AND other.status IN ('posted','sending','uncertain')
                ) ORDER BY j.queued, j.pack_key LIMIT 1
            """, (section.name, now)).fetchone()
            if row is None:
                return None
            self.db.execute("""
                UPDATE jobs SET status='sending', started=?, channel_id=?, attempts=attempts+1
                WHERE section=? AND pack_key=?
            """, (now, str(section.channel_id), section.name, row["pack_key"]))
            return {**dict(row), "started": now, "channel_id": str(section.channel_id),
                    "pack": Pack.from_dict(json.loads(row["payload"]))}

    def complete(self, job: dict, message_id: str, sent_at: float | None = None):
        sent_at = time.time() if sent_at is None else sent_at
        with self.db:
            self.db.execute("""
                UPDATE jobs SET status='posted', message_id=?, error=''
                WHERE section=? AND pack_key=?
            """, (str(message_id), job["section"], job["pack_key"]))
            self.db.execute("UPDATE sections SET last_sent=MAX(last_sent, ?) WHERE name=?", (sent_at, job["section"]))
            self.db.execute("""
                UPDATE jobs SET status='duplicate' WHERE pack_key=? AND status='pending'
            """, (job["pack_key"],))

    def defer(self, job: dict, error: str, *, uncertain: bool = False, now: float | None = None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("""
                UPDATE jobs SET status=?, available=?, error=? WHERE section=? AND pack_key=?
            """, ("uncertain" if uncertain else "pending", now + 300, error[:300], job["section"], job["pack_key"]))

    def discard(self, job: dict, reason: str):
        with self.db:
            self.db.execute("UPDATE jobs SET status='skipped', error=? WHERE section=? AND pack_key=?",
                            (reason[:300], job["section"], job["pack_key"]))

    def interrupted(self) -> list[dict]:
        return [{**dict(row), "pack": Pack.from_dict(json.loads(row["payload"]))}
                for row in self.db.execute("""
                    SELECT j.*, p.payload FROM jobs j JOIN packs p ON p.key=j.pack_key
                    WHERE j.status IN ('sending','uncertain') ORDER BY j.started
                """)]

    def pause(self, section: str, paused: bool):
        with self.db:
            self.db.execute("UPDATE sections SET paused=? WHERE name=?", (int(paused), section))

    def cooldown(self, section: str, minutes: int):
        with self.db:
            self.db.execute("UPDATE sections SET cooldown=? WHERE name=?", (minutes * 60, section))

    def status(self) -> dict:
        sections = []
        for state in self.db.execute("SELECT * FROM sections ORDER BY name"):
            counts = {r["status"]: r["n"] for r in self.db.execute(
                "SELECT status, COUNT(*) n FROM jobs WHERE section=? GROUP BY status", (state["name"],),
            )}
            sections.append({**dict(state), "counts": counts})
        return {"sections": sections, "sources": [dict(r) for r in self.db.execute("SELECT * FROM health ORDER BY source")]}

    def queue(self, section: str, limit: int = 5) -> list[Pack]:
        return [Pack.from_dict(json.loads(row[0])) for row in self.db.execute("""
            SELECT p.payload FROM jobs j JOIN packs p ON p.key=j.pack_key
            WHERE j.section=? AND j.status='pending' ORDER BY j.queued, j.pack_key LIMIT ?
        """, (section, limit))]

    def cursor(self, name: str) -> str:
        row = self.db.execute("SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
        return row[0] if row else ""

    def set_cursor(self, name: str, value: str):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?, ?)", (name, str(value)))

    def source_health(self, source: str, error: str = ""):
        now = time.time()
        with self.db:
            self.db.execute("""
                INSERT INTO health(source, success, checked, error) VALUES (?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET checked=excluded.checked, error=excluded.error,
                success=CASE WHEN excluded.error='' THEN excluded.success ELSE health.success END
            """, (source, 0 if error else now, now, error[:300]))

    def request_slot(self, source: str, limit: int) -> bool:
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("DELETE FROM requests WHERE timestamp<?", (now - 86400,))
            count = self.db.execute("SELECT COUNT(*) FROM requests WHERE source=?", (source,)).fetchone()[0]
            if count >= limit:
                return False
            self.db.execute("INSERT INTO requests VALUES (?, ?)", (source, now))
            return True

    def cached(self, pack: Pack) -> Pack | None:
        row = self.db.execute("""
            SELECT payload FROM detail_cache WHERE source=? AND source_id=? AND refreshed>?
        """, (pack.source, pack.source_id, time.time() - 86400)).fetchone()
        return Pack.from_dict(json.loads(row[0])) if row else None

    def cache(self, pack: Pack):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO detail_cache VALUES (?, ?, ?, ?)",
                            (pack.source, pack.source_id, json.dumps(pack.to_dict()), time.time()))

    def replace_payload(self, key: str, pack: Pack):
        with self.db:
            self.db.execute("UPDATE packs SET payload=? WHERE key=?", (json.dumps(pack.to_dict()), key))
