"""Fetch a CGIS SLIMS Viewer dataset (the CommunityPAL "publicmaps" stack) as
GeoJSON (4326), joined to its Assessment Parcels dataset for the address string.

CGIS hosts Lanark County and Timmins (and Perth, Mississippi Mills, Smiths
Falls) on one viewer, ``https://www.cgis.com/publicmaps/?c=<client>``. Each
client's datasets are listed in the page's ``window.preload`` object, and every
dataset has a whole-file export on the public API::

    https://www.cgis.com/api2/public/<client>/geometry/<datasetId>.geobuf

The ``.geojson`` export answers 500 server-side, ``.flatgeobuf`` 500s on the
polygon datasets, so geobuf (protobuf) is the one route that serves every
dataset (probed 2026-10-31). The ``?t=<tileset stamp>`` the viewer appends is
not needed: the export answers the same bytes without it.

**The archived pull is a derived product, not a raw one.** The "Civic
Addresses" dataset carries a 15-digit roll number and the street number only;
the street name lives on the "Assessment Parcels" dataset (``location``, e.g.
"2125 SCOTCH LINE"). This fetcher is the only place that sees both, so it
joins them here, by roll, and what the vault archives is that join. Three more
transformations, all deterministic and all stated here so a reader of the
archive knows what it is:

* properties come out of the export keyed by column UUID; they are renamed to
  the column names the viewer config gives them (``street_number``,
  ``location``), and the export's ``__map_key__`` (the 15-digit roll the
  parcels join on) is emitted as ``roll``;
* a civic feature is a MultiPoint of 1..n points (one roll, several located
  addresses: 629 Lanark rolls carry 2 points, one Timmins roll 123). Each point
  becomes its own Point feature with the same properties -- the tracker models
  address points, and an identity synthesized from number + roll + geometry
  keeps the siblings apart;
* the geobuf header omits ``precision``; the geobuf spec's default is 6 but the
  proto3 schema the Python decoder ships reads 0, so coordinates would come out
  as lon/lat x 1e6. The spec default is applied.

``data_url`` grammar (no new Source field, no catalog column)::

    https://www.cgis.com/api2/public/<client>/geometry/<civicId>.geobuf?join=<parcelId>

``join`` names the Assessment Parcels dataset. Without it the pull is the civic
export alone (no ``location``).

The join is the fragile half: a parcels export that arrives short blanks
``location`` on the rows it misses. The identity the tracker synthesizes does
not use it (number + roll + geometry), so a collapsed join reads as a mass
"full/street changed" day the flag pipeline holds, not a re-key -- and the
pull refuses outright when the join rate falls under ``JOIN_FLOOR``: measured
100.0% in both cities on 2026-10-31, so anything near the floor is damage, not
data. There is no independent row count on this route; the vault's 2%
feature-count flag is the only truncation guard for the civic export itself.
"""

import json
import os
import re
import time
from datetime import date as _date
from urllib.parse import parse_qs, urlsplit

import requests

from addressvault import net
from addressvault.fetch.degraded import (
    DegradedPull, check_geometry, has_coords)

TIMEOUT = 300
# Short retry tier for a flaky answer (the export route 500s now and then on
# formats it cannot serve; a transient 5xx on one it can is worth a second try).
RETRIES = 3
RETRY_WAITS = (30, 60, 120)
_FLAKE_STATUS = {500, 502, 503, 504}
# The viewer page is served to any client, but keep the browser UA the probes
# used: the API sits behind ARR with the viewer as its only intended caller.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
VIEWER = "https://www.cgis.com/publicmaps/?c={client}"
EXPORT = "https://www.cgis.com/api2/public/{client}/geometry/{dataset}.geobuf"
# Share of civic rows that must find a parcel; see the module docstring.
JOIN_FLOOR = 0.5
# The geobuf spec default, applied when the header carries none.
DEFAULT_PRECISION = 6
# Parcel ``location`` values that are placeholders, not addresses (Timmins
# publishes 2,525 parcels as "flagged parcel"; none carried a civic point on
# 2026-10-31, but a join onto one must not read as an address).
_NULL_LOCATIONS = {"flagged parcel"}

_URL_RE = re.compile(r"^/api2/public/(?P<client>[^/]+)/geometry/(?P<dataset>\d+)\.geobuf$")


