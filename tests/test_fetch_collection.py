"""The batch writer must produce the file the one-shot dump did, byte for byte:
content dedup hashes it, so a drift stores a fresh copy of every city."""

import json

import pytest

from addressvault import sketch
from addressvault.fetch.collection import CollectionWriter

FIELDS = {"street": "STREET", "num": "OBJECTID"}


def _feat(i, name="Rue Saint-Jérôme", coords=(-79.123456789, 43.5)):
    return {"type": "Feature",
            "properties": {"OBJECTID": i, "STREET": name, "UNIT": None, "AREA": 1e-07},
            "geometry": {"type": "Point", "coordinates": list(coords)} if coords else None}


def _one_shot(features):
    return json.dumps({"type": "FeatureCollection", "features": features})


@pytest.mark.parametrize("batches", [
    [],                                   # no rows at all
    [[_feat(1)]],                         # one batch of one
    [[_feat(1), _feat(2)]],               # one batch of several
    [[_feat(1), _feat(2)], [_feat(3)], [_feat(4, coords=None), _feat(5)]],  # seams
    [[_feat(1)], [], [_feat(2)]],         # an empty batch adds no seam
])
def test_batches_write_the_bytes_the_one_shot_dump_did(tmp_path, batches):
    path = tmp_path / "t-2026-01-01.geojson"
    with CollectionWriter(str(path), FIELDS) as out:
        for b in batches:
            out.write(b)
        out.commit()
    features = [f for b in batches for f in b]
    assert path.read_text(encoding="utf-8") == _one_shot(features)
    assert out.count == len(features)
    assert out.missing == sum(1 for f in features if not f["geometry"])
    assert not (tmp_path / "t-2026-01-01.geojson.tmp").exists()
    # The fingerprints taken on the way past are the ones a re-read would give.
    assert out.reduction == sketch.reduce_file(str(path), FIELDS)


def test_nothing_is_left_behind_when_the_pull_fails_midway(tmp_path):
    path = tmp_path / "t-2026-01-01.geojson"
    with pytest.raises(RuntimeError):
        with CollectionWriter(str(path), FIELDS) as out:
            out.write([_feat(1)])
            raise RuntimeError("page 2 never came")
    assert list(tmp_path.iterdir()) == []


def test_an_uncommitted_file_is_never_visible_under_its_real_name(tmp_path):
    path = tmp_path / "t-2026-01-01.geojson"
    with CollectionWriter(str(path), FIELDS) as out:
        out.write([_feat(1)])
        assert not path.exists()
        out.commit()
    assert path.exists()
