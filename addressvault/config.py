"""Vault directory + path resolution.

The vault root holds the SQLite catalog, the hot ``<slug>-DATE.geojson`` files,
and (under it) the restic repo + password file. Point ADDRESSVAULT_DIR at it, or
pass ``dir=`` to ``Vault``.
"""

import os

DB_NAME = "catalog.db"


def resolve_dir(dir=None):
    d = dir or os.environ.get("ADDRESSVAULT_DIR")
    if not d:
        raise SystemExit(
            "No vault dir. Set ADDRESSVAULT_DIR or pass dir= to point at the vault folder."
        )
    return os.path.abspath(d)


def snapshot_name(slug, date):
    return f"{slug}-{date}.geojson"


def snapshot_path(root, slug, date):
    return os.path.join(root, snapshot_name(slug, date))


def db_path(root):
    return os.path.join(root, DB_NAME)
