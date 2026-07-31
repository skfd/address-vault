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


def _src():
    return Source(slug="t", provider="T", data_url="http://example.invalid/0",
                  access="arcgis", format="geojson")


def _page(*oids):
    return {"features": [
        {"type": "Feature", "properties": {"OBJECTID": o},
         "geometry": {"type": "Point", "coordinates": [float(o), 0.0]}}
        for o in oids]}


def _one_page(monkeypatch):
    """Two features, then an empty page -- the shortest complete pagination."""
    pages = iter([_page(1, 2), _page()])
    monkeypatch.setattr(arcgis, "_query", lambda s, u, p: next(pages))


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
