import json

from addressvault import sketch

FIELDS = {"number": "NUM", "street": "ST"}


def _feat(i, street="MAIN STREET", x=None, **extra):
    return {"type": "Feature",
            "properties": {"NUM": str(i), "ST": street, **extra},
            "geometry": {"type": "Point",
                         "coordinates": [i if x is None else x, 43.0]}}


def _write(path, feats):
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    return str(path)


def _churn(tmp_path, a, b, fields=FIELDS):
    return sketch.churn(sketch.of_file(_write(tmp_path / "a.geojson", a), fields),
                        sketch.of_file(_write(tmp_path / "b.geojson", b), fields))


def test_identical_content_has_no_churn(tmp_path):
    feats = [_feat(i) for i in range(500)]
    assert _churn(tmp_path, feats, list(feats)) == 0


def test_row_order_is_not_churn(tmp_path):
    # A source paging its layer differently reorders rows without changing one
    # address. The sketch is of the *set*, so it must not notice.
    feats = [_feat(i) for i in range(500)]
    assert _churn(tmp_path, feats, list(reversed(feats))) == 0


def test_republished_metadata_is_not_address_churn(tmp_path):
    """The brant case, which is why whole-feature hashing is not enough.

    That source regenerates every GLOBALID and shifts every CREATED_DATE by a
    day on each publish while not moving a single address. Hashing whole
    features calls that 100% churn every morning; hashing the declared address
    fields calls it what it is.
    """
    day1 = [_feat(i, GLOBALID=f"aaa-{i}", CREATED_DATE=1786170236828)
            for i in range(500)]
    day2 = [_feat(i, GLOBALID=f"zzz-{i}", CREATED_DATE=1786256402135)
            for i in range(500)]
    assert _churn(tmp_path, day1, day2) == 0


def test_a_rename_holding_count_and_size_is_churn(tmp_path):
    # The blind spot: same feature count, same byte count, every street renamed
    # to an equal-length name. Neither scalar test can see this.
    day1 = [_feat(i, street="MAIN STREET") for i in range(500)]
    day2 = [_feat(i, street="OAKS AVENUE") for i in range(500)]
    a, b = _write(tmp_path / "1.geojson", day1), _write(tmp_path / "2.geojson", day2)
    assert len(open(a).read()) == len(open(b).read())  # byte-neutral, truly
    assert sketch.churn(sketch.of_file(a, FIELDS), sketch.of_file(b, FIELDS)) == 1


def test_a_partial_rewrite_is_measured_not_just_detected(tmp_path):
    # 10% of streets renamed: the estimate has to land near 10%, because the
    # threshold is a percentage and not a boolean.
    day1 = [_feat(i) for i in range(2000)]
    day2 = [_feat(i, street="RENAMED ROAD") if i % 10 == 0 else _feat(i)
            for i in range(2000)]
    assert 0.07 < _churn(tmp_path, day1, day2) < 0.13


def test_a_regeocode_is_churn(tmp_path):
    day1 = [_feat(i) for i in range(500)]
    day2 = [_feat(i, x=i + 0.5) for i in range(500)]  # every point moved
    assert _churn(tmp_path, day1, day2) == 1


def test_coordinate_jitter_below_a_tenth_of_a_metre_is_not_churn(tmp_path):
    # Re-exported floats wobble in the last digits; that is not a re-geocode.
    day1 = [_feat(i, x=i + 0.1234567) for i in range(500)]
    day2 = [_feat(i, x=i + 0.12345671) for i in range(500)]
    assert _churn(tmp_path, day1, day2) == 0


def test_a_feature_with_no_usable_point_still_counts(tmp_path):
    # Unlocated addresses appear in real layers; they must not break the pass,
    # and their address still has to be part of the fingerprint.
    day1 = [{"type": "Feature", "properties": {"NUM": "1", "ST": "MAIN"},
             "geometry": None}]
    day2 = [{"type": "Feature", "properties": {"NUM": "1", "ST": "RENAMED"},
             "geometry": {"type": "Point", "coordinates": [None, None]}}]
    assert _churn(tmp_path, day1, day1) == 0
    assert _churn(tmp_path, day1, day2) == 1


def test_a_sketch_is_capped_and_survives_a_round_trip(tmp_path):
    feats = [_feat(i) for i in range(sketch.K * 3)]
    sk = sketch.of_file(_write(tmp_path / "a.geojson", feats), FIELDS)
    assert len(sk) == sketch.K            # bounded no matter how big the city
    assert sk == sorted(sk)
    assert sketch.decode(sketch.encode(sk)) == sk


def test_churn_is_unknown_when_either_day_predates_sketching(tmp_path):
    feats = [_feat(i) for i in range(10)]
    sk = sketch.of_file(_write(tmp_path / "a.geojson", feats), FIELDS)
    # A day pulled before the column existed stores NULL. That is "cannot say",
    # not "nothing changed", and must never read as a quiet day.
    assert sketch.churn(sketch.decode(None), sk) is None
    assert sketch.churn(sk, sketch.decode(b"")) is None
