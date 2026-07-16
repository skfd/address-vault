import os
import shutil
import threading

import pytest

from addressvault import archive, config
from addressvault.sources import Source
from addressvault.vault import Archived, PullInProgress, Vault

from conftest import write_geojson

HAS_RESTIC = shutil.which("restic") is not None


def _add(vault, base, name="addr.geojson"):
    vault.add_source(Source(slug="t", provider="T", data_url=f"{base}/{name}",
                            access="static", format="geojson"))


def test_pull_static_lands_hot(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    snap = vault.pull("t", today="2026-01-01")
    assert snap.tier == "hot"
    assert snap.features == 3
    assert os.path.isfile(vault.path("t", "latest"))


def test_unchanged_content_dedups(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    snap2 = vault.pull("t", today="2026-01-02")

    assert snap2.unchanged_since == "2026-01-01"
    assert snap2.tier == "cold"  # no hot copy of its own
    # no duplicate physical file for the unchanged day
    assert not os.path.isfile(config.snapshot_path(vault.root, "t", "2026-01-02"))
    # reading the unchanged day resolves to the canonical file
    assert vault.path("t", "2026-01-02").endswith("t-2026-01-01.geojson")


def test_changed_content_makes_new_snapshot(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    write_geojson(d, "addr.geojson", 5)  # remote changes
    snap2 = vault.pull("t", today="2026-01-02")

    assert snap2.unchanged_since is None
    assert snap2.features == 5
    assert snap2.tier == "hot"


def test_same_day_repeat_pull_is_a_noop(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    write_geojson(d, "addr.geojson", 5)  # remote changes mid-day
    snap = vault.pull("t", today="2026-01-01")  # rerun of the same day
    assert snap.features == 3  # short-circuit: no second fetch
    snap = vault.pull("t", today="2026-01-01", force=True)
    assert snap.features == 5  # force still refetches


def test_snapshots_listing_and_tier_filter(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 2)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    vault.pull("t", today="2026-01-02")  # unchanged -> cold
    hot = vault.snapshots("t", tier="hot")
    cold = vault.snapshots("t", tier="cold")
    assert [s.date for s in hot] == ["2026-01-01"]
    assert [s.date for s in cold] == ["2026-01-02"]


def test_pull_records_terminal_lease_status(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    st = vault.pull_status("t")
    assert st is not None
    assert st.kind == "pull" and st.state == "done"
    assert not st.active


def test_concurrent_pull_is_reported_not_restarted(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    # Simulate another live consumer holding the slug's lease mid-fetch.
    assert vault.cat.acquire_lease("t", "pull", "2026-01-01", "other:1",
                                   vault.lease_ttl, "fetching")
    with pytest.raises(PullInProgress) as ei:
        vault.pull("t", today="2026-01-01")
    st = ei.value.status
    assert st.state == "fetching" and st.holder == "other:1" and st.active


def test_stale_lease_is_reclaimed(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    # A dead holder's lease that has gone silent past the TTL must not wedge the
    # city: backdate its last state change well beyond lease_ttl.
    assert vault.cat.acquire_lease("t", "pull", "2026-01-01", "dead:1",
                                   vault.lease_ttl, "fetching")
    vault.cat.conn.execute(
        "UPDATE leases SET updated_at='2000-01-01T00:00:00+00:00' WHERE slug='t'")
    snap = vault.pull("t", today="2026-01-01")
    assert snap.features == 3
    assert vault.pull_status("t").state == "done"


def test_failed_pull_marks_lease_failed(vault):
    # Unreachable source -> fetch raises -> lease ends "failed" with detail, and
    # the slug is not left wedged (a later valid pull can reclaim it).
    vault.add_source(Source(slug="t", provider="T",
                            data_url="http://127.0.0.1:1/missing.geojson",
                            access="static", format="geojson"))
    with pytest.raises(Exception):
        vault.pull("t", today="2026-01-01")
    st = vault.pull_status("t")
    assert st.state == "failed" and st.detail
    # The failure is also durably logged in jobs (the lease row gets
    # overwritten by the next attempt; the job log is the history).
    job = vault.cat.conn.execute(
        "SELECT * FROM jobs WHERE kind='pull' AND slug='t' AND state='failed'"
    ).fetchone()
    assert job is not None and job["detail"]


def test_wait_coalesces_onto_holder_result(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)  # a real fetch would record 3 features
    _add(vault, base)
    # Another live holder is mid-fetch; it will finish shortly with a distinctive
    # snapshot (999 features) that a real fetch here would never produce.
    assert vault.cat.acquire_lease("t", "pull", "2026-01-01", "other:1",
                                   vault.lease_ttl, "fetching")

    def holder():
        import time as _t
        _t.sleep(0.2)
        v2 = Vault(dir=vault.root)  # separate connection, like another process
        v2.cat.upsert_snapshot(
            slug="t", date="2026-01-01", sha256="deadbeef", features=999, bytes=10,
            src_last_modified=None, src_content_length=None,
            on_disk=1, archived=0, restored_until=None,
            unchanged_since=None, fetched_at="2026-01-01T00:00:00+00:00")
        v2.cat.set_lease_state("t", "done")

    t = threading.Thread(target=holder); t.start()
    snap = vault.pull("t", today="2026-01-01", wait=True, poll_interval=0.02)
    t.join()
    # Coalesced onto the holder's write; did NOT run a second fetch (would be 3).
    assert snap.features == 999


def test_wait_takes_over_failed_holder(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    # A prior holder failed and released a terminal lease -> we must pull ourselves.
    assert vault.cat.acquire_lease("t", "pull", "2026-01-01", "other:1",
                                   vault.lease_ttl, "fetching")
    vault.cat.set_lease_state("t", "failed", detail="boom")
    snap = vault.pull("t", today="2026-01-01", wait=True, poll_interval=0.02)
    assert snap.features == 3
    assert vault.pull_status("t").state == "done"


def test_wait_times_out_while_lease_held(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    # A fresh lease that never releases -> wait must give up, not hang.
    assert vault.cat.acquire_lease("t", "pull", "2026-01-01", "other:1",
                                   vault.lease_ttl, "fetching")
    with pytest.raises(TimeoutError):
        vault.pull("t", today="2026-01-01", wait=True,
                   poll_interval=0.02, wait_timeout=0.2)


@pytest.mark.skipif(not HAS_RESTIC, reason="restic not installed")
def test_archive_lifecycle_sweep_thaw_recool(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 4)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")

    # Sweep: hot -> cold (file removed, archived flag set).
    swept = vault.sweep(keep_days=0, today="2026-01-02")
    assert [s.date for s in swept] == ["2026-01-01"]
    s = vault.snapshot("t", "2026-01-01")
    assert s.tier == "cold" and s.archived and not s.on_disk
    assert not os.path.isfile(config.snapshot_path(vault.root, "t", "2026-01-01"))
    with pytest.raises(Archived):
        vault.path("t", "2026-01-01")

    # Thaw: copies back (still archived -> both on disk and archived).
    vault.thaw("t", "2026-01-01", ttl_hours=1)
    s2 = vault.snapshot("t", "2026-01-01")
    assert s2.on_disk and s2.archived
    assert os.path.isfile(vault.path("t", "2026-01-01"))
    assert "2026-01-01" in archive.archived_dates(vault.root, "t")  # archive copy intact

    # Re-cool: TTL elapsed -> drop the hot copy only, archive untouched.
    vault.cat.set_snapshot("t", "2026-01-01", restored_until="2000-01-01T00:00:00+00:00")
    recooled = vault.recool_expired()
    assert [s.date for s in recooled] == ["2026-01-01"]
    assert not os.path.isfile(config.snapshot_path(vault.root, "t", "2026-01-01"))
    s3 = vault.snapshot("t", "2026-01-01")
    assert s3.archived and not s3.on_disk
