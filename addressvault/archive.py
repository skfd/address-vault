"""restic cold tier: chunk-deduped, kept-forever archive of dated dumps.

Lifted from address-layerist's cache.py. Exposes the restic primitives the Vault
drives -- ``backup`` (hot -> cold) and ``dump`` (copy cold -> a target, never
moving: the archived copy is immutable and permanent). Catalog bookkeeping stays
in the Vault. Everything is best-effort: no restic / locked repo -> return False,
never raise, so a build never fails on the archive tier.

The restic password lives in plaintext next to the cache (``restic.pass``) -- it
enables local chunk-dedup, it is NOT an encryption-at-rest boundary.
"""

import json
import os
import secrets
import shutil
import subprocess


def available():
    return shutil.which("restic") is not None


def _repo(root):
    return os.environ.get("RESTIC_REPOSITORY") or os.path.join(root, "restic")


def _env(root):
    repo = _repo(root)
    pf = os.path.join(root, "restic.pass")
    return {**os.environ, "RESTIC_REPOSITORY": repo, "RESTIC_PASSWORD_FILE": pf}, repo, pf


def _run(args, env):
    return subprocess.run(["restic", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def _initialized(repo):
    return os.path.exists(os.path.join(repo, "config"))


def ensure_repo(root):
    """Create the password file and init the repo if needed; return env or None."""
    env, repo, pf = _env(root)
    if not os.path.exists(pf):
        os.makedirs(os.path.dirname(pf), exist_ok=True)
        with open(pf, "w", encoding="utf-8") as f:
            f.write(secrets.token_hex(16))
    if not _initialized(repo):
        _run(["init"], env)
        if not _initialized(repo):  # tolerate a concurrent init that won the race
            return None
    return env


def backup(root, path, slug, date):
    """Archive one snapshot file. Returns True on success."""
    env = ensure_repo(root)
    if env is None:
        return False
    r = _run(["backup", path, "--tag", f"slug={slug},date={date},addressvault"], env)
    return r.returncode == 0


def archived_dates(root, slug):
    """Sorted dates archived for a slug (empty if no repo / restic unavailable)."""
    if not available():
        return []
    env, repo, _ = _env(root)
    if not _initialized(repo):
        return []
    r = _run(["snapshots", "--tag", f"slug={slug}", "--json"], env)
    if r.returncode != 0:
        return []
    dates = set()
    for s in json.loads(r.stdout or "[]"):
        for t in s.get("tags", []):
            if t.startswith("date="):
                dates.add(t[len("date="):])
    return sorted(dates)


def dump(root, slug, date, target):
    """Copy <slug>-<date>.geojson out of restic into ``target`` (atomic). True on ok.

    Uses ``restic dump`` (stream the one file) rather than ``restic restore`` --
    the latter recreates the original absolute path and trips over Windows
    timestamp permissions on the synthesised parent dirs.
    """
    if not available():
        return False
    env, repo, _ = _env(root)
    if not _initialized(repo):
        return False
    name = f"{slug}-{date}.geojson"

    r = _run(["ls", "latest", "--tag", f"slug={slug},date={date}", "--json"], env)
    if r.returncode != 0:
        return False
    stored = None
    for line in r.stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "file" and obj.get("path", "").replace("\\", "/").endswith(name):
            stored = obj["path"]
            break
    if not stored:
        return False

    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "wb") as out:
        r = subprocess.run(["restic", "dump", "latest", "--tag", f"slug={slug},date={date}",
                            stored], stdout=out, stderr=subprocess.PIPE, env=env)
    if r.returncode != 0:
        os.remove(tmp)
        return False
    os.replace(tmp, target)
    return True
