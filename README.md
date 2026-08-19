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
addressvault report [--out PATH] [--days N] [--json]   # HTML status page, or the same as data
addressvault review <slug> <date> <verdict> -m NOTE    # answer a flagged large change
addressvault backfill [slug ...]          # reduce on-disk days the fingerprint columns predate
```

`report` renders one static HTML file from the catalog — a city × day matrix
(new / large change / unchanged / failed / no attempt), a month calendar, storage
and growth per tier including catalog-vs-disk drift, the **Large changes** list,
and the failure log. It defaults to `<vault>/report.html`; open it in a browser,
no server involved.

**Large changes** is the operator's cue to look at a city. A day lands there when
something moved against the previous snapshot, by whichever of these tests sees
it first — they are ordered so that the most explanatory answer wins:

| test | catches | threshold | needs |
| --- | --- | --- | --- |
| feature count | a city gaining or losing addresses wholesale | 2% | nothing — retroactive over all history |
| duplicate rows | a source replaying part of its own load | 0.5% move | a stored count, so forward-only |
| published fields | a schema change — named, not mistaken for a rewrite | any change | a stored field list, so forward-only |
| bytes vs. features | rows rewritten in place, which a count-only check cannot see | 2% | nothing — retroactive |
| addresses rewritten | a rewrite that holds size *and* count steady | 2% | a stored sketch, so forward-only |

Every threshold sits in a wide empty band measured from the vault's own history:
the largest *legitimate* day-over-day feature change was 0.63%, and the largest
legitimate move in the duplicate-row share was 0.0218%.

Two of these exist because of what the other three could not see. **Duplicate
rows** are invisible to all of them — the sketch below fingerprints the *set* of
addresses, so when brant's nightly rebuild replayed a 1,000-row batch on
2026-08-16 it read 0.0000% churn in both directions, and only the feature count
caught it, and only because 1,000 rows happened to clear 2%. It is compared as a
*move* rather than a level because the level belongs to the source and nothing
else: windsor publishes 14.17% duplicate rows every day, waterloo 0.005%.

**Published fields** is checked before the byte test because a field added to
every row explains a size move that would otherwise be reported as a rewrite:
guelph added one field on 2026-07-24 and was described as 53,796 rows rewritten
in place, which was true of none of them. Naming the field says everything the
percentage did not.

The last test exists because the scalar ones are scalars, and a source can rename
every street to an equal-length name, or re-geocode every point, without moving
either number. Each pull reduces its snapshot to an 8 KB **bottom-k sketch** —
the 1,024 smallest hashes of its addresses — so the report compares 8 KB against
8 KB and recovers the fraction of addresses that changed, without ever reading
two snapshots at once. Reducing a 448 MB snapshot costs about eight seconds,
against a pull that just downloaded it.

What is hashed is the address each source *declares* (its `[fields]` map, plus
the coordinate rounded to ~0.1 m), not the whole feature. Sources republish
churning metadata — brant regenerates every `GLOBALID` and shifts every
`CREATED_DATE` daily without moving a single address — so hashing whole features
would report 100% churn every morning and be worthless.

A snapshot pulled before these columns existed carries none of them, and a day
the vault cannot measure is reported as unmeasured rather than as quiet.
`addressvault backfill` recovers them for any day whose file is still hot; a day
already swept to cold stays unmeasured, since thawing the archive to answer a
report is not a trade worth making.

This is a flag, not a gate. Unlike a refused pull (which records nothing, because
the data provably is not what the source holds), a flagged day **is** what the
source served — it is recorded, and consumers read it normally. The vault raises
the question because it is the only place that can ask it cheaply; it must not
answer it by discarding the day, since a genuine 30% change is exactly what an
archive exists to keep.

### Answering a flag

A flag is a question, and only a person can close it — unlike a refusal, which
clears itself the moment a good pull lands. Until it is answered it sits in the
list forever, so a vault that is never reviewed ends up with a Large-changes list
that is mostly archaeology and gets skimmed.

```
addressvault review <slug> <date> artifact|schema|real -m "what it was"
```

| verdict | means |
| --- | --- |
| `artifact` | what arrived is not true of the world — a source replaying part of its own load, serving damage, or (for the seeding days) the vault's own history. No address changed. |
| `schema` | the source really did change what it publishes, but not the addresses: fields added, removed or reshaped. Real, and still not an address change. |
| `real` | the addresses really did move. |

The note is required, and is the point: a verdict with no reasoning is a dismiss
button, and a list that has been dismissed clean reads as "nothing outstanding"
while being exactly as unexplained as before. When the same city moves the same
way again, that note is what says what it turned out to be last time.

Answering never edits. The bytes, the counts and the fingerprints are untouched
and `data` keeps serving the day; the verdict is recorded alongside as a `jobs`
row. The day keeps its purple cell too — it did move, and that stays true. What
changes is that it leaves the open list and joins **Answered**, and the heading
counts only what nobody has looked at yet.

Only `real` is a story. Deciding what to *do* about a verdict — reimport,
suppress, publish — belongs to the consumer, which is why this is a label and
never a filter.

### Reading the flags from a consumer

`report --json` is the same data without the HTML, for something downstream
asking "can I trust this day?":

```
addressvault report --json [--days N] [--out PATH]
```

```json
{ "generated": "...", "today": "2026-08-19", "from": "2026-07-09",
  "changes":  [ { "slug": "brant", "date": "2026-08-16",
                  "why": "+5.2% features: 19,335 -> 20,335 (since 2026-08-15)",
                  "verdict": "artifact", "note": "...", "reviewed_at": "..." } ],
  "unmeasured": [ { "slug": "...", "date": "..." } ],
  "refused":    [ { "slug": "...", "date": "...", "detail": "..." } ],
  "failed":     [ { "slug": "...", "date": "...", "detail": "..." } ] }
