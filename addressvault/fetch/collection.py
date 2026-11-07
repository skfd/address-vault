"""Write a FeatureCollection a batch at a time, as the arcgis pages arrive.

The fetchers used to hold the whole layer as dicts and ``json.dump`` it at the
end. That cost twice over: york's 431,980 features as Python objects run to
gigabytes, and ``json.dump`` streams through the pure-Python encoder (only
``json.dumps`` takes the C one) -- 37 s for york's 448 MB, against 7.8 s for the
same bytes through ``json.dumps``.

The bytes must not move. Content dedup compares sha256 against the previous
day, so a file that serialised one separator differently would store a fresh
hot copy of every city on the first day and break each ``unchanged_since``
chain. Each batch goes through ``json.dumps`` with the defaults ``json.dump``
used, and the seams between batches are the same ``", "`` it put between
features, so the file is identical to the one-shot dump. ``tests`` pin that.

A refused or failed pull must leave nothing behind, as it did when nothing
touched the disk until every check had passed: until ``commit`` everything is
in ``<path>.tmp``, and leaving the ``with`` block on an exception removes it.
"""

import json
import os

from addressvault.fetch.degraded import has_coords

_HEAD = '{"type": "FeatureCollection", "features": ['
_TAIL = "]}"


class CollectionWriter:
    def __init__(self, filepath):
        self.path = filepath
        self.tmp = filepath + ".tmp"
        self.count = 0
        self.missing = 0  # features with no usable position; see check_geometry
        self._f = open(self.tmp, "w", encoding="utf-8")
        self._f.write(_HEAD)

    def write(self, batch):
        if not batch:
            return
        if self.count:
            self._f.write(", ")
        self._f.write(json.dumps(batch)[1:-1])
        self.count += len(batch)
        self.missing += sum(1 for f in batch if not has_coords(f))

    def commit(self):
        """Finish the file and move it into place (atomic: a reader never sees
        a partial file)."""
        self._f.write(_TAIL)
        self._f.close()
        os.replace(self.tmp, self.path)

    def discard(self):
        self._f.close()
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.discard()
        return False
