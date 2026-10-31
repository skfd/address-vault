"""The slims path: a CGIS geobuf export joined to its parcels dataset, with the
header's missing precision applied and MultiPoints exploded."""

import json

import pytest

pytest.importorskip("geobuf")

from addressvault.fetch import slims
from addressvault.fetch.degraded import DegradedPull
from addressvault.sources import Source

NUM = "e4034310-2b01-4cec-8c38-5b36eeb7903a"
LOC = "bca03632-fd7b-4275-bb89-deeeb8ff5b23"
LEGAL = "f048a081-7274-42a9-8331-744c0ddbc8f7"

# The viewer page: a JS object literal with unquoted top-level keys and JSON
# values, as cgis.com serves it (with other keys around the one we read).
PRELOAD = """<html><script>
      if (!x) { window.location = 'ie-old.html'; }
      window.preload = {

          client: {"id":18,"name":"Lanark County","cpal_clientcode":"LanarkCounty"},

          datasets: {"data":[
            {"id":83040,"name":"Civic Addresses","updated_at":"2026-09-30T15:24:31Z",
             "tileset_timestamp":"2026-10-31T10:29:04Z","columns":[
               {"id":"7ee3fdfb-0000-0000-0000-000000000000","name":"point_on_surface","alias":"Point On Surface","datatype":10},
               {"id":"%s","name":"street_number","alias":"Street_Number","datatype":0}]},
            {"id":85360,"name":"Assessment Parcels","columns":[
               {"id":"%s","name":"location","alias":"Address","datatype":0},
               {"id":"%s","name":"legal_text","alias":"Legal Text","datatype":0}]}
          ]},

          user: null,

          app: "cpal"
      };
</script></html>""" % (NUM, LOC, LEGAL)


def _encode(fc, precision=6):
    import geobuf
    from geobuf import geobuf_pb2
    buf = geobuf.encode(fc, precision)
    if precision == 0:  # what cgis.com serves: the header field absent, values x 1e6
        data = geobuf_pb2.Data()
        data.ParseFromString(geobuf.encode(fc, 6))
        data.precision = 0
        buf = data.SerializeToString()
    return buf


def _civic():
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"__key__": "090100001000200", NUM: "487",
                                           "__map_key__": "090100001000200"},
         "geometry": {"type": "MultiPoint", "coordinates": [[-75.879543, 44.897925]]}},
        {"type": "Feature", "properties": {"__key__": "090100001000100", NUM: "12",
                                           "__map_key__": "090100001000100"},
         "geometry": {"type": "MultiPoint",
                      "coordinates": [[-75.877953, 44.899099], [-75.877590, 44.898779]]}},
        {"type": "Feature", "properties": {"__key__": "099999999999999", NUM: "1",
                                           "__map_key__": "099999999999999"},
         "geometry": {"type": "MultiPoint", "coordinates": [[-75.9, 44.9]]}},
    ]}


def _parcels(flagged=False):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"__key__": "090100001000100", LOC: "Rideau River Rd",
                                           LEGAL: "CON B PT LOT 16", "__map_key__": "090100001000100"},
         "geometry": {"type": "Polygon", "coordinates": [[[-75.88, 44.9], [-75.87, 44.9],
                                                          [-75.87, 44.89], [-75.88, 44.9]]]}},
        {"type": "Feature", "properties": {"__key__": "090100001000200", LOC: "487 RIDEAU RIVER RD",
                                           "__map_key__": "090100001000200"},
         "geometry": {"type": "Polygon", "coordinates": [[[-75.88, 44.9], [-75.87, 44.9],
                                                          [-75.87, 44.89], [-75.88, 44.9]]]}},
        {"type": "Feature", "properties": {"__key__": "099999999999999",
                                           LOC: "flagged parcel" if flagged else None},
         "geometry": {"type": "Polygon", "coordinates": [[[-75.88, 44.9], [-75.87, 44.9],
                                                          [-75.87, 44.89], [-75.88, 44.9]]]}},
    ]}


class _Resp:
    def __init__(self, body):
        self.content = body if isinstance(body, bytes) else body.encode()

    @property
    def text(self):
        return self.content.decode()


def _serve(monkeypatch, civic_buf, parcel_buf, html=PRELOAD):
    urls = []

    def _get(session, url):
        urls.append(url)
        if url.startswith(slims.VIEWER.format(client="LanarkCounty")):
            return _Resp(html)
        if url.endswith("/83040.geobuf"):
            return _Resp(civic_buf)
        if url.endswith("/85360.geobuf"):
            return _Resp(parcel_buf)
        raise AssertionError(url)

    monkeypatch.setattr(slims, "_get", _get)
    return urls


def _src(url="https://www.cgis.com/api2/public/LanarkCounty/geometry/83040.geobuf?join=85360"):
    return Source(slug="lanark", provider="Lanark County", data_url=url,
                  access="slims", format="geojson")


