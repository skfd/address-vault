"""SQLite catalog: the index of sources and dated snapshots.

Single file in the vault root, WAL mode so consumers can read concurrently while
the scheduler does the occasional short write. A snapshot's tier is two
independent booleans -- ``on_disk`` (a hot copy exists) and ``archived`` (present
in restic, true forever once swept) -- because a thaw *copies* out of the archive,
so a day can be both at once.

The ``leases`` table is the cross-process gate: one row per slug, claimed with a
``BEGIN IMMEDIATE`` transaction so that among many consumer processes only one
writes a given city at a time. The rest read the row to report progress. The
connection runs in autocommit mode (``isolation_level=None``) so that explicit
transaction is honoured exactly.
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
  unchanged_since TEXT, fetched_at TEXT, sketch BLOB,
  distinct_addresses INTEGER, field_names TEXT,
  PRIMARY KEY(slug, date)
);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, kind TEXT, slug TEXT, date TEXT, state TEXT, detail TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS leases(
  slug TEXT PRIMARY KEY, kind TEXT, state TEXT, holder TEXT, date TEXT, detail TEXT,
  started_at TEXT, updated_at TEXT
);
"""

# A lease is finished once its writer stops: it succeeded ("done"), the source
# failed ("failed"), the data came back untrustworthy and was thrown away
# ("refused" -- see fetch.DegradedPull), or the link went down under it
# ("deferred" -- no attempt to hold against the source). Any other state is
# live. Shared with ``PullStatus.active``: a state missing here leaves the slug
# wedged.
TERMINAL_STATES = ("done", "failed", "refused", "deferred")

# What a person can conclude about a flagged day, once they have looked at it.
# The vault flags a large change but cannot say what it means; these are the
# answers, recorded as `jobs` rows of kind "review" so no schema change is
# needed and the verdict travels with the vault rather than with one consumer.
#
#   artifact  what tripped the flag is not true of the world -- a source
#             replaying part of its own load, serving damage, or (for the
#             seeding days) the vault's own history. Do not publish it.
#   schema    the source really did change what it publishes: fields added,
#             removed or reshaped. Real, and not in itself an address change.
#   real      the addresses really did move. Publish it.
#
# The distinction that matters downstream is that only "real" is a story. The
# vault records which of the three it is; what to do about it belongs to the
# consumer, which is why this is a label and never a filter.
#
# A verdict names what the day most consequentially was, and one day can be
# more than one thing -- huron 2026-08-22 added a column *and* seven addresses.
# It deliberately does NOT claim the rest of the day stood still: for two
# months renfrew 2026-06-29 read as `schema` while carrying 27 new rows, and
# the wording here ("no address changed") was part of why nobody looked.
#
# So the verdict is never the whole account. ``report --json`` carries
# ``addresses`` on every flagged day -- net movement and rewrite share, as
# numbers, whatever the verdict says -- precisely so a label cannot conceal
# what moved underneath it. Read the two together; a `schema` or `artifact`
# day with non-zero net movement is a normal reading, not a contradiction,
# and matters because those two are what a consumer suppresses on.
VERDICTS = ("artifact", "schema", "real")

_SNAP_COLS = (
    "slug", "date", "sha256", "features", "bytes",
    "src_last_modified", "src_content_length",
    "on_disk", "archived", "restored_until", "unchanged_since", "fetched_at",
    "sketch", "distinct_addresses", "field_names",
)

# Columns added to `snapshots` after the first vaults were built. SCHEMA above
# creates them for a new vault; an existing one gets them here. Both paths must
# agree, so a column added there needs a line here too.
_ADDED_COLS = (("sketch", "BLOB"),
               ("distinct_addresses", "INTEGER"),
               ("field_names", "TEXT"))


def _now():
    return datetime.now(timezone.utc).isoformat()


