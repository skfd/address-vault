"""Vault: the library API over the catalog, fetcher, and restic archive.

Reads (``snapshots``/``snapshot``/``path``) need no running process. ``pull``
fetches + content-dedups + records a snapshot. ``sweep`` cools aged hot copies
into restic; ``thaw`` copies a cold day back; ``recool_expired`` drops thawed
copies past their TTL. One writer (the scheduler) avoids restic lock races.
"""

import hashlib
import os
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from addressvault import archive, config
from addressvault.catalog import Catalog
from addressvault.fetch import fetch
from addressvault.sources import Source


class Archived(Exception):
    """Raised by ``Vault.path`` when the requested day is only in the cold tier."""


@dataclass
class Snapshot:
    slug: str
    date: str
    sha256: str
    features: int
    bytes: int
    on_disk: bool
    archived: bool
    restored_until: str | None
    unchanged_since: str | None
    fetched_at: str

    @property
    def tier(self):
        return "hot" if self.on_disk else "cold"

    @classmethod
    def from_row(cls, row):
        return cls(
            row["slug"], row["date"], row["sha256"], row["features"], row["bytes"],
            bool(row["on_disk"]), bool(row["archived"]), row["restored_until"],
            row["unchanged_since"], row["fetched_at"],
        )


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _src_from_row(row):
    import json
    return Source(
        slug=row["slug"], provider=row["provider"], data_url=row["data_url"],
        access=row["access"], format=row["format"], source_crs=row["source_crs"] or "",
        fields=json.loads(row["fields_json"] or "{}"), license_name=row["license_name"] or "",
        schedule=row["schedule"] or "daily", enabled=bool(row["enabled"]),
    )


class Vault:
    def __init__(self, dir=None):
        self.root = config.resolve_dir(dir)
        os.makedirs(self.root, exist_ok=True)
        self.cat = Catalog(config.db_path(self.root))

    # --- registry ---
    def seed(self, datasets_dir):
        from addressvault import sources
        srcs = sources.load_dir(datasets_dir)
        for s in srcs:
            self.cat.upsert_source(s)
        return srcs

    def add_source(self, source):
        self.cat.upsert_source(source)

    def sources(self):
        return [_src_from_row(r) for r in self.cat.list_sources()]

    def source(self, slug):
        row = self.cat.get_source(slug)
        if not row:
            raise LookupError(f"unknown source: {slug}")
        return _src_from_row(row)

    # --- reads ---
    def snapshots(self, slug, frm=None, to=None, tier=None):
        on_disk = {"hot": True, "cold": False}.get(tier) if tier else None
        rows = self.cat.list_snapshots(slug, frm=frm, to=to, on_disk=on_disk)
        return [Snapshot.from_row(r) for r in rows]

    def snapshot(self, slug, date="latest"):
        row = self.cat.latest_snapshot(slug) if date == "latest" \
            else self.cat.get_snapshot(slug, date)
        if not row:
            raise LookupError(f"no snapshot {slug} {date}")
        return Snapshot.from_row(row)

    def path(self, slug, date="latest"):
        """Hot file path for a day. Follows the unchanged-content pointer; raises
        Archived if the resolved canonical copy is only in the cold tier."""
        snap = self.snapshot(slug, date)
        canon = snap.unchanged_since or snap.date
        crow = self.cat.get_snapshot(slug, canon)
        if crow and crow["on_disk"]:
            return config.snapshot_path(self.root, slug, canon)
        raise Archived(f"{slug} {canon} is cold; thaw it first")

    # --- writes ---
    def pull(self, slug, force=False, today=None):
        src = self.source(slug)
        day = today or date.today().isoformat()
        latest = self.cat.latest_snapshot(slug)
        prev = None
        if latest and latest["on_disk"]:
            prev = {"last_modified": latest["src_last_modified"],
                    "content_length": latest["src_content_length"],
                    "path": config.snapshot_path(self.root, slug, latest["date"])}

        path, count, headers = fetch(src, self.root, prev=prev, force=force, today=day)
        sha = _sha256(path)
        today_path = config.snapshot_path(self.root, slug, day)
        prior = self.cat.snapshot_by_sha(slug, sha, exclude_date=day)

        if prior:  # identical content already on record -> dedup, store no second copy
            canon = prior["unchanged_since"] or prior["date"]
            if path == today_path and os.path.isfile(today_path) and day != canon:
                os.remove(today_path)
            self.cat.upsert_snapshot(
                slug=slug, date=day, sha256=sha,
                features=prior["features"], bytes=prior["bytes"],
                src_last_modified=headers.get("last_modified"),
                src_content_length=headers.get("content_length"),
                on_disk=0, archived=0, restored_until=None,
                unchanged_since=canon, fetched_at=_now(),
            )
        else:
            if path != today_path:  # static reused an existing file; materialise today's
                shutil.copyfile(path, today_path)
            self.cat.upsert_snapshot(
                slug=slug, date=day, sha256=sha,
                features=count, bytes=os.path.getsize(today_path),
                src_last_modified=headers.get("last_modified"),
                src_content_length=headers.get("content_length"),
                on_disk=1, archived=0, restored_until=None,
                unchanged_since=None, fetched_at=_now(),
            )
        self.cat.record_job("pull", slug, day, "done")
        return self.snapshot(slug, day)

    def thaw(self, slug, date, ttl_hours=24):
        snap = self.snapshot(slug, date)
        canon = snap.unchanged_since or snap.date
        crow = self.cat.get_snapshot(slug, canon)
        until = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
        if crow["on_disk"]:
            if crow["restored_until"] is not None:  # already a thawed copy -> extend TTL
                self.cat.set_snapshot(slug, canon, restored_until=until)
            return self.snapshot(slug, canon)
        if not archive.available():
            raise RuntimeError("restic not found; cannot thaw")
        target = config.snapshot_path(self.root, slug, canon)
        if not archive.dump(self.root, slug, canon, target):
            raise RuntimeError(f"thaw failed for {slug} {canon}")
        self.cat.set_snapshot(slug, canon, on_disk=1, restored_until=until)
        self.cat.record_job("thaw", slug, canon, "done")
        return self.snapshot(slug, canon)

    def sweep(self, keep_days=2, today=None):
        day = date.fromisoformat(today) if isinstance(today, str) else (today or date.today())
        cutoff = (day - timedelta(days=keep_days)).isoformat()
        swept = []
        if not archive.available():
            print("  [archive] restic not found; leaving hot snapshots uncompressed")
            return swept
        for row in self.cat.due_for_sweep(cutoff):
            p = config.snapshot_path(self.root, row["slug"], row["date"])
            if not os.path.isfile(p):
                self.cat.set_snapshot(row["slug"], row["date"], on_disk=0)
                continue
            if archive.backup(self.root, p, row["slug"], row["date"]):
                os.remove(p)
                self.cat.set_snapshot(row["slug"], row["date"], on_disk=0, archived=1)
                self.cat.record_job("sweep", row["slug"], row["date"], "done")
                swept.append(self.snapshot(row["slug"], row["date"]))
            else:
                print(f"  [archive] backup failed for {row['slug']} {row['date']}, kept on disk")
        return swept

    def recool_expired(self, now=None):
        now_iso = now or _now()
        recooled = []
        for row in self.cat.due_for_recool(now_iso):
            p = config.snapshot_path(self.root, row["slug"], row["date"])
            if os.path.isfile(p):
                os.remove(p)
            self.cat.set_snapshot(row["slug"], row["date"], on_disk=0, restored_until=None)
            recooled.append(self.snapshot(row["slug"], row["date"]))
        return recooled

    def stats(self):
        return self.cat.stats()
