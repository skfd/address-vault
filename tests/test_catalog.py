from addressvault import config
from addressvault.catalog import Catalog
from addressvault.sources import Source


def _cat(tmp_path):
    return Catalog(config.db_path(str(tmp_path)))


def test_source_roundtrip(tmp_path):
    cat = _cat(tmp_path)
    src = Source(slug="t", provider="T", data_url="http://x", access="static",
                 format="geojson", fields={"number": "NUM"})
    cat.upsert_source(src)
    row = cat.get_source("t")
    assert row["provider"] == "T"
    assert row["fields_json"] == '{"number": "NUM"}'
    assert len(cat.list_sources()) == 1


def test_an_existing_vault_gains_columns_added_later(tmp_path):
    """``CREATE TABLE IF NOT EXISTS`` leaves a built table alone, so opening an
    older vault has to add the columns it predates -- with its rows intact and
    NULL where it never had a value."""
    import sqlite3

    path = config.db_path(str(tmp_path))
    old = sqlite3.connect(path)  # a vault built before `sketch` existed
    old.executescript("""
        CREATE TABLE snapshots(
          slug TEXT, date TEXT, sha256 TEXT, features INTEGER, bytes INTEGER,
          src_last_modified TEXT, src_content_length INTEGER,
          on_disk INTEGER, archived INTEGER, restored_until TEXT,
          unchanged_since TEXT, fetched_at TEXT, PRIMARY KEY(slug, date));
        INSERT INTO snapshots (slug, date, features) VALUES ('t','2026-01-01',7);
    """)
    old.commit()
    old.close()

    cat = Catalog(path)
    cols = {r["name"] for r in cat.conn.execute("PRAGMA table_info(snapshots)")}
    assert "sketch" in cols
    row = cat.get_snapshot("t", "2026-01-01")
    assert row["features"] == 7 and row["sketch"] is None

    Catalog(path)  # opening again must not try to add it twice


def test_snapshot_query_and_flags(tmp_path):
    cat = _cat(tmp_path)
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        cat.upsert_snapshot(slug="t", date=day, sha256="a" + day, features=1, bytes=10,
                            src_last_modified=None, src_content_length=None,
                            on_disk=1, archived=0, restored_until=None,
                            unchanged_since=None, fetched_at="now")
    assert cat.latest_snapshot("t")["date"] == "2026-01-03"
    assert len(cat.list_snapshots("t", frm="2026-01-02")) == 2
    assert len(cat.list_snapshots("t", to="2026-01-01")) == 1

    cat.set_snapshot("t", "2026-01-01", on_disk=0, archived=1)
    assert cat.get_snapshot("t", "2026-01-01")["on_disk"] == 0
    assert len(cat.list_snapshots("t", on_disk=True)) == 2


def test_due_for_sweep_excludes_unchanged_and_thawed(tmp_path):
    cat = _cat(tmp_path)
    base = dict(features=1, bytes=10, src_last_modified=None, src_content_length=None,
                fetched_at="now")
    cat.upsert_snapshot(slug="t", date="2026-01-01", sha256="a", on_disk=1, archived=0,
                        restored_until=None, unchanged_since=None, **base)         # eligible
    cat.upsert_snapshot(slug="t", date="2026-01-02", sha256="a", on_disk=0, archived=0,
                        restored_until=None, unchanged_since="2026-01-01", **base)  # dedup -> skip
    cat.upsert_snapshot(slug="t", date="2026-01-03", sha256="b", on_disk=1, archived=1,
                        restored_until="2030-01-01T00:00:00+00:00",
                        unchanged_since=None, **base)                              # thawed -> skip
    due = cat.due_for_sweep("2026-12-31")
    assert [r["date"] for r in due] == ["2026-01-01"]


def test_snapshot_by_sha_returns_earliest(tmp_path):
    cat = _cat(tmp_path)
    base = dict(features=1, bytes=10, src_last_modified=None, src_content_length=None,
                on_disk=1, archived=0, restored_until=None, unchanged_since=None,
                fetched_at="now")
    cat.upsert_snapshot(slug="t", date="2026-01-05", sha256="dup", **base)
    cat.upsert_snapshot(slug="t", date="2026-01-02", sha256="dup", **base)
    assert cat.snapshot_by_sha("t", "dup", exclude_date="2026-01-09")["date"] == "2026-01-02"


def test_backfill_reduces_on_disk_days_and_dedup_days_inherit(vault, http_dir):
    from conftest import write_geojson
    from addressvault.sources import Source

    base, d = http_dir
    write_geojson(d, "addr.geojson", 50)
    vault.add_source(Source(slug="t", provider="T", data_url=f"{base}/addr.geojson",
                            access="static", format="geojson"))
    vault.pull("t", today="2026-01-01")
    vault.pull("t", today="2026-01-02")  # same bytes -> a dedup day

    # Blank the columns to stand in for days pulled before they existed.
    vault.cat.conn.execute("UPDATE snapshots SET sketch=NULL, "
                           "distinct_addresses=NULL, field_names=NULL")
    vault.cat.conn.commit()

    filled = vault.backfill()
    assert ("t", "2026-01-01") in filled  # the canonical day was re-read
    assert ("t", "2026-01-02") in filled  # the dedup day inherited it

    canon = vault.cat.get_snapshot("t", "2026-01-01")
    dedup = vault.cat.get_snapshot("t", "2026-01-02")
    assert canon["distinct_addresses"] == 50
    assert canon["field_names"] == "id"
    for col in ("sketch", "distinct_addresses", "field_names"):
        assert dedup[col] == canon[col], col
    assert vault.backfill() == []  # nothing left to do; safe to re-run
