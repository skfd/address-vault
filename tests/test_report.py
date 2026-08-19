import json
import os

import pytest

from addressvault import config, report

from conftest import write_geojson


def _add(vault, base, name="addr.geojson", fields=None):
    from addressvault.sources import Source
    vault.add_source(Source(slug="t", provider="T", data_url=f"{base}/{name}",
                            access="static", format="geojson",
                            fields=fields or {}))


ADDR_FIELDS = {"number": "NUM", "street": "ST"}


def _write_addresses(d, n, street="MAIN STREET", name="addr.geojson", **extra):
    """An n-address layer whose size depends only on the *length* of what it
    says, so a same-length rewrite is byte-for-byte identical."""
    (d / name).write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "properties": {"NUM": f"{i:05d}", "ST": street, **extra},
         "geometry": {"type": "Point", "coordinates": [float(i), 43.0]}}
        for i in range(n)]}))


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
    # Day one already carries the fields day two rewrites, so what moves is the
    # values and only the values -- a source that *added* a field would be a
    # schema change, which is a different finding and is tested separately.
    (d / "addr.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "features": [
            {"type": "Feature",
             "properties": {"id": i, "street": "AVENUE", "unit": "1"},
             "geometry": {"type": "Point", "coordinates": [float(i), 0.0]}}
            for i in range(200)]}))
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
    # Distinct shas throughout: identical days dedup and short-circuit, and it is
    # the feature counts these two cases turn on.
    empty = {"date": "2026-06-26", "sha256": "e0", "features": 0, "bytes": 0,
             "sketch": None}
    why, blind = report._spike(empty, {"date": "2026-06-28", "sha256": "a1",
                                       "features": 525451, "bytes": 589258096,
                                       "sketch": None})
    assert why.startswith("525,451 features, up from an empty snapshot")
    assert not blind  # a call this loud does not need the sketch to be sure
    # ...but a run of empty days is one flat line, not a spike every morning.
    assert report._spike(empty, {"date": "2026-06-27", "sha256": "e1",
                                 "features": 0, "bytes": 0,
                                 "sketch": None}) == (None, False)


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


def test_a_rewrite_that_moves_neither_count_nor_bytes_is_flagged(vault, http_dir):
    """The case both scalar tests are blind to, end to end.

    Every street renamed to an equal-length name: the city has the same number
    of addresses, in a file of exactly the same size, and none of them are the
    addresses it had yesterday.
    """
    from datetime import date

    base, d = http_dir
    _write_addresses(d, 400, street="MAIN STREET")
    _add(vault, base, fields=ADDR_FIELDS)
    vault.pull("t", today="2026-01-01")
    _write_addresses(d, 400, street="OAKS AVENUE")
    # Forced because a byte-neutral rewrite republished within the same second
    # is invisible to the static path's HEAD cache -- same Content-Length, same
    # Last-Modified. Real pulls are a day apart; only the test is that quick.
    vault.pull("t", today="2026-01-02", force=True)

    a, b = vault.snapshot("t", "2026-01-01"), vault.snapshot("t", "2026-01-02")
    assert (a.features, a.bytes) == (b.features, b.bytes)  # scalars see nothing
    assert a.sha256 != b.sha256                            # yet it is a new file

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    why = data["spikes"][("t", "2026-01-02")]
    assert "100.0% of addresses rewritten" in why
    assert report._cell(data, "t", "2026-01-02")[0] == report.SPIKED


def test_republished_metadata_alone_is_not_flagged(vault, http_dir):
    """A source that regenerates per-row ids and timestamps daily without
    touching an address (brant does exactly this) must stay quiet, or the flag
    is noise every morning for the cities that need it most."""
    from datetime import date

    base, d = http_dir
    _write_addresses(d, 400, GLOBALID="aaaaaaaa", CREATED="1786170236828")
    _add(vault, base, fields=ADDR_FIELDS)
    vault.pull("t", today="2026-01-01")
    _write_addresses(d, 400, GLOBALID="zzzzzzzz", CREATED="1786256402135")
    vault.pull("t", today="2026-01-02", force=True)  # see the note above

    a, b = vault.snapshot("t", "2026-01-01"), vault.snapshot("t", "2026-01-02")
    assert a.sha256 != b.sha256  # a genuinely different file was published
    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    assert data["spikes"] == {}
    assert report._cell(data, "t", "2026-01-02")[0] == report.NEW


