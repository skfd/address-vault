import os

import pytest

from addressvault import config, report

from conftest import write_geojson


def _add(vault, base, name="addr.geojson"):
    from addressvault.sources import Source
    vault.add_source(Source(slug="t", provider="T", data_url=f"{base}/{name}",
                            access="static", format="geojson"))


def _storage(vault, days=7, today="2026-01-02"):
    from datetime import date
    dates = report._collect(vault.cat, vault.root, days, date.fromisoformat(today))
    return dates["storage"]


def test_storage_totals_real_bytes_and_growth(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")

    st = _storage(vault)
    path = config.snapshot_path(vault.root, "t", "2026-01-01")
    assert st["tiers"]["hot"] == os.path.getsize(path)
    assert st["tiers"]["catalog"] > 0  # catalog.db (+ its WAL) counted separately
    assert st["total"] == sum(st["tiers"].values())
    assert st["added"]["2026-01-01"] == os.path.getsize(path)
    assert st["added"]["2026-01-02"] == 0
    assert not st["warn"]


def test_unchanged_day_costs_no_new_bytes(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    vault.pull("t", today="2026-01-02")  # same content -> shares the file

    st = _storage(vault)
    assert st["added"]["2026-01-02"] == 0
    assert st["tiers"]["hot"] == os.path.getsize(
        config.snapshot_path(vault.root, "t", "2026-01-01"))


def test_missing_and_orphan_files_are_flagged(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")

    os.remove(config.snapshot_path(vault.root, "t", "2026-01-01"))
    with open(config.snapshot_path(vault.root, "ghost", "2026-01-01"), "w") as f:
        f.write("{}")

    warn = _storage(vault)["warn"]
    assert any("t-2026-01-01.geojson" in w and "missing" in w for w in warn)
    assert any("ghost-2026-01-01.geojson" in w and "not a hot snapshot" in w
               for w in warn)


def test_size_drift_is_flagged(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    with open(config.snapshot_path(vault.root, "t", "2026-01-01"), "a") as f:
        f.write(" ")

    assert any("catalog recorded" in w for w in _storage(vault)["warn"])


def test_a_link_that_died_mid_fetch_is_no_attempt_not_a_failure(vault, monkeypatch):
    from datetime import date

    from addressvault import net
    from addressvault.sources import Source
    # The pull holds the lease, then the link drops. No failed job is written,
    # but the lease still has to end terminal -- and the report reads failed
    # leases as the freshest error it has. A "deferred" lease keeps that day a
    # grey "no attempt" cell instead of a red one against a source nobody could
    # reach, and keeps the city off the "last pull failed" list.
    vault.add_source(Source(slug="t", provider="T",
                            data_url="http://127.0.0.1:1/missing.geojson",
                            access="static", format="geojson"))
    monkeypatch.setattr("addressvault.fetch.static.fetch",
                        lambda *a, **k: (_ for _ in ()).throw(net.Offline("link is offline")))
    with pytest.raises(net.Offline):
        vault.pull("t", today="2026-01-01")

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    assert ("t", "2026-01-01") not in data["fails"]
    assert data["current"] == []
    page = open(report.build(vault, days=7, today=date(2026, 1, 2)), encoding="utf-8").read()
    assert "No city is currently in a failed state." in page


def test_build_writes_storage_section(vault, http_dir):
    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")

    from datetime import date
    out = report.build(vault, days=7, today=date(2026, 1, 2))
    page = open(out, encoding="utf-8").read()
    assert "<h2>Storage</h2>" in page
    assert "New bytes per day" in page
    assert "Catalog vs disk" in page
