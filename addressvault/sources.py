"""Source registry: the list of city data sources the vault pulls.

A Source carries the data-source keys shared with
``ontario-address-changes/datasets/<slug>.toml`` (slug/provider/data_url/access/
format/license_name/source_crs/[fields]) plus vault-only scheduling. ``load_dir``
seeds the registry straight from that datasets folder; tracker-only tables
(``[identity]``, ``[classes]``, ...) are ignored.
"""

import glob
import os
import tomllib
from dataclasses import dataclass, field

_REQUIRED = ("slug", "provider", "data_url", "access", "format")
_VALID_ACCESS = ("arcgis", "static")
_VALID_FORMAT = ("geojson", "shapefile")


@dataclass
class Source:
    slug: str
    provider: str
    data_url: str
    access: str
    format: str
    license_name: str = ""
    source_crs: str = ""                       # e.g. "EPSG:2952"; reproject to WGS84
    fields: dict = field(default_factory=dict)  # source property -> canonical name
    schedule: str = "daily"
    enabled: bool = True


def _validate(raw, where):
    missing = [k for k in _REQUIRED if not raw.get(k)]
    if missing:
        raise SystemExit(f"{where}: missing required keys: {missing}")
    if raw["access"] not in _VALID_ACCESS:
        raise SystemExit(f"{where}: access must be one of {_VALID_ACCESS}")
    if raw["format"] not in _VALID_FORMAT:
        raise SystemExit(f"{where}: format must be one of {_VALID_FORMAT}")


def from_toml(path):
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    _validate(raw, path)
    return Source(
        slug=raw["slug"],
        provider=raw["provider"],
        data_url=raw["data_url"],
        access=raw["access"],
        format=raw["format"],
        license_name=raw.get("license_name", ""),
        source_crs=raw.get("source_crs", ""),
        fields=raw.get("fields", {}),
        schedule=raw.get("schedule", "daily"),
        enabled=raw.get("enabled", True),
    )


def load_dir(datasets_dir):
    """Parse every ``*.toml`` in a datasets folder into Sources (sorted by slug)."""
    paths = sorted(glob.glob(os.path.join(datasets_dir, "*.toml")))
    if not paths:
        raise SystemExit(f"no *.toml found in {datasets_dir}")
    return [from_toml(p) for p in paths]