def test_a_dedup_day_carries_the_sketch_forward(vault, http_dir):
    """A dedup day stores no sketch of its own, so it has to inherit one -- or
    the day after it has no baseline and the next real rewrite goes unseen."""
    from datetime import date

    base, d = http_dir
    _write_addresses(d, 400, street="MAIN STREET")
    _add(vault, base, fields=ADDR_FIELDS)
    vault.pull("t", today="2026-01-01")
    vault.pull("t", today="2026-01-02")          # identical -> dedup pointer
    _write_addresses(d, 400, street="OAKS AVENUE")
    vault.pull("t", today="2026-01-03", force=True)  # see the note above

    row = vault.cat.get_snapshot("t", "2026-01-02")
    assert row["unchanged_since"] == "2026-01-01" and row["sketch"]
    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 3))
    assert "100.0% of addresses rewritten" in data["spikes"][("t", "2026-01-03")]


def test_a_day_pulled_before_sketching_is_not_read_as_quiet(vault, http_dir):
    """Sketches only exist going forward. A missing one means "cannot say", and
    must not be mistaken for "nothing moved" -- so the day is reported as
    unmeasured, on the cell and in the section, rather than passing as clean."""
    from datetime import date

    base, d = http_dir
    _write_addresses(d, 400, street="MAIN STREET")
    _add(vault, base, fields=ADDR_FIELDS)
    vault.pull("t", today="2026-01-01")
    vault.cat.set_snapshot("t", "2026-01-01", sketch=None)  # as an old row is
    _write_addresses(d, 400, street="OAKS AVENUE")
    vault.pull("t", today="2026-01-02", force=True)  # see the note above

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    assert data["spikes"] == {}  # unseeable, not silently declared fine
    assert data["unmeasured"] == {("t", "2026-01-02")}
    assert "rewrites unchecked" in report._cell(data, "t", "2026-01-02")[1]
    assert "Rewrites unchecked on 1 changed city-day (" in report._spikes(data)


def test_an_identical_day_is_not_reported_unmeasured(vault, http_dir):
    """A dedup day reaching back past sketching has no sketch to compare, but it
    does not need one: the file is byte for byte the one before it. Reporting it
    unmeasured would bury the days that genuinely were not checked."""
    from datetime import date

    base, d = http_dir
    _write_addresses(d, 400, street="MAIN STREET")
    _add(vault, base, fields=ADDR_FIELDS)
    vault.pull("t", today="2026-01-01")
    vault.pull("t", today="2026-01-02")  # identical -> dedup pointer
    for day in ("2026-01-01", "2026-01-02"):
        vault.cat.set_snapshot("t", day, sketch=None)  # as old rows are

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    assert data["unmeasured"] == set()
    assert report._cell(data, "t", "2026-01-02")[0] == report.UNCHANGED
    assert "unchecked" not in report._spikes(data)


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


def _fc(feats):
    return json.dumps({"type": "FeatureCollection", "features": feats})


def _rows(n, *, street="AVENUE", extra=None):
    return [{"type": "Feature",
             "properties": {"id": i, "street": street, **(extra or {})},
             "geometry": {"type": "Point", "coordinates": [float(i), 0.0]}}
            for i in range(n)]


def test_a_replayed_batch_of_duplicate_rows_is_flagged(vault, http_dir):
    # brant 2026-08-16: the source's nightly rebuild wrote one 1,000-row batch
    # twice. The addresses were all already there, so the sketch read 0.0000%
    # churn; only the count of rows that repeat an address moved.
    from datetime import date

    base, d = http_dir
    (d / "addr.geojson").write_text(_fc(_rows(400)))
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    # 400 rows again, but 20 of them are a second copy of rows 0-19.
    (d / "addr.geojson").write_text(_fc(_rows(380) + _rows(20)))
    vault.pull("t", today="2026-01-02")

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    why = data["spikes"][("t", "2026-01-02")]
    assert "duplicate rows" in why and "0 -> 20" in why
    assert report._cell(data, "t", "2026-01-02")[0] == report.SPIKED


def test_a_source_that_always_duplicates_is_not_flagged_every_day(vault, http_dir):
    # windsor publishes 14% duplicate rows every single day. The level is the
    # source's business; only a move in it is the report's.
    from datetime import date

    base, d = http_dir
    (d / "addr.geojson").write_text(_fc(_rows(380) + _rows(20)))
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    (d / "addr.geojson").write_text(_fc(_rows(380, street="AVE") + _rows(20, street="AVE")))
    vault.pull("t", today="2026-01-02")

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    assert "duplicate rows" not in (data["spikes"].get(("t", "2026-01-02")) or "")


