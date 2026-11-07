"""Fetch dispatch: pick the access path declared by the source.

All paths write ``<dest_dir>/<slug>-DATE.geojson`` (WGS84) and return
``(filepath, features, headers, reduction)``. ``headers`` carries the source's
Last-Modified/Content-Length (for arcgis, the layer's ``lastEditDate`` where
it reports one; the dataset's ``updated_at`` and the export's byte size for
slims) so the Vault can record them.
``reduction`` is the snapshot's ``sketch.Reduction``, taken in the same pass
that wrote or scanned the file, or None when the static path reused the
previous file unread. ``prev`` (the previous snapshot's headers, feature count
and path) lets the static path skip a re-download when the remote is unchanged.
"""

from . import arcgis, slims, static
from .degraded import DegradedPull  # noqa: F401  (re-exported: raised below, handled by Vault)


def fetch(source, dest_dir, *, prev=None, force=False, today=None):
    if source.access == "arcgis":
        return arcgis.fetch(source, dest_dir, force=force, today=today)
    if source.access == "static":
        return static.fetch(source, dest_dir, prev=prev, force=force, today=today)
    if source.access == "slims":
        return slims.fetch(source, dest_dir, force=force, today=today)
    raise ValueError(f"unknown access: {source.access!r}")
