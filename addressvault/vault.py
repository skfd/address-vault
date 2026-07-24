"""Vault: the library API over the catalog, fetcher, and restic archive.

Reads (``snapshots``/``snapshot``/``path``) need no running process. ``pull``
fetches + content-dedups + records a snapshot. ``sweep`` cools aged hot copies
into restic; ``thaw`` copies a cold day back; ``recool_expired`` drops thawed
copies past their TTL.

Every write takes a per-slug lease (see ``catalog.acquire_lease``) so that among
many consumer processes only one writes a given city at a time: concurrent
``pull``/``thaw`` of a busy slug raise ``PullInProgress`` (carrying a coarse
``PullStatus`` to poll) instead of starting a second fetch or racing restic;
``sweep``/``recool`` skip a busy slug this cycle. A crashed holder's lease goes
stale after ``LEASE_TTL_SECONDS`` and is reclaimed.
"""

import hashlib
import os
import shutil
import socket
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from addressvault import archive, config, net
from addressvault.catalog import Catalog
from addressvault.fetch import fetch
from addressvault.sources import Source

# How long a lease may go without a state change before another caller may
# reclaim it (a holder crash must not wedge a city forever). Must comfortably
# exceed the slowest expected fetch, since a download reports no intermediate
# state under the coarse-progress model.
LEASE_TTL_SECONDS = 1800


class Archived(Exception):
    """Raised by ``Vault.path`` when the requested day is only in the cold tier."""


class PullInProgress(Exception):
    """Raised by a write (pull/thaw) when another caller already holds the
    slug's lease. Carries the live ``PullStatus`` so the caller can poll."""

    def __init__(self, status):
        super().__init__(f"{status.slug}: {status.kind} in progress ({status.state})")
        self.status = status


def _holder():
    return f"{socket.gethostname()}:{os.getpid()}"


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


