"""Fetch a static file (geojson or shapefile, optionally zipped) -> GeoJSON (4326).

Lifted from address-layerist. A HEAD smart-cache short-circuits the (possibly
~590 MB) download when the remote's Last-Modified/Content-Length match the
previous snapshot. A plain GeoJSON download is already WGS84, so it is moved into
place rather than parsed whole -- only ijson ever streams it. The shapefile path
parses + reprojects in memory (pyshp/pyproj imported lazily).
"""

import glob
import json
import os
import time
import zipfile
from datetime import date as _date

import ijson
import requests

from addressvault import net

TIMEOUT = 300
# A connection dropped mid-body used to cost the whole day's fetch (observed
# 2026-07-17 and 2026-07-23, both on the ~590 MB dump).
RETRIES = 3
RETRY_WAIT = 30


def _int(val):
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _download(url, dest):
    """Stream ``url`` to ``dest``; return its response headers of interest.

    Retries a dropped connection, resuming with a Range request where the server
    allows it and starting over where it doesn't. A content-encoded body can't
    be resumed: Range counts encoded bytes, and what is on disk is decoded.
    """
    got = 0
    resumable = False
    headers = {}
    for attempt in range(RETRIES + 1):
        want = {"Range": f"bytes={got}-"} if got else {}
        try:
            with requests.get(url, stream=True, timeout=TIMEOUT, headers=want) as r:
                r.raise_for_status()
                if r.status_code != 206:  # full body: (re)start from byte 0
                    got = 0
                    resumable = not r.headers.get("Content-Encoding")
                    headers = {"last_modified": r.headers.get("Last-Modified"),
                               "content_length": _int(r.headers.get("Content-Length"))}
                with open(dest, "ab" if got else "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 18):
                        f.write(chunk)
                        got += len(chunk)
            return headers
        except (requests.ConnectionError, requests.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            total = headers.get("content_length")
            if resumable and total and got >= total:
                return headers  # dropped on the last chunk; the file is whole
            if attempt == RETRIES:
                # Out of retries. If the link itself is down, this was never a
                # source failure -- say so, so the caller skips the day instead
                # of logging a failure. No waiting here: we hold the slug's
                # lease, so deferring is the pre-lease gate's job, not ours.
                net.wait_for_link(wait=False)
                raise
            if not resumable:
                got = 0
            print(f"  download dropped at {got:,} bytes ({e});"
                  f" retrying in {RETRY_WAIT}s")
            time.sleep(RETRY_WAIT)


def _head(url):
    try:
        r = requests.head(url, timeout=30, allow_redirects=True)
        r.raise_for_status()
        return {"last_modified": r.headers.get("Last-Modified"),
                "content_length": _int(r.headers.get("Content-Length"))}
    except requests.RequestException as e:
        print(f"  (HEAD check failed, will download: {e})")
        return {}


def _unzip(path, dest_dir):
    with zipfile.ZipFile(path) as z:
        z.extractall(dest_dir)


def _count_features(path):
    """Stream-count Features without loading the file into memory."""
    with open(path, "rb") as f:
        return sum(1 for _ in ijson.items(f, "features.item"))


def _read_shapefile(shp_path, source_crs=""):
    """Read a point shapefile, reprojecting to EPSG:4326 using its .prj."""
    import shapefile  # pyshp
    from pyproj import CRS, Transformer

    prj_path = shp_path[:-4] + ".prj"
    transformer = None
    crs = None
    if os.path.exists(prj_path):
        with open(prj_path, encoding="utf-8", errors="replace") as f:
            crs = CRS.from_wkt(f.read())
    elif source_crs:
        crs = CRS.from_user_input(source_crs)
    if crs is not None and crs.to_epsg() != 4326:
        transformer = Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)

    reader = shapefile.Reader(shp_path)
    field_names = [f[0] for f in reader.fields[1:]]  # drop DeletionFlag
    feats = []
    for sr in reader.iterShapeRecords():
        pts = sr.shape.points
        if not pts:
            continue
        x, y = pts[0]
        if transformer:
            x, y = transformer.transform(x, y)
        props = dict(zip(field_names, sr.record))
        feats.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Point", "coordinates": [x, y]},
        })
    return feats


def _locate(work_dir, pattern):
    hits = glob.glob(os.path.join(work_dir, "**", pattern), recursive=True)
    if not hits:
        raise FileNotFoundError(f"no {pattern} found in {work_dir}")
    return hits[0]


def fetch(source, dest_dir, *, prev=None, force=False, today=None):
    day = today or _date.today().isoformat()
    os.makedirs(dest_dir, exist_ok=True)
    filename = f"{source.slug}-{day}.geojson"
    filepath = os.path.join(dest_dir, filename)

    # Remote unchanged since the previous snapshot: reuse it, skip the download.
    if not force and prev and prev.get("path") and os.path.isfile(prev["path"]):
        remote = _head(source.data_url)
        if (remote and remote.get("last_modified") == prev.get("last_modified")
                and remote.get("content_length") == prev.get("content_length")):
            print(f"  reusing {os.path.basename(prev['path'])} (remote unchanged)")
            return prev["path"], _count_features(prev["path"]), remote

    work = os.path.join(dest_dir, "_download")
    os.makedirs(work, exist_ok=True)
    raw = os.path.join(work, "download.bin")
    print(f"  downloading {source.data_url}")
    headers = _download(source.data_url, raw)

    is_zip = zipfile.is_zipfile(raw)
    if is_zip:
        _unzip(raw, work)

    if source.format == "shapefile":
        shp = _locate(work, "*.shp")
        print(f"  reading shapefile {os.path.basename(shp)}")
        features = _read_shapefile(shp, source.source_crs)
        tmp = filepath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": features}, f)
        os.replace(tmp, filepath)  # atomic
        count = len(features)
    else:  # geojson -- already WGS84; move it into place without parsing whole
        if is_zip:
            try:
                src = _locate(work, "*.geojson")
            except FileNotFoundError:
                src = _locate(work, "*.json")
        else:
            src = raw
        os.replace(src, filepath)
        count = _count_features(filepath)

    print(f"  parsed {count:,} features")
    return filepath, count, headers
