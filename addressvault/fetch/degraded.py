"""What makes an arrived pull unusable, shared by both fetch paths.

``DegradedPull`` lives here rather than beside either fetcher because both
raise it and the Vault catches it: it is the contract between them, not an
ArcGIS detail.
"""

# How many rows may arrive without coordinates before what came back is not an
# address layer any more. A point layer whose geometry is gone still carries
# every row, every field and a plausible byte count, so no other check here
# notices: muskoka 2026-06-30 served all 66,512 rows with a null geometry and
# was recorded as a clean day. Only an incidental 14.4% drop in file size ever
# gave it away, and a source that drops geometry while padding the properties
# would not even do that.
#
# Not zero: an address can legitimately sit in the table before anybody has
# located it. The band either side is wide, though -- across the 76 snapshots on
# disk (11.5M features, every source represented at least once) the most any
# legitimate pull missed was 0.0026%, one row of huron's 38,315, and the
# failure sat at 100%.
GEOMETRY_TOLERANCE = 0.01


class DegradedPull(ValueError):
    """The pull arrived, and is not trustworthy: the transport worked but what
    came back does not describe the layer. Kept apart from an ordinary fetch
    error because the two want different responses -- a timeout resolves itself
    on the next run, while this one is either a source that has started serving
    damaged data or a real change big enough that somebody should look."""


def has_coords(feature):
    """Whether a GeoJSON feature carries a usable position.

    One predicate for both fetch paths, so "missing geometry" cannot come to
    mean two different things depending on which one pulled the city. A null
    ``geometry``, a geometry with no ``coordinates``, and an empty coordinate
    list are the three shapes a located-nowhere row has been seen in."""
    geom = feature.get("geometry")
    return bool(geom and geom.get("coordinates"))


def check_geometry(missing, total):
    """Refuse a point layer that came back without its points.

    Counted as a share rather than any-at-all so that the handful of addresses a
    city has recorded but not yet located does not refuse the city every day."""
    if total and missing > total * GEOMETRY_TOLERANCE:
        raise DegradedPull(f"geometry-less pull: {missing:,} of {total:,} "
                           f"features have no coordinates ({missing / total:.1%})")