@dataclass
class PullStatus:
    """A snapshot of a slug's write lease: what is happening and how far along.
    States are coarse: ``fetching``/``writing`` (a pull), ``thawing``,
    ``sweeping``/``recooling``, then terminal ``done``/``failed``."""

    slug: str
    kind: str
    state: str
    holder: str
    date: str
    detail: str | None
    started_at: str
    updated_at: str

    @property
    def active(self):
        return self.state not in ("done", "failed")

    @classmethod
    def from_row(cls, row):
        return cls(
            row["slug"], row["kind"], row["state"], row["holder"],
            row["date"], row["detail"], row["started_at"], row["updated_at"],
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
        self.lease_ttl = LEASE_TTL_SECONDS

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

    def pull_status(self, slug):
        """The current write lease for ``slug`` (progress of an in-flight or the
        last pull/thaw/sweep), or None if the slug has never been written."""
        row = self.cat.get_lease(slug)
        return PullStatus.from_row(row) if row else None

    def _lease_is_stale(self, status):
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(status.updated_at)).total_seconds()
        return age > self.lease_ttl

    def _await_release(self, slug, poll_interval, deadline):
        """Block until ``slug``'s lease is terminal or stale (crashed holder).
        Returns the final status (or None), or raises TimeoutError past deadline."""
        while True:
            st = self.pull_status(slug)
            if st is None or not st.active or self._lease_is_stale(st):
                return st
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for {slug} {st.kind} ({st.state})")
            time.sleep(poll_interval)

    # --- writes ---
    def pull(self, slug, force=False, today=None, *,
             wait=False, poll_interval=2.0, wait_timeout=None):
        """Fetch + dedup + record today's snapshot. If today's snapshot is
        already recorded, return it without fetching (daily cadence, same as
        the scheduler's ``is_due``) unless ``force``. With ``wait=True``, if
        another caller already holds the slug, block until it finishes and
        return its freshly written ``latest`` (coalesce, no second fetch) --
        regardless of ``force``, since a fresh pull just completed -- or, if
        that holder failed or crashed, take over. ``wait=False`` (default)
        raises ``PullInProgress`` instead of blocking."""
        deadline = None if wait_timeout is None else time.monotonic() + wait_timeout
        while True:
            try:
                return self._pull_once(slug, force, today)
            except PullInProgress:
                if not wait:
                    raise
            st = self._await_release(slug, poll_interval, deadline)
            if st is not None and st.state == "done":
                return self.snapshot(slug, "latest")
            # failed / stale / gone -> loop and take the lease ourselves

    def _pull_once(self, slug, force, today):
        src = self.source(slug)
        day = today or date.today().isoformat()
        # Same-day short-circuit: today's snapshot is already recorded, so a
        # repeat pull (e.g. a rerun of a daily job) is a no-op -- no lease,
        # no fetch. A concurrent pull that hasn't recorded yet is not caught
        # here; it surfaces as PullInProgress via the lease below.
        if not force:
            row = self.cat.get_snapshot(slug, day)
            if row:
                return Snapshot.from_row(row)
        # A pull downloads; hold no lease while waiting out a metered link, so a
        # multi-hour wait never looks like a stale holder to a coalescing peer.
        net.wait_for_unmetered()
        if not self.cat.acquire_lease(slug, "pull", day, _holder(), self.lease_ttl, "fetching"):
            raise PullInProgress(self.pull_status(slug))
        try:
            return self._pull_locked(slug, src, day, force)
        except PullInProgress:
            raise
        except Exception as e:
            self.cat.record_job("pull", slug, day, "failed", detail=str(e))
            self.cat.set_lease_state(slug, "failed", detail=str(e))
            raise

    def _pull_locked(self, slug, src, day, force):
        latest = self.cat.latest_snapshot(slug)
        prev = None
        if latest and latest["on_disk"]:
            prev = {"last_modified": latest["src_last_modified"],
                    "content_length": latest["src_content_length"],
                    "path": config.snapshot_path(self.root, slug, latest["date"])}

        path, count, headers = fetch(src, self.root, prev=prev, force=force, today=day)
        self.cat.set_lease_state(slug, "writing")
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
        self.cat.set_lease_state(slug, "done")
        return self.snapshot(slug, day)

    def thaw(self, slug, date, ttl_hours=24, *,
             wait=False, poll_interval=2.0, wait_timeout=None):
        """Copy a cold day back to hot. With ``wait=True``, if another caller
        holds the slug, block until it releases, then thaw ourselves; ``wait=False``
        raises ``PullInProgress``."""
        deadline = None if wait_timeout is None else time.monotonic() + wait_timeout
        while True:
            try:
                return self._thaw_once(slug, date, ttl_hours)
            except PullInProgress:
                if not wait:
                    raise
            self._await_release(slug, poll_interval, deadline)
            # lease released -> loop and take it ourselves to thaw our date

    def _thaw_once(self, slug, date, ttl_hours):
        if not self.cat.acquire_lease(slug, "thaw", date, _holder(), self.lease_ttl, "thawing"):
            raise PullInProgress(self.pull_status(slug))
        try:
            snap = self._thaw_locked(slug, date, ttl_hours)
            self.cat.set_lease_state(slug, "done")
            return snap
        except PullInProgress:
            raise
        except Exception as e:
            self.cat.set_lease_state(slug, "failed", detail=str(e))
            raise

    def _thaw_locked(self, slug, date, ttl_hours):
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
            slug, d = row["slug"], row["date"]
            if not self.cat.acquire_lease(slug, "sweep", d, _holder(), self.lease_ttl, "sweeping"):
                print(f"  [archive] {slug} is being written, skipping this cycle")
                continue
            try:
                p = config.snapshot_path(self.root, slug, d)
                if not os.path.isfile(p):
                    self.cat.set_snapshot(slug, d, on_disk=0)
                    continue
                if archive.backup(self.root, p, slug, d):
                    os.remove(p)
                    self.cat.set_snapshot(slug, d, on_disk=0, archived=1)
                    self.cat.record_job("sweep", slug, d, "done")
                    swept.append(self.snapshot(slug, d))
                else:
                    print(f"  [archive] backup failed for {slug} {d}, kept on disk")
            finally:
                self.cat.set_lease_state(slug, "done")
        return swept

    def recool_expired(self, now=None):
        now_iso = now or _now()
        recooled = []
        for row in self.cat.due_for_recool(now_iso):
            slug, d = row["slug"], row["date"]
            if not self.cat.acquire_lease(slug, "recool", d, _holder(), self.lease_ttl, "recooling"):
                continue
            try:
                p = config.snapshot_path(self.root, slug, d)
                if os.path.isfile(p):
                    os.remove(p)
                self.cat.set_snapshot(slug, d, on_disk=0, restored_until=None)
                recooled.append(self.snapshot(slug, d))
            finally:
                self.cat.set_lease_state(slug, "done")
        return recooled

    def stats(self):
        return self.cat.stats()
