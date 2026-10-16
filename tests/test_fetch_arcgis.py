"""The arcgis paging path: the layer-metadata call is the fetch's first request
and was the one request outside the retry budget, so any blip on it cost the
whole city."""

import json

import pytest
import requests

from addressvault.fetch import arcgis
from addressvault.sources import Source

META = {"maxRecordCount": 2, "supportedQueryFormats": "JSON,geoJSON",
        "objectIdField": "OBJECTID"}


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(arcgis, "RETRY_WAIT", 0)
    monkeypatch.setattr(arcgis, "FLAKE_WAITS", (0, 0, 0, 0))


def _src():
    return Source(slug="t", provider="T", data_url="http://example.invalid/0",
                  access="arcgis", format="geojson")


def _page(*oids):
    return {"features": [
        {"type": "Feature", "properties": {"OBJECTID": o},
         "geometry": {"type": "Point", "coordinates": [float(o), 0.0]}}
        for o in oids]}


def _one_page(monkeypatch, expected=2):
    """Two features, then an empty page -- the shortest complete pagination."""
    pages = iter([_page(1, 2), _page()])
    monkeypatch.setattr(arcgis, "_query", lambda s, u, p: next(pages))
    monkeypatch.setattr(arcgis, "_count", lambda s, u: expected)


def test_metadata_blip_is_retried_not_fatal(tmp_path, monkeypatch):
    calls = []

    def _meta(session, url):
        calls.append(session)
        if len(calls) == 1:
            raise requests.ConnectionError("getaddrinfo failed")
        return META

    monkeypatch.setattr(arcgis, "_layer_meta", _meta)
    _one_page(monkeypatch)

    path, count, _ = arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")

    assert count == 2
    assert json.loads(open(path).read())["features"][0]["properties"]["OBJECTID"] == 1
    assert calls[0] is not calls[1]  # a blocked connection won't recover: new Session


def test_metadata_gives_up_after_the_retry_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(arcgis, "RETRIES", 2)
    calls = []

    def _meta(session, url):
        calls.append(1)
        raise requests.ConnectionError("getaddrinfo failed")

    monkeypatch.setattr(arcgis, "_layer_meta", _meta)
    with pytest.raises(requests.ConnectionError):
        arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert len(calls) == 3  # the initial attempt plus RETRIES


def test_metadata_and_pages_share_one_budget(tmp_path, monkeypatch):
    # A city that limps through metadata has already spent part of its budget;
    # the pages get what is left, not a fresh allowance.
    monkeypatch.setattr(arcgis, "RETRIES", 1)
    metas = iter([requests.ConnectionError("blip"), META])

    def _meta(session, url):
        got = next(metas)
        if isinstance(got, Exception):
            raise got
        return got

    def _query(session, url, params):
        raise requests.Timeout("stalled")

    monkeypatch.setattr(arcgis, "_layer_meta", _meta)
    monkeypatch.setattr(arcgis, "_query", _query)
    with pytest.raises(requests.Timeout):  # budget already spent on metadata
        arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")


def test_an_error_body_under_http_200_is_not_a_layer(tmp_path, monkeypatch):
    # ArcGIS reports server-side failures as {"error": ...} with a 200, which
    # used to read as a layer with no maxRecordCount and no features.
    class _Resp:
        @staticmethod
        def raise_for_status():
            pass

        @staticmethod
        def json():
            return {"error": {"code": 500, "message": "Unable to complete operation"}}

    monkeypatch.setattr(arcgis, "TIMEOUT", 0)
    monkeypatch.setattr(requests.Session, "get", lambda self, u, **k: _Resp())
    with pytest.raises(ValueError, match="Unable to complete operation"):
        arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert list(tmp_path.iterdir()) == []  # nothing written over a real city


def test_an_empty_first_page_fails_instead_of_writing_an_empty_snapshot(
        tmp_path, monkeypatch):
    monkeypatch.setattr(arcgis, "_layer_meta", lambda s, u: META)
    monkeypatch.setattr(arcgis, "_count", lambda s, u: None)
    monkeypatch.setattr(arcgis, "_query", lambda s, u, p: _page())
    with pytest.raises(ValueError, match="first page returned no features"):
        arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert list(tmp_path.iterdir()) == []


def test_a_pull_that_stops_short_of_the_layer_count_fails(tmp_path, monkeypatch):
    # kitchener 2026-07-28: a blank page arrived mid-stream, ended the paging
    # loop, and 102,000 of 131,912 addresses were recorded as the whole city.
    monkeypatch.setattr(arcgis, "_layer_meta", lambda s, u: META)
    _one_page(monkeypatch, expected=50)
    with pytest.raises(ValueError, match=r"truncated pull: 2 features, layer reports 50"):
        arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert list(tmp_path.iterdir()) == []


def test_a_layer_that_reports_no_count_is_still_pulled(tmp_path, monkeypatch):
    # Unverifiable is the guarantee we had before the probe, not a new failure.
    monkeypatch.setattr(arcgis, "_layer_meta", lambda s, u: META)
    _one_page(monkeypatch, expected=None)
    _, count, _ = arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert count == 2


