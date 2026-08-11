"""A fixed-size fingerprint of a snapshot's addresses, for measuring churn.

The report can already see a day where the feature count moved, or where the
byte count moved apart from it. What neither can see is a source that rewrites
rows in place at constant size -- renaming streets, re-geocoding points, swapping
two fields row for row. That is a full dataset turnover with no scalar signal at
all, and it is the damaging case for consumers.

Catching it means comparing the rows themselves, which sounds like diffing two
multi-hundred-MB files. It is not: each snapshot is reduced once, at pull time,
to a **bottom-k sketch** -- the k smallest 64-bit hashes of its addresses. Two
sketches estimate the Jaccard similarity of the full sets they came from, so the
report compares 8 KB against 8 KB and gets back the fraction of rows that were
rewritten. Reducing a 251 MB snapshot costs about three seconds, against a pull
that just downloaded it.

What gets hashed is the address, not the feature. Sources republish churning
metadata -- brant regenerates every ``GLOBALID`` and shifts every
``CREATED_DATE`` daily while not moving a single address -- so hashing whole
features reports 100% churn every morning and is worthless. Each source already
declares which properties carry the address (``Source.fields``, shared with
``ontario-address-changes``); those, plus the rounded coordinate, are the
identity. Everything else is publishing noise and is deliberately excluded.
"""

import hashlib
import heapq
import json

import ijson

# 1,024 hashes -- 8 KB per snapshot. Calibrated by rewriting a known fraction of
# a real snapshot: k=1024 reads a true 2% churn as 1.74% and a true 1% as 0.99%,
# which separates the two cleanly either side of the threshold. k=256 is half
# the size but reads that same 1% as 1.39%, close enough to 2% to cry wolf;
# k=4096 costs four times as much and is no more accurate than k=1024.
K = 1024

# Only canonical snapshots carry one (dedup days inherit, as they do for
# features/bytes), so this is ~10 sketches a day: ~32 MB a year.
_HASH_BYTES = 8


def project(feat, fields):
    """The address a feature carries, as this source declares it.

    ``fields`` is the source's canonical-name -> source-property map. Sorting by
    canonical name means two sources spelling the same field differently still
    hash their own data consistently day to day, which is all that is required:
    sketches are only ever compared within one slug.
    """
    props = feat.get("properties") or {}
    vals = [str(props.get(src, "")) for _, src in sorted(fields.items())]
    coords = (feat.get("geometry") or {}).get("coordinates") or []
    try:
        # Six decimals is ~0.1 m: finer than that is float noise, not a
        # re-geocode. Also normalises ijson's Decimal against the float the
        # arcgis path builds, so the two never disagree about the same point.
        pt = [round(float(c), 6) for c in coords[:2]]
    except (TypeError, ValueError):
        pt = []  # an address with no usable point is still an address
    return json.dumps([vals, pt], separators=(",", ":"))


def _h64(s):
    return int.from_bytes(
        hashlib.blake2b(s.encode(), digest_size=_HASH_BYTES).digest(), "big")


def of_file(path, fields, k=K):
    """Stream a snapshot and return its sketch: the k smallest address hashes.

    Streamed, so peak memory is the sketch itself rather than the file. Duplicate
    addresses collapse -- the sketch is of the *set* of addresses, which is what
    makes it comparable across days when the row order changes.
    """
    heap, held = [], set()  # max-heap of negated hashes; `held` mirrors it
    with open(path, "rb") as f:
        for feat in ijson.items(f, "features.item"):
            h = _h64(project(feat, fields))
            if h in held:
                continue
            if len(heap) < k:
                heapq.heappush(heap, -h)
                held.add(h)
            elif h < -heap[0]:
                held.discard(-heapq.heappushpop(heap, -h))
                held.add(h)
    return sorted(-x for x in heap)


def encode(sk):
    return b"".join(h.to_bytes(_HASH_BYTES, "big") for h in sk)


def decode(blob):
    if not blob:
        return []
    return [int.from_bytes(blob[i:i + _HASH_BYTES], "big")
            for i in range(0, len(blob), _HASH_BYTES)]


def churn(a, b):
    """Fraction of addresses rewritten between two sketches, or None if either
    snapshot predates sketching.

    The k smallest hashes of the union are an unbiased sample of the union, so
    the share of them present in both sketches estimates the Jaccard similarity
    J of the full sets. Replacing a fraction p of a set gives J = (1-p)/(1+p),
    inverted here to recover p -- a number on the same scale as the feature and
    byte tests, rather than a bare similarity.

    Assumes the two sets are about the same size, which is what makes p
    meaningful; when they are not, the feature-count test has already fired.
    """
    if not a or not b:
        return None
    sa, sb = set(a), set(b)
    # Never estimate over more hashes than the smaller sketch can vouch for.
    merged = sorted(sa | sb)[:min(len(a), len(b))]
    if not merged:
        return None
    j = sum(1 for h in merged if h in sa and h in sb) / len(merged)
    return (1 - j) / (1 + j)
