"""Fetch an ArcGIS REST feature/map layer via paginated /query, as GeoJSON (4326).

Lifted from address-layerist. OBJECTID-window pagination keeps each page an
indexed range scan (fast on large layers), unlike resultOffset which re-scans
from the start every page.
"""

import json
import os
import time
from datetime import date as _date

import requests

from addressvault import net
from addressvault.fetch.degraded import (  # noqa: F401  (DegradedPull re-exported)
    DegradedPull, check_geometry, has_coords)

TIMEOUT = 120
DEFAULT_PAGE = 2000
# maps.ottawa.ca (F5 GSLB) started dropping new TCP connections after ~15-35
# rapid page requests (observed 2026-07-16); a keep-alive Session stays under
# that limit. If a fetch still stalls, wait out the observed ~15 min block and
# resume from last_oid instead of failing the whole city.
RETRIES = 3
RETRY_WAIT = 900
# How far below the layer's own reported count a pull may land before we call it
# truncated. Not zero: the layer keeps being edited while we page it, so a few
# rows can legitimately disappear between the count probe and the last page.
COUNT_TOLERANCE = 0.01
# And how far above. Wider than the short side, because the two sides are not
# symmetric: rows added after the count probe legitimately land on this one,
# while the OID window already skips any that land behind us.
#
# Worth checking at all because paging assumes the OID window is stable across
# queries, and for some layers it demonstrably is not -- brant serves a view
# that renumbers OBJECTID 1..N on every pull. Within one pull the order has
# held, but nothing here verifies that, and a window that shifted underneath us
# would re-serve rows we already hold. That is invisible to every other check:
# the rows are real, the geometry is real, only there are too many of them.
OVERCOUNT_TOLERANCE = 0.02


def _json(r):
    """Parse a response, rejecting the error bodies ArcGIS serves under HTTP 200.

    raise_for_status() sees those as healthy, so left unchecked the caller reads
    a body with no maxRecordCount and no features as a tiny layer that is already
    exhausted -- and writes an empty snapshot over a real city (york,
    2026-07-31: metadata and first page both degraded, 431k addresses -> 0)."""
    r.raise_for_status()
    body = r.json()
    if isinstance(body, dict) and "error" in body:
        err = body["error"] if isinstance(body["error"], dict) else {}
        raise ValueError(f"arcgis error {err.get('code', '?')}: "
                         f"{err.get('message') or body['error']}")
    return body


def _layer_meta(session, url):
    return _json(session.get(url, params={"f": "json"}, timeout=TIMEOUT))


def _query(session, url, params):
    return _json(session.get(url + "/query", params=params, timeout=TIMEOUT))


def _count(session, url):
    """The layer's own row count, or None if it will not report one.

    This is the only independent measure of how big the pull should be: the
    paging loop below cannot tell a layer that ended from a page that came back
    blank by accident (kitchener 2026-07-28 stopped at 102,000 of 131,912, huron
    at 20,000 of 38,300 -- both exact multiples of the page size). A layer that
    refuses the query is left unchecked rather than failed; that is the older,
    weaker guarantee, not a new failure."""
    try:
        body = _query(session, url, {"where": "1=1", "returnCountOnly": "true",
                                     "f": "json"})
    except ValueError as e:  # error body under HTTP 200: query unsupported here
        print(f"  count probe rejected ({e}); pull size cannot be verified")
        return None
    n = body.get("count")
    return n if isinstance(n, int) else None


def _esri_to_geojson(esri):
    """Convert an esri-json query response (point layers) to GeoJSON features."""
    feats = []
    for f in esri.get("features", []):
        g = f.get("geometry") or {}
        x, y = g.get("x"), g.get("y")
        coords = [x, y] if x is not None and y is not None else None
        feats.append({
            "type": "Feature",
            "properties": f.get("attributes", {}),
            "geometry": {"type": "Point", "coordinates": coords} if coords else None,
        })
    return feats


def _max_oid(batch, oid_field):
    return max(f["properties"][oid_field] for f in batch)


