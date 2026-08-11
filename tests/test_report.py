import json
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


def test_a_refused_pull_is_not_shown_as_an_ordinary_failure(vault, monkeypatch):
    from datetime import date

    from addressvault.fetch import DegradedPull
    from addressvault.sources import Source
    # A source that answers with damaged data is a different problem from one
    # that cannot be reached: nobody has to do anything about a timeout, and
    # somebody has to decide about this.
    vault.add_source(Source(slug="t", provider="T", data_url="http://x/0",
                            access="arcgis", format="geojson"))
    monkeypatch.setattr(
        "addressvault.fetch.arcgis.fetch",
        lambda *a, **k: (_ for _ in ()).throw(
            DegradedPull("truncated pull: 20,000 features, layer reports 38,300")))
    with pytest.raises(DegradedPull):
        vault.pull("t", today="2026-01-01")

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    assert ("t", "2026-01-01") in data["refused"]
    assert ("t", "2026-01-01") not in data["fails"]   # never counted as a failure
    assert data["current"] == [] and len(data["standing"]) == 1
    assert report._cell(data, "t", "2026-01-01")[0] == report.REFUSED

    page = open(report.build(vault, days=7, today=date(2026, 1, 2)), encoding="utf-8").read()
    assert "Refused &mdash; needs a look (1)" in page or "Refused — needs a look (1)" in page
    assert "layer reports 38,300" in page
    assert "No city is currently in a failed state." in page  # the red list stays empty

    # ...and the lease must still be terminal, or the slug is wedged for good
    assert not vault.pull_status("t").active


def test_a_refusal_clears_when_the_source_recovers(vault, http_dir):
    from datetime import date

    base, d = http_dir
    write_geojson(d, "addr.geojson", 3)
    _add(vault, base)
    vault.cat.record_job("pull", "t", "2026-01-01", "refused", detail="truncated pull")
    vault.cat.set_lease_state("t", "refused", detail="truncated pull")

    vault.pull("t", today="2026-01-02")
    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    assert data["standing"] == [], "a good pull takes the city off the needs-a-look list"
    # the day it happened keeps its amber cell; the log is history, not a to-do
    assert report._cell(data, "t", "2026-01-01")[0] == report.REFUSED


def test_a_large_swing_is_flagged_but_still_recorded(vault, http_dir):
    from datetime import date

    base, d = http_dir
    write_geojson(d, "addr.geojson", 1000)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    write_geojson(d, "addr.geojson", 500)  # half the city disappears overnight
    vault.pull("t", today="2026-01-02")

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    assert "-50.0% features" in data["spikes"][("t", "2026-01-02")]
    assert report._cell(data, "t", "2026-01-02")[0] == report.SPIKED
    # The whole point of not making this a refusal: the day is recorded and a
    # consumer can still read it. Flagging is the operator's cue, not a gate.
    assert vault.snapshot("t", "2026-01-02").features == 500
    assert os.path.isfile(vault.path("t", "2026-01-02"))


def test_ordinary_daily_growth_is_not_flagged(vault, http_dir):
    from datetime import date

    base, d = http_dir
    write_geojson(d, "addr.geojson", 1000)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    write_geojson(d, "addr.geojson", 1005)  # a normal day's new addresses
    vault.pull("t", today="2026-01-02")

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    assert data["spikes"] == {}
    assert report._cell(data, "t", "2026-01-02")[0] == report.NEW


def test_rows_rewritten_in_place_are_flagged(vault, http_dir):
    from datetime import date

    base, d = http_dir
    write_geojson(d, "addr.geojson", 200)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    # The same 200 addresses, every one of them re-tagged. The feature count
    # says nothing happened; the byte count says the file was rewritten. This is
    # the shape a count-only check misses entirely.
    (d / "addr.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "features": [
            {"type": "Feature",
             "properties": {"id": i, "street": "RENAMED AVENUE", "unit": "100"},
             "geometry": {"type": "Point", "coordinates": [float(i), 0.0]}}
            for i in range(200)]}))
    vault.pull("t", today="2026-01-02")

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    why = data["spikes"][("t", "2026-01-02")]
    assert "rows rewritten in place" in why and "+0.0% features" in why
    assert report._cell(data, "t", "2026-01-02")[0] == report.SPIKED


def test_a_dedup_day_is_never_a_spike(vault, http_dir):
    from datetime import date

    base, d = http_dir
    write_geojson(d, "addr.geojson", 1000)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    vault.pull("t", today="2026-01-02")  # identical content -> dedup pointer

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    assert data["spikes"] == {}
    assert report._cell(data, "t", "2026-01-02")[0] == report.UNCHANGED


def test_the_baseline_may_sit_outside_the_window(vault, http_dir):
    from datetime import date

    base, d = http_dir
    write_geojson(d, "addr.geojson", 1000)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    write_geojson(d, "addr.geojson", 500)
    vault.pull("t", today="2026-01-10")

    # A 3-day window starts at 2026-01-08, so the only day it can measure
    # 2026-01-10 against is one the report does not itself show.
    data = report._collect(vault.cat, vault.root, 3, date(2026, 1, 10))
    assert "2026-01-01" not in data["dates"]
    assert "-50.0% features" in data["spikes"][("t", "2026-01-10")]


def test_recovery_from_an_empty_snapshot_is_a_spike():
    # Empty pulls are refused now, but days recorded before that guard existed
    # are still in the catalog (every city, 2026-06-25/26). Coming back from one
    # is a mass change with no baseline to be a percentage of.
    empty = {"date": "2026-06-26", "features": 0, "bytes": 0}
    assert report._spike(empty, {"date": "2026-06-28", "features": 525451,
                                 "bytes": 589258096}) \
        .startswith("525,451 features, up from an empty snapshot")
    # ...but a run of empty days is one flat line, not a spike every morning.
    assert report._spike(empty, {"date": "2026-06-27", "features": 0,
                                 "bytes": 0}) is None


def test_build_writes_large_changes_section(vault, http_dir):
    from datetime import date

    base, d = http_dir
    write_geojson(d, "addr.geojson", 1000)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    page = open(report.build(vault, days=7, today=date(2026, 1, 2)),
                encoding="utf-8").read()
    assert "Large changes (0)" in page
    assert "No city&#x27;s content moved" in page or "No city's content moved" in page

    write_geojson(d, "addr.geojson", 500)
    vault.pull("t", today="2026-01-02")
    page = open(report.build(vault, days=7, today=date(2026, 1, 2)),
                encoding="utf-8").read()
    assert "Large changes (1)" in page
    assert "-50.0% features" in page
    # a large change is not a failure, and must not be reported as one
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
