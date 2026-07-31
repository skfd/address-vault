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

TIMEOUT = 120
DEFAULT_PAGE = 2000
# maps.ottawa.ca (F5 GSLB) started dropping new TCP connections after ~15-35
# rapid page requests (observed 2026-07-16); a keep-alive Session stays under
# that limit. If a fetch still stalls, wait out the observed ~15 min block and
# resume from last_oid instead of failing the whole city.
RETRIES = 3
RETRY_WAIT = 900


def _layer_meta(session, url):
    r = session.get(url, params={"f": "json"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _query(session, url, params):
    r = session.get(url + "/query", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


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
    while True:
        try:
            meta = _layer_meta(session, source.data_url)
            break
        except (requests.ConnectionError, requests.Timeout) as e:
            _pause(e, f"{source.slug} metadata unreachable")

    page = min(meta.get("maxRecordCount") or DEFAULT_PAGE, DEFAULT_PAGE)
    can_geojson = "geoJSON" in (meta.get("supportedQueryFormats") or "")
    fmt = "geojson" if can_geojson else "json"
    oid_field = meta.get("objectIdField") or "OBJECTID"
    print(f"  querying {source.slug} (page={page}, f={fmt}, oid={oid_field})")

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
            break
        features.extend(batch)
        last_oid = _max_oid(batch, oid_field)
        print(f"\r  fetched {len(features):,} features ...", end="", flush=True)
        if len(batch) < page:
            break
    print()

    tmp = filepath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    os.replace(tmp, filepath)  # atomic: a reader never sees a partial file
    return filepath, len(features), {}
