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
import zipfile
from datetime import date as _date

import ijson
import requests

TIMEOUT = 300


def _int(val):
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _download(url, dest):
    """Stream ``url`` to ``dest``; return its response headers of interest."""
    with requests.get(url, stream=True, timeout=TIMEOUT) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 18):
                f.write(chunk)
        return {"last_modified": r.headers.get("Last-Modified"),
                "content_length": _int(r.headers.get("Content-Length"))}


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
