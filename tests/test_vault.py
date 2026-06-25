import os
import shutil

import pytest

from addressvault import archive, config
from addressvault.sources import Source
from addressvault.vault import Archived

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
