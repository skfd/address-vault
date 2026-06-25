"""SQLite catalog: the index of sources and dated snapshots.

Single file in the vault root, WAL mode so consumers can read concurrently while
the scheduler does the occasional short write. A snapshot's tier is two
independent booleans -- ``on_disk`` (a hot copy exists) and ``archived`` (present
in restic, true forever once swept) -- because a thaw *copies* out of the archive,
so a day can be both at once.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from addressvault import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  slug TEXT PRIMARY KEY, provider TEXT, data_url TEXT, access TEXT, format TEXT,
  source_crs TEXT, fields_json TEXT, license_name TEXT,
  schedule TEXT, enabled INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS snapshots(
  slug TEXT, date TEXT,
  sha256 TEXT, features INTEGER, bytes INTEGER,
  src_last_modified TEXT, src_content_length INTEGER,
  on_disk INTEGER, archived INTEGER, restored_until TEXT,
  unchanged_since TEXT, fetched_at TEXT,
  PRIMARY KEY(slug, date)
);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, kind TEXT, slug TEXT, date TEXT, state TEXT, detail TEXT,
  created_at TEXT, updated_at TEXT
);
"""

_SNAP_COLS = (
    "slug", "date", "sha256", "features", "bytes",
    "src_last_modified", "src_content_length",
    "on_disk", "archived", "restored_until", "unchanged_since", "fetched_at",
)


def _now():
    return datetime.now(timezone.utc).isoformat()


class Catalog:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # --- sources ---
    def upsert_source(self, src):
        self.conn.execute(
            """INSERT INTO sources
               (slug, provider, data_url, access, format, source_crs, fields_json,
                license_name, schedule, enabled, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET
                 provider=excluded.provider, data_url=excluded.data_url,
                 access=excluded.access, format=excluded.format,
                 source_crs=excluded.source_crs, fields_json=excluded.fields_json,
                 license_name=excluded.license_name, schedule=excluded.schedule,
                 enabled=excluded.enabled""",
            (src.slug, src.provider, src.data_url, src.access, src.format,
             src.source_crs, json.dumps(src.fields), src.license_name,
             src.schedule, int(src.enabled), _now()),
        )
        self.conn.commit()

    def get_source(self, slug):
        return self.conn.execute("SELECT * FROM sources WHERE slug=?", (slug,)).fetchone()

    def list_sources(self):
        return self.conn.execute("SELECT * FROM sources ORDER BY slug").fetchall()

    # --- snapshots ---
    def upsert_snapshot(self, **f):
        vals = [f.get(c) for c in _SNAP_COLS]
        placeholders = ",".join("?" * len(_SNAP_COLS))
        self.conn.execute(
            f"INSERT OR REPLACE INTO snapshots ({','.join(_SNAP_COLS)}) VALUES ({placeholders})",
            vals,
        )
        self.conn.commit()

    def set_snapshot(self, slug, date, **changes):
        cols = ",".join(f"{k}=?" for k in changes)
        self.conn.execute(
            f"UPDATE snapshots SET {cols} WHERE slug=? AND date=?",
            (*changes.values(), slug, date),
        )
        self.conn.commit()

    def get_snapshot(self, slug, date):
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE slug=? AND date=?", (slug, date)
        ).fetchone()

    def latest_snapshot(self, slug):
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE slug=? ORDER BY date DESC LIMIT 1", (slug,)
        ).fetchone()

    def snapshot_by_sha(self, slug, sha256, exclude_date=None):
        """Earliest snapshot of this slug with identical content (the canonical day)."""
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE slug=? AND sha256=? AND date<>? "
            "ORDER BY date ASC LIMIT 1",
            (slug, sha256, exclude_date or ""),
        ).fetchone()

    def list_snapshots(self, slug, frm=None, to=None, on_disk=None):
        q = "SELECT * FROM snapshots WHERE slug=?"
        args = [slug]
        if frm:
            q += " AND date>=?"; args.append(frm)
        if to:
            q += " AND date<=?"; args.append(to)
        if on_disk is not None:
            q += " AND on_disk=?"; args.append(int(on_disk))
        q += " ORDER BY date"
        return self.conn.execute(q, args).fetchall()

    def due_for_sweep(self, cutoff_date):
        """Naturally-hot, not-yet-archived canonical snapshots at/older than cutoff."""
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE on_disk=1 AND archived=0 "
            "AND restored_until IS NULL AND unchanged_since IS NULL AND date<=? "
            "ORDER BY slug, date",
            (cutoff_date,),
        ).fetchall()

    def due_for_recool(self, now_iso):
        """Thawed copies whose TTL has elapsed."""
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE on_disk=1 AND restored_until IS NOT NULL "
            "AND restored_until<=? ORDER BY slug, date",
            (now_iso,),
        ).fetchall()

    # --- jobs (operation log; powers stats and audit) ---
    def record_job(self, kind, slug, date, state, detail=""):
        now = _now()
        self.conn.execute(
            "INSERT INTO jobs (id, kind, slug, date, state, detail, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, kind, slug, date, state, detail, now, now),
        )
        self.conn.commit()

    def stats(self):
        c = self.conn
        sources = c.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        hot = c.execute("SELECT COUNT(*) FROM snapshots WHERE on_disk=1").fetchone()[0]
        cold = c.execute(
            "SELECT COUNT(*) FROM snapshots WHERE on_disk=0 AND archived=1"
        ).fetchone()[0]
        hot_bytes = c.execute(
            "SELECT COALESCE(SUM(bytes),0) FROM snapshots WHERE on_disk=1"
        ).fetchone()[0]
        return {
            "sources": sources, "snapshots": total,
            "hot": hot, "cold": cold, "hot_bytes": hot_bytes,
        }
