import os

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