def parse_url(data_url):
    """``(client, civic dataset id, parcel dataset id or None)`` from the
    data_url grammar above. Anything else is a config error, raised here so a
    bad TOML fails at seed/pull time with the URL in the message."""
    u = urlsplit(data_url)
    m = _URL_RE.match(u.path)
    if u.netloc != "www.cgis.com" or not m:
        raise ValueError(f"slims data_url must be https://www.cgis.com/api2/public/"
                         f"<client>/geometry/<id>.geobuf[?join=<id>], got {data_url!r}")
    q = parse_qs(u.query)
    join = q.get("join", [None])[0]
    if join is not None and not join.isdigit():
        raise ValueError(f"slims data_url join must be a dataset id, got {join!r}")
    return m.group("client"), int(m.group("dataset")), (int(join) if join else None)


def _get(session, url):
    """GET ``url`` with the short retry tier; returns the Response."""
    for attempt in range(RETRIES + 1):
        try:
            r = session.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            if r.status_code in _FLAKE_STATUS and attempt < RETRIES:
                raise requests.HTTPError(f"{r.status_code} from {url}", response=r)
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout,
                requests.exceptions.ChunkedEncodingError, requests.HTTPError) as e:
            net.wait_for_link(wait=False)
            if attempt == RETRIES:
                raise
            wait = RETRY_WAITS[attempt]
            print(f"  {url.rsplit('/', 1)[-1]}: {e}; retrying in {wait}s")
            time.sleep(wait)
            session = requests.Session()


# --- viewer config -----------------------------------------------------------

def preload_datasets(html):
    """``{dataset id: dataset dict}`` from the viewer page's ``window.preload``.

    The object is a JS literal (unquoted top-level keys, JSON values), so each
    depth-1 value is decoded on its own rather than the whole thing."""
    i = html.find("window.preload")
    if i < 0:
        raise ValueError("viewer page carries no window.preload")
    s = html.find("{", i)
    depth = 0
    instr = esc = False
    keys = []
    j = s
    while j < len(html):
        c = html[j]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
        elif c == '"':
            instr = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1 and c == ":":
            m = re.search(r"([A-Za-z_]\w*)\s*$", html[max(s, j - 64):j])
            if m:
                keys.append((m.group(1), j + 1))
        j += 1
    dec = json.JSONDecoder()
    values = {}
    for key, pos in keys:
        pos += len(html[pos:pos + 64]) - len(html[pos:pos + 64].lstrip())
        values[key], _ = dec.raw_decode(html, pos)
    rows = (values.get("datasets") or {}).get("data")
    if not isinstance(rows, list):
        raise ValueError("viewer preload has no datasets.data")
    return {d["id"]: d for d in rows if isinstance(d, dict) and "id" in d}


def _columns(dataset):
    """``{column UUID: column name}`` for a preload dataset."""
    return {c["id"]: c["name"] for c in dataset.get("columns", []) if c.get("id")}


# --- geobuf ------------------------------------------------------------------

def decode_geobuf(buf):
    """A GeoJSON FeatureCollection dict from geobuf bytes, with the spec's
    default precision applied where the header omits it (see the docstring).
    Imported lazily, as static.py does pyshp: a missing optional dependency
    fails the pull of a slims city, not the dispatch of every city."""
    from geobuf import geobuf_pb2
    from geobuf.decode import Decoder

    data = geobuf_pb2.Data()
    data.ParseFromString(buf)
    dec = Decoder()
    dec.data = data
    dec.e = 10 ** (data.precision or DEFAULT_PRECISION)
    dec.dim = data.dimensions or 2
    kind = data.WhichOneof("data_type")
    if kind is None:  # an empty collection serializes to no fields at all
        return {"type": "FeatureCollection", "features": []}
    if kind != "feature_collection":
        raise ValueError(f"geobuf is a {kind}, not a feature_collection")
    return dec.decode_feature_collection(data.feature_collection)


def _points(geometry):
    """The positions of a Point/MultiPoint geometry, in export order."""
    if not geometry or not geometry.get("coordinates"):
        return []
    if geometry.get("type") == "Point":
        return [geometry["coordinates"]]
    if geometry.get("type") == "MultiPoint":
        return list(geometry["coordinates"])
    raise ValueError(f"civic export carries a {geometry.get('type')}, not points")


# --- join --------------------------------------------------------------------

def parcel_locations(parcels, columns):
    """``{roll: location}`` from the parcels export. Deterministic under
    duplicate rolls: the smallest non-placeholder location wins, whatever the
    export order. Placeholders and blanks are left out."""
    loc_col = next((uid for uid, name in columns.items() if name == "location"), None)
    if loc_col is None:
        raise ValueError("parcels dataset has no `location` column in the viewer config")
    out = {}
    for f in parcels.get("features", []):
        p = f.get("properties") or {}
        roll = p.get("__key__")
        loc = p.get(loc_col)
        if not roll or not isinstance(loc, str):
            continue
        loc = loc.strip()
        if not loc or loc.lower() in _NULL_LOCATIONS:
            continue
        if roll not in out or loc < out[roll]:
            out[roll] = loc
    return out