def fetch(source, dest_dir, *, force=False, today=None):
    day = today or _date.today().isoformat()
    os.makedirs(dest_dir, exist_ok=True)
    filename = f"{source.slug}-{day}.geojson"
    filepath = os.path.join(dest_dir, filename)

    session = requests.Session()
    retries = RETRIES

    def _pause(exc, what):
        """Shared by the metadata call and by each page: wait out the block,
        then hand back a fresh Session (a blocked connection won't recover).
        Re-raises once the shared budget is spent, or at once if the link itself
        is down -- we hold the slug's lease here, so deferring belongs to the
        pre-lease gate, not to us."""
        nonlocal retries, session
        net.wait_for_link(wait=False)
        if not retries:
            raise exc
        retries -= 1
        print(f"\n  {what}; retrying in {RETRY_WAIT}s")
        time.sleep(RETRY_WAIT)
        session = requests.Session()

    # The metadata call draws on the same budget as a page: it is just as
    # droppable, and a blip on this first request used to cost the whole city.
    # The count probe rides along in the same attempt -- it is the same kind of
    # cheap pre-flight request, and it is worthless if a blip is allowed to skip
    # it, since an unverified pull is exactly what we are trying to stop.
    while True:
        try:
            meta = _layer_meta(session, source.data_url)
            expected = _count(session, source.data_url)
            break
        except (requests.ConnectionError, requests.Timeout) as e:
            _pause(e, f"{source.slug} metadata unreachable")

    page = min(meta.get("maxRecordCount") or DEFAULT_PAGE, DEFAULT_PAGE)
    can_geojson = "geoJSON" in (meta.get("supportedQueryFormats") or "")
    fmt = "geojson" if can_geojson else "json"
    oid_field = meta.get("objectIdField") or "OBJECTID"
    print(f"  querying {source.slug} (page={page}, f={fmt}, oid={oid_field}"
          f"{f', expect={expected:,}' if expected is not None else ''})")

    features = []
    last_oid = -1
    while True:
        params = {
            "where": f"{oid_field} > {last_oid}", "outFields": "*",
            "outSR": 4326, "f": fmt,
            "orderByFields": oid_field, "resultRecordCount": page,
        }
        try:
            data = _query(session, source.data_url, params)
        except (requests.ConnectionError, requests.Timeout) as e:
            _pause(e, f"stalled at {len(features):,} features; "
                      f"resuming past {oid_field} {last_oid}")
            continue
        batch = data.get("features", []) if fmt == "geojson" else _esri_to_geojson(data)
        if not batch:
            # An empty page only terminates once we hold rows. No address layer
            # is legitimately empty, so an empty *first* page is the source
            # failing in some way the error check above did not catch -- fail
            # loudly instead of recording today as a city with no addresses.
            if not features:
                raise ValueError("first page returned no features")
            break
        features.extend(batch)
        last_oid = _max_oid(batch, oid_field)
        print(f"\r  fetched {len(features):,} features ...", end="", flush=True)
        if len(batch) < page:
            break
    print()

    # A pull that stopped early is indistinguishable from a complete one once
    # written, and downstream reads it as the city losing tens of thousands of
    # addresses overnight. Only the short side is checked: rows added while we
    # paged are normal, and the OID window has already skipped any that landed
    # behind us.
    if expected is not None and len(features) < expected * (1 - COUNT_TOLERANCE):
        raise DegradedPull(f"truncated pull: {len(features):,} features, "
                           f"layer reports {expected:,}")
    if expected is not None and len(features) > expected * (1 + OVERCOUNT_TOLERANCE):
        raise DegradedPull(f"overlong pull: {len(features):,} features, "
                           f"layer reports {expected:,}")

    # Every row can arrive, and be the wrong rows: the count check above passes
    # a response whose geometry the server dropped on the way out (muskoka
    # 2026-06-30, 66,512 rows and not one coordinate).
    check_geometry(sum(1 for f in features if not has_coords(f)), len(features))

    tmp = filepath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    os.replace(tmp, filepath)  # atomic: a reader never sees a partial file
    return filepath, len(features), {}