def test_rows_deleted_while_paging_stay_inside_the_tolerance(tmp_path, monkeypatch):
    # The count is probed before paging starts, so a live layer can shed a few
    # rows underneath us; that is not a truncated pull.
    monkeypatch.setattr(arcgis, "_layer_meta", lambda s, u: dict(META, maxRecordCount=500))
    pages = iter([_page(*range(1, 298)), _page()])
    monkeypatch.setattr(arcgis, "_query", lambda s, u, p: next(pages))
    monkeypatch.setattr(arcgis, "_count", lambda s, u: 300)  # 1% of 300 == 3
    _, count, _ = arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert count == 297


def test_a_down_link_surfaces_as_link_unavailable_without_burning_retries(
        tmp_path, monkeypatch):
    # We hold the slug's lease inside fetch, so a dead link must abort at once
    # for the caller to record as "no attempt" -- not sit through the budget.
    from addressvault import net

    calls = []

    def _meta(session, url):
        calls.append(1)
        raise requests.ConnectionError("getaddrinfo failed")

    monkeypatch.setattr(arcgis, "_layer_meta", _meta)
    monkeypatch.setattr("addressvault.net.wait_for_link",
                        lambda **k: (_ for _ in ()).throw(net.Offline("link is offline")))
    with pytest.raises(net.Offline):
        arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert len(calls) == 1  # no retries against a link that cannot work


def test_a_layer_that_lost_its_geometry_is_refused(tmp_path, monkeypatch):
    # muskoka 2026-06-30: the count matched, every field arrived, and all 66,512
    # rows came back with a null geometry. Every check we had passed it.
    monkeypatch.setattr(arcgis, "_layer_meta", lambda s, u: META)
    monkeypatch.setattr(arcgis, "_count", lambda s, u: 2)
    flat = {"features": [{"type": "Feature", "properties": {"OBJECTID": o},
                          "geometry": None} for o in (1, 2)]}
    pages = iter([flat, _page()])
    monkeypatch.setattr(arcgis, "_query", lambda s, u, p: next(pages))
    with pytest.raises(arcgis.DegradedPull,
                       match=r"geometry-less pull: 2 of 2 features"):
        arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert list(tmp_path.iterdir()) == []  # nothing written over a located city


def test_a_few_unlocated_addresses_are_not_a_refusal(tmp_path, monkeypatch):
    # A city can record an address before anybody has put a pin in it; huron
    # carries one such row in 38,315. That is the layer, not a broken pull.
    monkeypatch.setattr(arcgis, "_layer_meta", lambda s, u: dict(META, maxRecordCount=500))
    good = _page(*range(1, 200))["features"]
    good[0]["geometry"] = None
    pages = iter([{"features": good}, _page()])
    monkeypatch.setattr(arcgis, "_query", lambda s, u, p: next(pages))
    monkeypatch.setattr(arcgis, "_count", lambda s, u: 199)
    _, count, _ = arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert count == 199  # 1/199 == 0.5%, inside the 1% tolerance


def test_a_pull_that_overshoots_the_layer_count_is_refused(tmp_path, monkeypatch):
    # Paging assumes the OID window is stable across queries; a layer that
    # renumbers underneath us would re-serve rows we already hold. Every row is
    # real and located, so only the count says anything.
    monkeypatch.setattr(arcgis, "_layer_meta", lambda s, u: dict(META, maxRecordCount=500))
    pages = iter([_page(*range(1, 301)), _page()])
    monkeypatch.setattr(arcgis, "_query", lambda s, u, p: next(pages))
    monkeypatch.setattr(arcgis, "_count", lambda s, u: 200)
    with pytest.raises(arcgis.DegradedPull,
                       match=r"overlong pull: 300 features, layer reports 200"):
        arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert list(tmp_path.iterdir()) == []


def test_rows_added_while_paging_stay_inside_the_overcount_tolerance(tmp_path, monkeypatch):
    # The count is probed before paging starts, so a live layer can gain a few
    # rows ahead of us. That is the asymmetry the wider tolerance exists for.
    monkeypatch.setattr(arcgis, "_layer_meta", lambda s, u: dict(META, maxRecordCount=500))
    pages = iter([_page(*range(1, 303)), _page()])
    monkeypatch.setattr(arcgis, "_query", lambda s, u, p: next(pages))
    monkeypatch.setattr(arcgis, "_count", lambda s, u: 300)  # 2% of 300 == 6
    _, count, _ = arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert count == 302


# ---- the short retry tier: the server answering badly, not failing to answer ----

def _http_error(status):
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status} Server Error", response=resp)