def build_features(civic, columns, locations):
    """The archived features: one Point per civic position, properties renamed
    and joined. Returns ``(features, joined, rows)`` -- ``joined`` and ``rows``
    count civic rows (before the explode), for the join-rate guard."""
    names = {uid: name for uid, name in columns.items()}
    rows = joined = 0
    out = []
    for f in civic.get("features", []):
        p = f.get("properties") or {}
        rows += 1
        roll = p.get("__map_key__") or p.get("__key__")
        props = {"roll": roll}
        for uid, val in p.items():
            if uid in ("__key__", "__map_key__"):
                continue
            props[names.get(uid, uid)] = val
        if locations is not None:
            loc = locations.get(roll)
            joined += loc is not None
            props["location"] = loc
        props = dict(sorted(props.items()))
        pts = _points(f.get("geometry"))
        if not pts:  # kept, so the geometry guard can count it
            out.append({"type": "Feature", "properties": props, "geometry": None})
        for n, pt in enumerate(pts):
            out.append({"type": "Feature", "properties": dict(props),
                        "geometry": {"type": "Point", "coordinates": [pt[0], pt[1]]},
                        "_sort": (str(roll or ""), n)})
    # Export order is not promised stable across days; the sha the vault dedups
    # on must be, so order by roll then point index.
    out.sort(key=lambda f: f.pop("_sort", ("", 0)))
    return out, joined, rows


def fetch(source, dest_dir, *, force=False, today=None):
    day = today or _date.today().isoformat()
    os.makedirs(dest_dir, exist_ok=True)
    filename = f"{source.slug}-{day}.geojson"
    filepath = os.path.join(dest_dir, filename)

    client, civic_id, parcel_id = parse_url(source.data_url)
    session = requests.Session()

    print(f"  reading viewer config for {client}")
    datasets = preload_datasets(_get(session, VIEWER.format(client=client)).text)
    missing = [i for i in (civic_id, parcel_id) if i is not None and i not in datasets]
    if missing:
        raise ValueError(f"dataset(s) {missing} not in the {client} viewer config "
                         f"({len(datasets)} listed); renamed, withdrawn or moved")
    civic_ds = datasets[civic_id]
    civic_cols = _columns(civic_ds)
    if "street_number" not in civic_cols.values():
        raise ValueError(f"dataset {civic_id} ({civic_ds.get('name')!r}) has no "
                         f"street_number column: {sorted(civic_cols.values())}")
    print(f"  {civic_ds.get('name')!r} {civic_id}: updated {civic_ds.get('updated_at')}, "
          f"tiles {civic_ds.get('tileset_timestamp')}")

    url = EXPORT.format(client=client, dataset=civic_id)
    print(f"  downloading {url}")
    r = _get(session, url)
    civic = decode_geobuf(r.content)
    civic_bytes = len(r.content)
    headers = {"last_modified": civic_ds.get("updated_at"),
               "content_length": civic_bytes}
    if not civic.get("features"):
        raise ValueError("civic export decoded to no features")

    locations = None
    if parcel_id is not None:
        url = EXPORT.format(client=client, dataset=parcel_id)
        print(f"  downloading {url} (parcels, for `location`)")
        parcels = decode_geobuf(_get(session, url).content)
        locations = parcel_locations(parcels, _columns(datasets[parcel_id]))
        print(f"  {len(locations):,} parcels with a location")

    features, joined, rows = build_features(civic, civic_cols, locations)
    if locations is not None:
        rate = joined / rows if rows else 0.0
        print(f"  joined {joined:,} of {rows:,} civic rows ({rate:.1%}); "
              f"{len(features):,} points")
        if rate < JOIN_FLOOR:
            raise DegradedPull(f"parcel join collapsed: {joined:,} of {rows:,} civic "
                               f"rows found a location ({rate:.1%}, floor "
                               f"{JOIN_FLOOR:.0%}, 100% on 2026-10-31)")
    else:
        print(f"  {rows:,} civic rows, {len(features):,} points (no parcel join)")

    lons = [f["geometry"]["coordinates"][0] for f in features if has_coords(f)]
    lats = [f["geometry"]["coordinates"][1] for f in features if has_coords(f)]
    if lons and not (all(-180 <= x <= 180 for x in lons) and all(-90 <= y <= 90 for y in lats)):
        raise ValueError("decoded coordinates are not lon/lat: the export's "
                         "precision or CRS changed")
    check_geometry(sum(1 for f in features if not has_coords(f)), len(features))

    tmp = filepath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    os.replace(tmp, filepath)  # atomic: a reader never sees a partial file
    return filepath, len(features), headers
