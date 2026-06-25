import pytest

from addressvault import sources

SAMPLE = '''
slug = "barrie"
provider = "City of Barrie"
data_url = "https://example.com/COB_ADDRESS_POINT/FeatureServer/0"
access = "arcgis"
format = "geojson"
license_name = "Open Government Licence - Barrie"

[identity]
key_field = "ADDRESSID"

[fields]
number = "ADDRNUMBER"
street = "SSTRNAME"
'''


def test_from_toml(tmp_path):
    p = tmp_path / "barrie.toml"
    p.write_text(SAMPLE)
    src = sources.from_toml(str(p))
    assert src.slug == "barrie"
    assert src.access == "arcgis"
    assert src.fields == {"number": "ADDRNUMBER", "street": "SSTRNAME"}
    assert src.schedule == "daily"   # default


def test_load_dir(tmp_path):
    (tmp_path / "a.toml").write_text(SAMPLE)
    (tmp_path / "b.toml").write_text(SAMPLE.replace('"barrie"', '"other"'))
    srcs = sources.load_dir(str(tmp_path))
    assert sorted(s.slug for s in srcs) == ["barrie", "other"]


def test_validation_rejects_bad_access(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text(SAMPLE.replace('access = "arcgis"', 'access = "ftp"'))
    with pytest.raises(SystemExit):
        sources.from_toml(str(p))


def test_validation_rejects_missing_key(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text('slug = "x"\nprovider = "X"\naccess = "static"\nformat = "geojson"\n')
    with pytest.raises(SystemExit):
        sources.from_toml(str(p))