def test_parse_url():
    assert slims.parse_url(_src().data_url) == ("LanarkCounty", 83040, 85360)
    assert slims.parse_url("https://www.cgis.com/api2/public/Timmins/geometry/98640.geobuf") \
        == ("Timmins", 98640, None)
    with pytest.raises(ValueError):
        slims.parse_url("https://www.cgis.com/publicmaps/?c=Timmins")
    with pytest.raises(ValueError):
        slims.parse_url(_src().data_url.replace("85360", "parcels"))


@pytest.mark.parametrize("precision", [0, 6])
def test_join_explode_and_precision(tmp_path, monkeypatch, precision):
    urls = _serve(monkeypatch, _encode(_civic(), precision), _encode(_parcels(), precision))

    path, count, headers = slims.fetch(_src(), str(tmp_path), today="2026-10-31")

    feats = json.load(open(path, encoding="utf-8"))["features"]
    assert count == len(feats) == 4  # three rolls, one of them two points
    assert path.endswith("lanark-2026-10-31.geojson")
    assert headers == {"last_modified": "2026-09-30T15:24:31Z",
                       "content_length": len(_encode(_civic(), precision))}
    # ordered by roll then point index, whatever the export order
    assert [f["properties"]["roll"] for f in feats] == [
        "090100001000100", "090100001000100", "090100001000200", "099999999999999"]
    # x 1e6 integers in the export come out as lon/lat
    assert feats[0]["geometry"] == {"type": "Point", "coordinates": [-75.877953, 44.899099]}
    assert feats[1]["geometry"]["coordinates"] == [-75.87759, 44.898779]
    # column UUIDs renamed, keys dropped, location joined by roll
    assert feats[2]["properties"] == {"location": "487 RIDEAU RIVER RD",
                                      "roll": "090100001000200", "street_number": "487"}
    assert feats[0]["properties"] == feats[1]["properties"] == {
        "location": "Rideau River Rd", "roll": "090100001000100", "street_number": "12"}
    assert feats[3]["properties"]["location"] is None  # parcel has none
    assert [u.rsplit("/", 1)[-1] for u in urls] == ["?c=LanarkCounty", "83040.geobuf", "85360.geobuf"]


def test_placeholder_location_is_null(tmp_path, monkeypatch):
    _serve(monkeypatch, _encode(_civic()), _encode(_parcels(flagged=True)))
    path, _, _ = slims.fetch(_src(), str(tmp_path), today="2026-10-31")
    feats = json.load(open(path, encoding="utf-8"))["features"]
    assert feats[3]["properties"]["location"] is None


def test_no_join_without_the_param(tmp_path, monkeypatch):
    urls = _serve(monkeypatch, _encode(_civic()), b"")
    path, count, _ = slims.fetch(
        _src("https://www.cgis.com/api2/public/LanarkCounty/geometry/83040.geobuf"),
        str(tmp_path), today="2026-10-31")
    feats = json.load(open(path, encoding="utf-8"))["features"]
    assert count == 4
    assert "location" not in feats[0]["properties"]
    assert not any(u.endswith("85360.geobuf") for u in urls)


def test_collapsed_join_is_refused(tmp_path, monkeypatch):
    empty = {"type": "FeatureCollection", "features": []}
    _serve(monkeypatch, _encode(_civic()), _encode(empty))
    with pytest.raises(DegradedPull, match="parcel join collapsed"):
        slims.fetch(_src(), str(tmp_path), today="2026-10-31")
    assert not (tmp_path / "lanark-2026-10-31.geojson").exists()


def test_dataset_gone_from_viewer_fails_loudly(tmp_path, monkeypatch):
    html = PRELOAD.replace('"id":83040', '"id":83041')
    _serve(monkeypatch, _encode(_civic()), _encode(_parcels()), html=html)
    with pytest.raises(ValueError, match=r"dataset\(s\) \[83040\] not in the LanarkCounty"):
        slims.fetch(_src(), str(tmp_path), today="2026-10-31")


def test_get_retries_5xx_but_not_4xx(monkeypatch):
    import requests
    monkeypatch.setattr(slims, "RETRY_WAITS", (0, 0, 0))

    class _S:
        def __init__(self, codes):
            self.codes = list(codes)
            self.calls = 0

        def get(self, url, **kw):
            self.calls += 1
            r = requests.Response()
            r.status_code = self.codes.pop(0)
            r.url = url
            return r

    s = _S([503, 500, 200])
    monkeypatch.setattr(slims.requests, "Session", lambda: s)  # the retry's fresh Session
    assert slims._get(s, "http://x/1.geobuf").status_code == 200
    assert s.calls == 3
    s = _S([404, 200])
    with pytest.raises(requests.HTTPError):
        slims._get(s, "http://x/1.geobuf")
    assert s.calls == 1  # withdrawn is final, not a flake


def test_preload_parses_the_js_literal():
    ds = slims.preload_datasets(PRELOAD)
    assert sorted(ds) == [83040, 85360]
    assert ds[85360]["columns"][0]["name"] == "location"