def test_a_field_appearing_is_named_not_called_a_rewrite(vault, http_dir):
    # guelph 2026-07-24 added STATION_RESPONSE_ORDER to all 53,796 rows and was
    # reported as 53,796 rows "rewritten in place" -- true of none of them.
    from datetime import date

    base, d = http_dir
    (d / "addr.geojson").write_text(_fc(_rows(300)))
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    (d / "addr.geojson").write_text(
        _fc(_rows(300, extra={"STATION_RESPONSE_ORDER": "1-4-5-2-3-6"})))
    vault.pull("t", today="2026-01-02")

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    why = data["spikes"][("t", "2026-01-02")]
    assert why.startswith("published fields changed: +STATION_RESPONSE_ORDER")
    assert "rewritten in place" not in why  # the size move is explained, not restated


def test_a_field_disappearing_is_named_too(vault, http_dir):
    # guelph 2026-08-04, the same event the other way round: AMAID was dropped.
    from datetime import date

    base, d = http_dir
    (d / "addr.geojson").write_text(_fc(_rows(300, extra={"AMAID": "0000000000"})))
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    (d / "addr.geojson").write_text(_fc(_rows(300)))
    vault.pull("t", today="2026-01-02")

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    assert data["spikes"][("t", "2026-01-02")].startswith(
        "published fields changed: -AMAID")


def _spiked(vault, http_dir):
    """A vault with one flagged large change: t 2026-01-02, half the city gone."""
    base, d = http_dir
    write_geojson(d, "addr.geojson", 1000)
    _add(vault, base)
    vault.pull("t", today="2026-01-01")
    write_geojson(d, "addr.geojson", 500)
    vault.pull("t", today="2026-01-02")


def test_an_answered_change_leaves_the_open_list_but_keeps_its_cell(vault, http_dir):
    from datetime import date

    _spiked(vault, http_dir)
    vault.review("t", "2026-01-02", "artifact",
                 "source replayed a batch; corrected next morning")

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    assert report._open_count(data) == 0        # nothing left to look at...
    assert ("t", "2026-01-02") in data["spikes"]  # ...but the day still moved
    assert report._cell(data, "t", "2026-01-02")[0] == report.SPIKED

    page = open(report.build(vault, days=7, today=date(2026, 1, 2)), encoding="utf-8").read()
    assert "Large changes (0)" in page
    assert "Every large change in this window has been looked at." in page
    assert "Answered (1)" in page
    assert "source replayed a batch" in page  # the note is the payoff, not the dismissal


def test_a_review_needs_a_reason_and_a_real_verdict(vault, http_dir):
    _spiked(vault, http_dir)
    with pytest.raises(ValueError, match="needs a note"):
        vault.review("t", "2026-01-02", "artifact", "   ")
    with pytest.raises(ValueError, match="verdict must be one of"):
        vault.review("t", "2026-01-02", "probably-fine", "a note")


def test_reviewing_a_day_that_is_not_flagged_is_refused(vault, http_dir):
    # Almost always a mistyped date: the verdict is only ever read back against
    # the large-change list, so it would be filed where nobody would see it.
    _spiked(vault, http_dir)
    with pytest.raises(LookupError, match="not a flagged large change"):
        vault.review("t", "2026-01-01", "artifact", "a note")


def test_a_later_reading_supersedes_an_earlier_one(vault, http_dir):
    from datetime import date

    _spiked(vault, http_dir)
    vault.review("t", "2026-01-02", "artifact", "looks like a bad load")
    vault.review("t", "2026-01-02", "real", "confirmed with the city: genuine")

    data = report._collect(vault.cat, vault.root, 7, date(2026, 1, 2))
    rv = data["reviews"][("t", "2026-01-02")]
    assert rv["state"] == "real" and "confirmed with the city" in rv["detail"]


def test_json_is_what_a_consumer_needs_to_trust_a_day(vault, http_dir):
    from datetime import date

    _spiked(vault, http_dir)
    js = report.to_json(report._collect(vault.cat, vault.root, 7, date(2026, 1, 2)))
    [chg] = js["changes"]
    assert chg["slug"] == "t" and chg["date"] == "2026-01-02"
    assert "-50.0% features" in chg["why"]
    # Unanswered is not the same as answered "real": one is an open question.
    assert chg["verdict"] is None and chg["note"] is None

    vault.review("t", "2026-01-02", "schema", "a field was dropped, no addresses moved")
    js = report.to_json(report._collect(vault.cat, vault.root, 7, date(2026, 1, 2)))
    [chg] = js["changes"]
    assert chg["verdict"] == "schema"
    assert chg["note"] == "a field was dropped, no addresses moved"
    assert chg["reviewed_at"]

    import json as _json
    _json.dumps(js)  # the whole contract has to survive a round trip