class Catalog:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        # autocommit: each write is its own transaction, and the explicit
        # BEGIN IMMEDIATE in acquire_lease is the only multi-statement one.
        self.conn = sqlite3.connect(db_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()

    def _add_missing_columns(self):
        """Bring an existing vault's `snapshots` up to SCHEMA.

        ``CREATE TABLE IF NOT EXISTS`` leaves an already-built table alone, so a
        new column has to be added explicitly. Older rows keep NULL, which every
        reader already has to handle -- a snapshot pulled before the column
        existed genuinely has no value for it.
        """
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(snapshots)")}
        for col, decl in _ADDED_COLS:
            if col not in have:
                self.conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {decl}")

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

    # `sketch`, `distinct_addresses` and `field_names` all come from one pass
    # over a snapshot's file, so they go missing and get filled together.
    _MISSING_FINGERPRINT = (
        "sketch IS NULL OR distinct_addresses IS NULL OR field_names IS NULL")

    def needing_fingerprints(self, slugs=None):
        """On-disk canonical snapshots missing any fingerprint column.

        Canonical only: a dedup day stores no file of its own, and takes its
        fingerprints from the day it points at (see ``inherit_fingerprints``)."""
        q = ("SELECT * FROM snapshots WHERE on_disk=1 AND unchanged_since IS NULL "
             f"AND ({self._MISSING_FINGERPRINT})")
        args = []
        if slugs:
            q += f" AND slug IN ({','.join('?' * len(slugs))})"
            args = list(slugs)
        return self.conn.execute(q + " ORDER BY slug, date", args).fetchall()

    def inherit_fingerprints(self):
        """Hand each dedup day the fingerprints of the day it points at.

        The content is byte-identical, so the fingerprints are too. This is the
        same inheritance a dedup pull does, applied to rows recorded before
        their canonical day had anything to lend -- which is why it runs after
        the reducing pass, not before. Returns the pairs it filled."""
        rows = self.conn.execute(
            "SELECT d.slug, d.date, c.sketch, c.distinct_addresses, c.field_names "
            "FROM snapshots d "
            "JOIN snapshots c ON c.slug=d.slug AND c.date=d.unchanged_since "
            "WHERE (d.sketch IS NULL OR d.distinct_addresses IS NULL "
            "       OR d.field_names IS NULL) "
            "  AND c.sketch IS NOT NULL AND c.distinct_addresses IS NOT NULL "
            "  AND c.field_names IS NOT NULL"
        ).fetchall()
        self.conn.executemany(
            "UPDATE snapshots SET sketch=?, distinct_addresses=?, field_names=? "
            "WHERE slug=? AND date=?",
            [(r["sketch"], r["distinct_addresses"], r["field_names"],
              r["slug"], r["date"]) for r in rows])
        self.conn.commit()
        return [(r["slug"], r["date"]) for r in rows]

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

    def failure_streaks(self):
        """``slug -> {"days", "since", "last_good", "detail"}`` for every city
        whose most recent pull day ended without a successful pull.

        ``days`` counts consecutive pull days, newest first, on which every pull
        failed or was refused -- one success anywhere in a day (a retry, an
        off-schedule pull) ends it. Days with no pull at all (machine off, link
        down) are skipped rather than counted or treated as a break: they say
        nothing about the source. This is the number that tells a source that
        hiccuped today from one that has been down for three weeks
        (west-parry-sound, 2026-09-24..10-15), which the per-day matrix shows
        only as a row of red cells."""
        days = {}
        for r in self.conn.execute(
                "SELECT slug, date, MAX(state='done') AS ok FROM jobs "
                "WHERE kind='pull' AND state IN ('done','failed','refused') "
                "GROUP BY slug, date ORDER BY slug, date DESC"):
            days.setdefault(r["slug"], []).append((r["date"], bool(r["ok"])))
        out = {}
        for slug, rows in days.items():
            n = 0
            for _, ok in rows:
                if ok:
                    break
                n += 1
            if not n:
                continue
            last_good = next((d for d, ok in rows if ok), None)
            detail = self.conn.execute(
                "SELECT detail FROM jobs WHERE kind='pull' AND slug=? "
                "AND state IN ('failed','refused') ORDER BY created_at DESC LIMIT 1",
                (slug,)).fetchone()
            out[slug] = {"days": n, "since": rows[n - 1][0], "last_good": last_good,
                         "detail": detail["detail"] if detail else ""}
        return out

    def record_review(self, slug, date, verdict, note):
        """Record a person's conclusion about a flagged day. See ``VERDICTS``."""
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
        if not (note or "").strip():
            # A verdict with no reasoning is a dismiss button, and a list that
            # has been dismissed clean reads as "nothing outstanding" while
            # being exactly as unexplained as before.
            raise ValueError("a review needs a note saying what it was")
        self.record_job("review", slug, date, verdict, detail=note.strip())

    def reviews(self):
        """``(slug, date) -> row`` of the latest verdict on each flagged day.

        Later supersedes earlier: a day can be looked at again when more is
        known, and the newest reading is the one that counts. Nothing is ever
        deleted, so the earlier readings stay in the job log as history."""
        return {(r["slug"], r["date"]): r for r in self.conn.execute(
            "SELECT slug, date, state, detail, created_at FROM jobs "
            "WHERE kind='review' ORDER BY created_at")}

    # --- leases (the per-slug single-writer gate) ---
    def acquire_lease(self, slug, kind, date, holder, ttl_seconds, state):
        """Atomically claim the write lease for ``slug``. Returns True if this
        caller now holds it. A row that is terminal (done/failed) or stale (no
        state change within ``ttl_seconds``, i.e. its holder likely died) is
        reclaimable; a live, fresh row is not."""
        now = datetime.now(timezone.utc)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:  # another process holds the write lock
            return False
        try:
            row = self.conn.execute(
                "SELECT state, updated_at FROM leases WHERE slug=?", (slug,)
            ).fetchone()
            if row is not None and row["state"] not in TERMINAL_STATES:
                age = (now - datetime.fromisoformat(row["updated_at"])).total_seconds()
                if age <= ttl_seconds:
                    self.conn.execute("COMMIT")
                    return False
            now_iso = now.isoformat()
            self.conn.execute(
                "INSERT OR REPLACE INTO leases "
                "(slug, kind, state, holder, date, detail, started_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (slug, kind, state, holder, date, None, now_iso, now_iso),
            )
            self.conn.execute("COMMIT")
            return True
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def set_lease_state(self, slug, state, detail=None):
        self.conn.execute(
            "UPDATE leases SET state=?, detail=?, updated_at=? WHERE slug=?",
            (state, detail, _now(), slug),
        )

    def get_lease(self, slug):
        return self.conn.execute("SELECT * FROM leases WHERE slug=?", (slug,)).fetchone()

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
