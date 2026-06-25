"""Fetch dispatch: pick the access path declared by the source.

Both paths write ``<dest_dir>/<slug>-DATE.geojson`` (WGS84) and return
``(filepath, features, headers)``. ``headers`` carries the source's
Last-Modified/Content-Length (empty for arcgis) so the Vault can record them.
``prev`` (the previous snapshot's headers + path) lets the static path skip a
re-download when the remote is unchanged.
"""

from . import arcgis, static


def fetch(source, dest_dir, *, prev=None, force=False, today=None):
    if source.access == "arcgis":
        return arcgis.fetch(source, dest_dir, force=force, today=today)
    if source.access == "static":
        return static.fetch(source, dest_dir, prev=prev, force=force, today=today)
    raise ValueError(f"unknown access: {source.access!r}")