def test_a_mid_page_503_heals_and_resumes_where_it_stopped(tmp_path, monkeypatch):
    # simcoe/guelph: page 2 answers 503 (or 500), the same page answers a
    # minute later. The pull resumes past the last OID it holds, not from zero.
    monkeypatch.setattr(arcgis, "_layer_meta", lambda s, u: META)
    monkeypatch.setattr(arcgis, "_count", lambda s, u: 4)
    seen = []
    script = iter([_page(1, 2), _http_error(503), _page(3, 4), _page()])

    def _query(session, url, params):
        seen.append(params["where"])
        got = next(script)
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(arcgis, "_query", _query)
    _, count, _ = arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert count == 4
    assert seen == ["OBJECTID > -1", "OBJECTID > 2", "OBJECTID > 2", "OBJECTID > 4"]


def test_an_arcgis_error_body_under_200_heals(tmp_path, monkeypatch):
    # kingston "Error invoking service", milton "Wait time ... has expired".
    monkeypatch.setattr(arcgis, "_layer_meta", lambda s, u: META)
    monkeypatch.setattr(arcgis, "_count", lambda s, u: 2)
    script = iter([arcgis.ArcgisError(500, "Error invoking service"), _page(1, 2), _page()])

    def _query(session, url, params):
        got = next(script)
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(arcgis, "_query", _query)
    _, count, _ = arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert count == 2


def test_a_stopped_service_fails_at_once(tmp_path, monkeypatch):
    # west-parry-sound: a 500 that says the service is not started does not
    # clear in minutes; waiting it out only lengthens the run.
    calls = []

    def _meta(session, url):
        calls.append(1)
        raise arcgis.ArcgisError(500, "Service wpsgnPublic/wpsgn_opendata/MapServer not started ")

    monkeypatch.setattr(arcgis, "_layer_meta", _meta)
    with pytest.raises(ValueError, match="not started"):
        arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert len(calls) == 1


def test_a_client_error_is_not_a_flake(tmp_path, monkeypatch):
    calls = []

    def _meta(session, url):
        calls.append(1)
        raise _http_error(404)

    monkeypatch.setattr(arcgis, "_layer_meta", _meta)
    with pytest.raises(requests.HTTPError):
        arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert len(calls) == 1


def test_the_flake_budget_is_bounded_and_shared(tmp_path, monkeypatch):
    # One budget per fetch: the metadata call spends some, pages get the rest.
    monkeypatch.setattr(arcgis, "_count", lambda s, u: 2)
    metas = iter([_http_error(502), META])

    def _meta(session, url):
        got = next(metas)
        if isinstance(got, Exception):
            raise got
        return got

    calls = []

    def _query(session, url, params):
        calls.append(1)
        raise _http_error(503)

    monkeypatch.setattr(arcgis, "_layer_meta", _meta)
    monkeypatch.setattr(arcgis, "_query", _query)
    with pytest.raises(requests.HTTPError):
        arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert len(calls) == len(arcgis.FLAKE_WAITS)  # 1 spent on metadata, +1 final try
    assert list(tmp_path.iterdir()) == []


def test_flakes_do_not_spend_the_connection_budget(tmp_path, monkeypatch):
    # The long tier (15 min waits) is for a connection that will not open; a
    # server that answered badly must not use it up.
    monkeypatch.setattr(arcgis, "RETRIES", 0)
    monkeypatch.setattr(arcgis, "_layer_meta", lambda s, u: META)
    monkeypatch.setattr(arcgis, "_count", lambda s, u: 2)
    script = iter([_http_error(500), _http_error(500), _page(1, 2), _page()])

    def _query(session, url, params):
        got = next(script)
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(arcgis, "_query", _query)
    _, count, _ = arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert count == 2


def test_an_empty_first_page_gets_a_second_look(tmp_path, monkeypatch):
    # cambridge, three days: an empty first page, a full layer minutes later.
    monkeypatch.setattr(arcgis, "_layer_meta", lambda s, u: META)
    monkeypatch.setattr(arcgis, "_count", lambda s, u: 2)
    pages = iter([_page(), _page(1, 2), _page()])
    monkeypatch.setattr(arcgis, "_query", lambda s, u, p: next(pages))
    _, count, _ = arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert count == 2


def test_a_broken_read_and_an_html_body_are_flakes():
    assert arcgis.is_flake(requests.exceptions.ChunkedEncodingError("IncompleteRead"))
    assert arcgis.is_flake(requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0))
    assert not arcgis.is_flake(arcgis.ArcgisError(400, "Invalid URL"))
    assert not arcgis.is_flake(ValueError("something else"))


def test_a_count_that_keeps_flaking_falls_back_to_unverified(tmp_path, monkeypatch):
    # Before the short tier, a count probe that errored left the pull unchecked.
    # Spending the budget on it must not turn that into a failed city.
    monkeypatch.setattr(arcgis, "_layer_meta", lambda s, u: META)
    pages = iter([_page(1, 2), _page()])

    def _query(session, url, params):
        if params.get("returnCountOnly"):
            raise arcgis.ArcgisError(500, "Error performing query operation")
        return next(pages)

    monkeypatch.setattr(arcgis, "_query", _query)
    _, count, _ = arcgis.fetch(_src(), str(tmp_path), today="2026-01-01")
    assert count == 2
