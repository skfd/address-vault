# address-vault

A single-host, tiered store for **raw city address dumps**. It pulls each source
on a schedule, keeps every dated snapshot forever across a **hot tier** (on disk)
and a **cold tier** (restic, chunk-deduped), and lets consumers list which dates
exist, see whether each is hot or cold, thaw a cold day on demand, and read a
specific date or `latest`.

It exists so the map-tile engine (`address-layerist`) and the change tracker
(`ontario-address-changes`) stop pulling city sites directly and stop owning
restic — they just read from one vault.

## Interface: the library is the API

There is **no HTTP server** — everything is single-host, so consumers import the
library directly:

```python
from addressvault import Vault, Archived

v = Vault(dir=r"D:\address-vault-data")   # or set ADDRESSVAULT_DIR
for snap in v.snapshots("toronto"):
    print(snap.date, snap.tier, snap.features)

try:
    path = v.path("toronto", "latest")     # a hot file path to stream
except Archived:
    v.thaw("toronto", "2026-01-01")         # copy the cold day back, then read
```

A thin CLI wraps the same calls for cron and humans (`addressvault ...`).

## Tiers

A snapshot is two independent booleans, because **thaw copies (never moves)** —
the cold copy is immutable and permanent:

| Transition | restic | disk |
|---|---|---|
| **sweep** (snapshot ages past `--keep-days`) | `backup` | delete hot file |
| **thaw** (cold → temporarily hot) | `dump` (copy out) | write hot file |
| **re-cool** (thaw TTL elapsed) | none — already archived | delete hot file |

Identical daily dumps are content-deduped: a pull whose bytes match the previous
snapshot records the date with `unchanged_since` pointing at the canonical day and
stores no second copy.

## CLI

```
addressvault seed <datasets_dir>          # import sources from ontario-address-changes/datasets/*.toml
addressvault sources [--json]
addressvault pull <slug> [--force]
addressvault pull-due                     # pull everything due today, then sweep
addressvault serve [--interval N]         # run the self-scheduler (one writer)
addressvault snapshots <slug> [--from D --to D --tier hot|cold] [--json]
addressvault data <slug> [<date>|latest] [-o PATH|-]   # stream bytes; errors with a thaw hint if cold
addressvault thaw <slug> <date> [--ttl-hours N]
addressvault sweep [--keep-days N]
addressvault stats [--json]
```

## Scheduling

`addressvault serve` is a self-contained loop (the single writer — no restic lock
races). Or drive it from the OS instead:

```
addressvault pull-due        # run hourly/daily from cron or Windows Task Scheduler
```

## Config

- `ADDRESSVAULT_DIR` — the vault folder (catalog, hot files, restic repo). Required
  unless you pass `dir=` / `--dir`.
- `restic` on `PATH` powers the cold tier. Without it, pulls/reads still work; the
  archive tier degrades gracefully (sweep is a no-op, thaw errors). The restic
  password is stored in plaintext at `<vault>/restic.pass` — it enables local
  chunk-dedup, it is **not** an encryption-at-rest boundary.

## Install / test

```
pip install -e .            # or .[shapefile] for pyshp/pyproj-backed sources
pytest                      # restic lifecycle test is skipped if restic is absent
```

## Not yet done (follow-up)

Rewiring `address-layerist` and `ontario-address-changes` to consume the vault
(via `import addressvault`) instead of pulling city sites directly, and removing
`address-layerist`'s in-engine `cache.py`. This repo ships the standalone vault
first; the consumer cutover is a deliberate second step.
