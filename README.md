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

## Concurrency: one writer per city

Consumers are separate processes that each call `pull()`, so the vault gates
writes with a **per-slug lease** in the catalog (claimed with a `BEGIN IMMEDIATE`
transaction — SQLite is the cross-process lock). Only one process pulls a given
city at a time; the rest do **not** start a second fetch. A concurrent
`pull`/`thaw` of a busy slug raises `PullInProgress`, carrying a coarse
`PullStatus` (`fetching → writing → done/failed`) to poll:

```python
from addressvault import Vault, PullInProgress

try:
    v.pull("toronto")                       # I won the lease: fetch + record
except PullInProgress as e:
    while e.status.active:                   # someone else is already pulling it
        time.sleep(2)
        e.status = v.pull_status("toronto")  # coarse progress, no second fetch
    snap = v.snapshot("toronto", "latest")   # read the copy they just wrote
```

Most consumers just want that coalescing without the loop, so `pull` (and `thaw`)
take an opt-in `wait`:

```python
snap = v.pull("toronto", wait=True)          # do it, or block on the in-flight
                                             # pull and return its result
```

`wait=True` returns the holder's freshly written `latest` (no second fetch); if
that holder failed or crashed, it takes over. On the CLI it is `--wait`:

```
addressvault pull toronto --wait             # coalesce; exits non-zero on timeout
```

`sweep`/`recool` take the same lease and simply skip a city being written this
cycle. A crashed holder's lease goes stale after `LEASE_TTL_SECONDS` (30 min) and
is reclaimed, so a dead process can't wedge a city.

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
addressvault pull <slug> [--force] [--wait]            # --wait: coalesce onto an in-flight pull
addressvault pull-due                     # pull everything due today, then sweep
addressvault serve [--interval N]         # run the self-scheduler (one writer)
addressvault snapshots <slug> [--from D --to D --tier hot|cold] [--json]
addressvault data <slug> [<date>|latest] [-o PATH|-]   # stream bytes; errors with a thaw hint if cold
addressvault status <slug> [--json]       # progress of an in-flight (or the last) pull
addressvault thaw <slug> <date> [--ttl-hours N]
addressvault sweep [--keep-days N]        # cool aged hot copies; drop expired thaws
addressvault stats [--json]
addressvault report [--out PATH] [--days N]            # self-contained HTML status page
```

`report` renders one static HTML file from the catalog — a city × day matrix
(new / unchanged / failed / no attempt), a month calendar, storage and growth
per tier including catalog-vs-disk drift, and the failure log. It defaults to
`<vault>/report.html`; open it in a browser, no server involved.

## Scheduling

`addressvault serve` is a self-contained loop; the per-slug lease (see
**Concurrency** above) is what actually prevents pull/restic races, so it is safe
to run alongside consumers that pull directly. Or drive it from the OS instead:

```
addressvault pull-due        # run hourly/daily from cron or Windows Task Scheduler
```

### The link gate

A `pull` that would download first checks the link, since two host-wide
conditions make fetching pointless or expensive:

- **offline** — nothing answers on 443 at `1.1.1.1`/`8.8.8.8`/`9.9.9.9`;
- **metered** — Windows reports cellular/tethering, or a Wi-Fi flagged
  "Metered connection".

Either way the pull re-checks every 15 min until the link is usable, then
fetches — or gives up at a nightly cutoff (22:30, before the quiet window) if it
never clears. A skipped pull records no snapshot and no failed job, so the source
stays due, the next run retries it, and the report shows "no attempt" rather than
a failure — a link that drops *mid*-fetch ends the lease `deferred` rather than
`failed` for the same reason. `pull-due` stops the whole run at the first
unusable link instead of walking the remaining cities into the same wall; it
still sweeps, since restic is local.

The two probes fail in opposite directions on purpose: metered fails **open** (a
broken API never silently blocks updates), offline fails **closed** (nothing
answering *is* the observation). DNS is deliberately not probed — a resolver that
fails while the link is up is a fetch-level error, retried by the fetchers.

Callers who want the error rather than the wait pass `Vault(link_wait=False)` and
catch `LinkUnavailable` (or `Offline`/`Metered`), exported from the package root.
The CLI's `pull` and `pull-due` exit **75** (`EX_TEMPFAIL`) when the link is
unusable, so a wrapper script can tell "no network" apart from a real failure.

## Config

- `ADDRESSVAULT_DIR` — the vault folder (catalog, hot files, restic repo). Required
  unless you pass `dir=` / `--dir`.
- `restic` on `PATH` powers the cold tier. Without it, pulls/reads still work; the
  archive tier degrades gracefully (sweep is a no-op, thaw errors). The restic
  password is stored in plaintext at `<vault>/restic.pass` — it enables local
  chunk-dedup, it is **not** an encryption-at-rest boundary.

## Install / test

Python 3.11+.

```
pip install -e .            # or .[shapefile] for pyshp/pyproj-backed sources
pytest                      # restic lifecycle test is skipped if restic is absent
```

## Consumers

`ontario-address-changes` and `toronto-addresses-import` consume the vault via
`import addressvault` (each declares it as a dependency): their `fetch` step calls
`Vault().pull(slug, wait=True)` (coalescing onto any in-flight pull) and reads it
back with `Vault().path(slug, "latest")`.

`address-layerist` deliberately does **not** import `addressvault`: the tile
engine only slims an input GeoJSON, so it reads the newest `<slug>-DATE.geojson`
straight from a directory (`$ADDRESSVAULT_DIR` by default) and knows nothing about
the vault API. Its city tasks pull first, then build:
`addressvault pull <slug> --wait && python run.py update`.

The vault has no scheduler process of its own here: it is fed as a side effect of
those daily jobs (each does a `pull`). `sweep` (aging hot days into the cold tier)
and `recool` only run via the scheduled `addressvault sweep` (or `pull-due`) —
see **Scheduling** above.