```

`verdict` is `null` where nobody has looked yet, which is deliberately distinct
from a verdict of `real`: one says the question is open, the other says a person
answered it. This matters most to a publication built on the vault — a duplicate
batch republished as "this city gained 1,000 addresses" is a fabricated story,
and a flag nothing downstream can read is a flag that only protects the operator.

## What a pull refuses

Two things can go wrong without anything failing: the transport works, the
response parses, and what came back still is not the layer. Those end the pull
as **refused** — recorded apart from `failed` all the way to the report, because
a human has to decide which it is, and nothing in here can.

| guard | catches | tolerance |
| --- | --- | --- |
| against the layer's own count, short | a pull that stopped early — a blank page mid-stream ends the paging loop, and 102,000 of kitchener's 131,912 addresses read as the whole city | 1% short, since the layer keeps being edited while we page it |
| against the layer's own count, long | rows served twice — paging assumes the OID window holds still across queries, and brant serves a view that renumbers `OBJECTID` 1..N on every pull | 2% over, wider because rows added after the count probe land on this side |
| against rows with no position | a point layer that arrived without its points — muskoka 2026-06-30 served all 66,512 rows with a null geometry | 1% unlocated, since a city can record an address before anybody pins it |

The geometry guard exists because nothing else sees that failure: the rows are
all there, the fields are all there, and the byte count is plausible. That day
was only ever caught after the fact, by an incidental 14.4% drop in file size,
and a source that dropped geometry without shrinking would not even leave that
trace. Both guards sit in wide empty bands — across the snapshots on disk (11.5M
features, every source represented) the most any legitimate pull missed was
0.0026%, one row of huron's 38,315, against a failure at 100%.

A refusal records nothing, deliberately: the data provably is not what the
source holds, so the source stays due and the next run retries it.

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
unusable, so a wrapper script can tell "no network" apart from a real failure —
`pull-due` and `serve` wait it out first, while `pull` reports it immediately,
since blocking a prompt until 22:30 helps nobody.

The sibling tile builders (`toronto-parks-layer`, `toronto-streets-layer`) reuse
`addressvault.net` for the same gate. They run weekly and fail fast rather than
waiting, since their scheduled tasks carry a hard `ExecutionTimeLimit`.

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
