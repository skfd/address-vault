"""Static HTML status report: what the vault holds, per city per day.

``addressvault report`` renders one self-contained HTML file from the catalog --
no server, open it in a browser. Five views:

* a city x day matrix (new data / pulled-but-unchanged / failed / no attempt),
* a month calendar of per-day totals (the "did the scheduler run" view),
* storage: real bytes on disk per tier, daily growth, and catalog-vs-disk drift,
* the days whose content moved far enough that somebody should look,
* the failure log (durable ``jobs`` rows plus each city's current lease state).

"Failed" for a past day is only as good as the job log: failures are recorded
in ``jobs`` from July 2026 on, so older gaps stay ambiguous "no attempt".

Storage numbers come from stat()ing the vault, never from ``restic stats`` --
the cold figure is therefore the repo's size on disk, not its dedup ratio.
"""

import html
import os
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from addressvault import archive, config, sketch

# Cell states, in legend order.
NEW, SPIKED, UNCHANGED, FAILED, REFUSED, MISSING = \
    "new", "spiked", "unchanged", "failed", "refused", "missing"

# How far a day's content may move from the previous snapshot before the report
# marks it and asks for a person. Calibrated against the vault's own history
# (1,905 snapshots to 2026-08-10): the largest legitimate day-over-day feature
# change was 0.63%, and nothing at all landed between 1% and 50%, so there is a
# wide empty band to put the line in.
SPIKE_FEATURES = 0.02
# Bytes moving while the row count holds still is the other shape of the same
# question -- a source rewriting rows in place rather than adding or dropping
# them (muskoka 2026-07-01 grew 16.8% on a 0.000% feature change). Compared as a
# divergence from the feature change, so ordinary growth, where the two move
# together, stays quiet.
SPIKE_DIVERGENCE = 0.02
# The share of a city's addresses that may be rewritten in a day before it wants
# a person -- the churn a rewrite holding both count *and* size steady would
# otherwise hide. Held at the same 2% as the other two so the three tests draw
# one line; the sketch reads a true 2% as ~1.7%, so this fires a shade late
# rather than early, which is the right way round for a flag nobody can dismiss.
SPIKE_CHURN = 0.02
# How far the share of rows that are duplicates of another row may move in a day.
# Duplication is invisible to all three tests above: the sketch fingerprints the
# *set* of addresses, so it read brant 2026-08-16 -- 1,000 rows replayed by the
# source's nightly rebuild -- as 0.0000% churn in both directions, and only the
# feature count caught it, and only because 1,000 rows happened to clear 2%.
#
# Compared as a move rather than a level, because the level is a property of the
# source and nothing else: windsor publishes 14.17% duplicate rows every day,
# waterloo 0.005%. The move is what is stable -- across the snapshots on disk the
# largest legitimate day-over-day change was 0.0218% (kingston), against 4.92%
# for the brant event, so the line sits in a wide empty band an order of
# magnitude either side.
SPIKE_DUPLICATES = 0.005


def build(vault, days=42, out=None, today=None):
    """Write the report and return its path."""
    data = _collect(vault.cat, vault.root, days, today or date.today())
    out = out or os.path.join(vault.root, "report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(_render(data))
    return out


# --- data ---

def _collect(cat, root, days, today):
    frm = (today - timedelta(days=days - 1)).isoformat()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    slugs = [r["slug"] for r in cat.list_sources()]

    snaps = {}  # (slug, date) -> row
    for r in cat.conn.execute("SELECT * FROM snapshots WHERE date>=?", (frm,)):
        snaps[(r["slug"], r["date"])] = r

    # A refused pull is tracked apart from a failed one the whole way through:
    # a failure is the source being unreachable and fixes itself, a refusal is
    # data we threw away and is waiting on somebody to look at it.
    fails = {}    # (slug, date) -> detail (latest failed pull job for that day)
    refused = {}  # (slug, date) -> detail, same for pulls the guards threw out
    for r in cat.conn.execute(
        "SELECT slug, date, state, detail FROM jobs WHERE kind='pull' "
        "AND state IN ('failed','refused') AND date>=? ORDER BY created_at", (frm,)
    ):
        bucket = refused if r["state"] == "refused" else fails
        bucket[(r["slug"], r["date"])] = r["detail"] or ""

    # A currently-failed lease is the freshest error we have (and, before the
    # job log started recording failures, the only one). Only ``failed``: a
    # ``deferred`` lease means the link died under the pull, which is a "no
    # attempt" day, not a failure to hold against the source.
    current = []   # (slug, date, detail, updated_at) — last pull errored
    standing = []  # ...and the subset still refusing, which needs a person
    for r in cat.conn.execute(
        "SELECT slug, date, state, detail, updated_at FROM leases "
        "WHERE state IN ('failed','refused') ORDER BY updated_at DESC"
    ):
        row = (r["slug"], r["date"], r["detail"] or "", r["updated_at"])
        if r["state"] == "refused":
            standing.append(row)
            if r["date"] and r["date"] >= frm:
                refused.setdefault((r["slug"], r["date"]), r["detail"] or "")
        else:
            current.append(row)
            if r["date"] and r["date"] >= frm:
                fails.setdefault((r["slug"], r["date"]), r["detail"] or "")

    log = cat.conn.execute(
        "SELECT slug, date, state, detail, created_at FROM jobs "
        "WHERE kind='pull' AND state IN ('failed','refused') "
        "ORDER BY created_at DESC LIMIT 100"
    ).fetchall()

    spikes, unmeasured = _churn(cat, frm)

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "today": today.isoformat(),
        "dates": dates, "slugs": slugs, "snaps": snaps, "fails": fails,
        "refused": refused, "current": current, "standing": standing,
        "log": log, "stats": cat.stats(), "spikes": spikes,
        "reviews": cat.reviews(),
        "unmeasured": unmeasured, "storage": _storage(cat, root, dates),
    }


def _spike(p, r):
    """``(why, blind)`` for day ``r`` against the previous snapshot ``p``.

    ``why`` says how far the content moved, or is None if it moved no more than
    a city normally does in a day. ``blind`` says the day cleared both scalar
    tests but carries no sketch to run the third against, so its quiet is only
    "nothing we could see moved" -- reported separately rather than counted as
    a clean day.

    Shapes of one question, cheapest and most explanatory first. The feature
    count moving is a city gaining or losing addresses wholesale. Rows that are
    duplicates of another row appearing en masse is a source replaying part of
    its own load. A change to the published field list explains a size move that
    would otherwise read as a rewrite, and is checked before the byte test for
    exactly that reason -- guelph 2026-07-24 added one field to every row and
    was reported as 53,796 rows rewritten in place, which was not true of a
    single one of them. The byte count moving while the feature count holds
    still is what remains after that: a source rewriting rows in place, measured
    as the gap between the two, so ordinary growth -- where bytes and rows move
    together -- says nothing. All of those are scalars, and a rewrite that holds
    size as well as count slips past every one: the sketch comparison sees that
    case, and is checked last because it is the only test needing a stored
    fingerprint and so is blind to days pulled before sketching existed.
    """
    if p["sha256"] and r["sha256"] == p["sha256"]:
        # Byte-identical to the day before: nothing moved, and no fingerprint is
        # needed to say so. Worth short-circuiting, or every dedup day reaching
        # back past sketching would be reported unmeasured when it is provably
        # unchanged.
        return None, False
    if not p["features"]:
        # No baseline to be relative to: the previous day recorded nothing. Any
        # content at all is a mass change against an empty snapshot.
        if not r["features"]:
            return None, False  # empty then, empty now -- nothing moved
        return f"{r['features']:,} features, up from an empty snapshot ({p['date']})", False
    df = (r["features"] - p["features"]) / p["features"]
    if abs(df) >= SPIKE_FEATURES:
        return (f"{df:+.1%} features: {p['features']:,} -> {r['features']:,} "
                f"(since {p['date']})"), False
    if p["distinct_addresses"] and r["distinct_addresses"]:
        # Duplicate rows: rows the source published twice. A level here is
        # normal and source-specific; a *move* is the source replaying a batch.
        dup_p = (p["features"] - p["distinct_addresses"]) / p["features"]
        dup_r = (r["features"] - r["distinct_addresses"]) / r["features"]
        if abs(dup_r - dup_p) >= SPIKE_DUPLICATES:
            return (f"{dup_r - dup_p:+.1%} duplicate rows: "
                    f"{p['features'] - p['distinct_addresses']:,} -> "
                    f"{r['features'] - r['distinct_addresses']:,} rows repeat an "
                    f"address already in the file (since {p['date']})"), False
    if p["field_names"] and r["field_names"]:
        # A schema change, not a data change. Named rather than measured: the
        # size move it causes says nothing a consumer can act on, and the field
        # names say everything.
        was = set(p["field_names"].split("\n"))
        now = set(r["field_names"].split("\n"))
        if was != now:
            moved = [f"+{f}" for f in sorted(now - was)]
            moved += [f"-{f}" for f in sorted(was - now)]
            return (f"published fields changed: {', '.join(moved)} "
                    f"(since {p['date']})"), False
    if p["bytes"]:
        db = (r["bytes"] - p["bytes"]) / p["bytes"]
        if abs(db - df) >= SPIKE_DIVERGENCE:
            return (f"{db:+.1%} bytes on {df:+.1%} features: rows rewritten in "
                    f"place, not added or dropped (since {p['date']})"), False
    if not p["sketch"] or not r["sketch"]:
        return None, True  # the content changed and we cannot say by how much
    rewritten = sketch.churn(sketch.decode(p["sketch"]), sketch.decode(r["sketch"]))
    if rewritten >= SPIKE_CHURN:
        return (f"{rewritten:.1%} of addresses rewritten on {df:+.1%} features "
                f"and no size change to show for it (since {p['date']})"), False
    return None, False


def _churn(cat, frm):
    """``(spikes, unmeasured)``: which days moved far enough to want a person,
    and which ones the vault could not finish asking about.

    ``spikes`` maps (slug, date) -> why. ``unmeasured`` is the set of (slug,
    date) whose content changed but whose rewrite share could not be measured,
    because one side of the comparison predates sketching. They are held apart
    from the quiet days rather than folded in with them: a report that cannot
    tell "checked, nothing moved" from "never checked" is worth less than one
    that admits the difference. A day with no predecessor at all is not in here
    -- no test can run without a baseline, and the matrix already shows that.

    Deliberately not a pull-time guard. A ``DegradedPull`` refusal records
    nothing because the data provably is not what the source holds; a day in
    here *is* what the source served, so it is recorded and consumers can read
    it normally. The vault raises the question because it is the only place that
    can ask it cheaply -- it already holds every prior day -- but it must not
    answer it by discarding the day, since a genuine 30% change is exactly what
    an archive exists to keep.

    Each day is compared against the snapshot before it, not against the same
    date last week: dedup days carry the canonical day's ``features``, ``bytes``
    and ``sketch`` forward, so consecutive rows differ only where the content
    actually did.
    """
    rows = cat.conn.execute(
        # Every row in the window, plus the one immediately before it per slug
        # -- the first day shown needs a baseline too.
        "SELECT slug, date, sha256, features, bytes, sketch, "
        "distinct_addresses, field_names FROM snapshots "
        "WHERE date>=? OR "
        "date=(SELECT MAX(date) FROM snapshots p WHERE p.slug=snapshots.slug "
        "AND p.date<?) ORDER BY slug, date", (frm, frm)
    ).fetchall()

    spikes, unmeasured, prev = {}, set(), {}
    for r in rows:
        p = prev.get(r["slug"])
        prev[r["slug"]] = r
        if p is None or r["date"] < frm:
            continue  # the pre-window baseline is a yardstick, not a finding
        why, blind = _spike(p, r)
        if why:
            spikes[(r["slug"], r["date"])] = why
        elif blind:
            unmeasured.add((r["slug"], r["date"]))
    return spikes, unmeasured


def _storage(cat, root, dates):
    """What the vault actually occupies, and where the catalog disagrees.

    One walk of the vault root does double duty: it totals bytes per tier and
    diffs the files it finds against the snapshots the catalog calls hot.
    """
    repo = archive._repo(root)  # env override, else <root>/restic
    expected = {}  # filename -> snapshot row the catalog believes is on disk
    for r in cat.conn.execute(
        "SELECT slug, date, bytes, restored_until FROM snapshots WHERE on_disk=1"
    ):
        expected[config.snapshot_name(r["slug"], r["date"])] = r

    tiers = {"hot": 0, "cold": _dir_bytes(repo), "catalog": 0, "other": 0}
    seen, warn = {}, []
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.abspath(dirpath) == os.path.abspath(repo):
            dirnames[:] = []  # already totalled above (and may live outside root)
            continue
        for name in filenames:
            size = _size(os.path.join(dirpath, name))
            if size is None:
                continue
            if dirpath == root and name.endswith(".geojson"):
                tiers["hot"] += size
                seen[name] = size
            elif dirpath == root and name.startswith(config.DB_NAME):
                tiers["catalog"] += size  # catalog.db plus its -wal/-shm
            else:
                tiers["other"] += size

    for name, row in sorted(expected.items()):
        if name not in seen:
            warn.append(f"{name}: catalog says on_disk, file is missing")
        elif seen[name] != row["bytes"]:
            warn.append(f"{name}: {_human_bytes(seen[name])} on disk, catalog "
                        f"recorded {_human_bytes(row['bytes'])}")
    for name in sorted(set(seen) - set(expected)):
        warn.append(f"{name}: on disk but not a hot snapshot in the catalog")
    stale = cat.conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE on_disk=1 AND restored_until IS NOT NULL "
        # same UTC-iso clock recool_expired() compares against
        "AND restored_until<=?", (datetime.now(timezone.utc).isoformat(),)
    ).fetchone()[0]
    if stale:
        warn.append(f"{stale} thawed snapshot(s) past their TTL still hot -- "
                    f"recool has not run")

    # Growth = bytes of genuinely new content per day; unchanged days reuse a
    # file that already exists, so they cost nothing.
    added = dict.fromkeys(dates, 0)
    for r in cat.conn.execute(
        "SELECT date, COALESCE(SUM(bytes),0) AS b FROM snapshots "
        "WHERE unchanged_since IS NULL AND date>=? GROUP BY date", (dates[0],)
    ):
        if r["date"] in added:
            added[r["date"]] = r["b"]

    total_added = sum(added.values())
    return {
        "tiers": tiers, "total": sum(tiers.values()), "warn": warn,
        "added": added, "added_total": total_added,
        "per_year": total_added / len(dates) * 365,
    }


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:  # vanished mid-walk, or unreadable
        return None


def _dir_bytes(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            total += _size(os.path.join(dirpath, name)) or 0
    return total


def _cell(data, slug, d):
    """(state, tooltip) for one matrix cell."""
    s = data["snaps"].get((slug, d))
    if s is not None:
        tier = ("hot" if s["on_disk"] else "") + ("+cold" if s["archived"] else "")
        tier = tier.lstrip("+") or "recorded"
        why = data["spikes"].get((slug, d))
        if why:
            # The cell stays purple once answered: the day did move, and that
            # stays true. What the answer changes is whether anybody still owes
            # it a look, which the tooltip and the section below now say.
            rv = data["reviews"].get((slug, d))
            verdict = (f" ANSWERED ({rv['state']}): {rv['detail']}." if rv
                       else " Not yet looked at.")
            return SPIKED, (f"{slug} {d}: LARGE CHANGE, recorded and readable -- "
                            f"{why}.{verdict} {_human_bytes(s['bytes'])} ({tier})")
        # Said on the cell as well as in the section below, because this is
        # where somebody asks what a green square means.
        blind = (" -- rewrites unchecked, no sketch on one side"
                 if (slug, d) in data["unmeasured"] else "")
        if s["unchanged_since"]:
            return UNCHANGED, (f"{slug} {d}: pulled, unchanged since "
                               f"{s['unchanged_since']} ({s['features']:,} features)"
                               f"{blind}")
        return NEW, (f"{slug} {d}: new data, {s['features']:,} features, "
                     f"{_human_bytes(s['bytes'])} ({tier}){blind}")
    detail = data["refused"].get((slug, d))
    if detail is not None:
        return REFUSED, f"{slug} {d}: pull REFUSED, nothing recorded -- {detail[:200]}"
    detail = data["fails"].get((slug, d))
    if detail is not None:
        return FAILED, f"{slug} {d}: pull FAILED -- {detail[:200]}"
    return MISSING, f"{slug} {d}: no pull recorded"


def _human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def to_json(data):
    """The report as data, for a consumer rather than an operator.

    ``report --json`` is how something downstream asks "can I trust this day?".
    Until this existed the answer lived only in rendered HTML, so a consumer had
    no way to reach it -- and the whole point of flagging a day is lost if the
    thing that would publish it cannot see the flag.

    ``verdict`` is null where nobody has looked yet. That is deliberately
    distinct from a verdict of "real": one says the question is open, the other
    says a person answered it. What a consumer does with either is its own
    business; this reports, it does not prescribe.
    """
    changes = []
    for (slug, d), why in sorted(data["spikes"].items()):
        rv = data["reviews"].get((slug, d))
        changes.append({
            "slug": slug, "date": d, "why": why,
            "verdict": rv["state"] if rv else None,
            "note": rv["detail"] if rv else None,
            "reviewed_at": rv["created_at"] if rv else None,
        })
    return {
        "generated": data["generated"],
        "today": data["today"],
        "from": data["dates"][0],
        "changes": changes,
        "unmeasured": [{"slug": s, "date": d} for s, d in sorted(data["unmeasured"])],
        "refused": [{"slug": s, "date": d, "detail": v}
                    for (s, d), v in sorted(data["refused"].items())],
        "failed": [{"slug": s, "date": d, "detail": v}
                   for (s, d), v in sorted(data["fails"].items())],
    }


# --- rendering ---

def _render(data):
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>address-vault status</title>", _CSS, "</head><body>",
        "<header><h1>address-vault</h1>",
        f"<p class='sub'>generated {data['generated']} &middot; "
        f"last {len(data['dates'])} days</p></header>",
        _kpis(data["stats"]),
        _legend(),
        "<h2>Pulls by city and day</h2>", _matrix(data),
        "<h2>Daily totals</h2>", _calendar(data),
        "<h2>Storage</h2>", _storage_section(data),
        f"<h2>Large changes ({_open_count(data)})</h2>", _spikes(data),
        "<h2>Failures</h2>", _failures(data),
        "<div id='tip' hidden></div>", _JS, "</body></html>",
    ]
    return "".join(parts)


def _kpis(st):
    tiles = [
        ("sources", f"{st['sources']}"),
        ("snapshots", f"{st['snapshots']:,}"),
        ("hot", f"{st['hot']:,}"),
        ("cold", f"{st['cold']:,}"),
        ("hot size", _human_bytes(st["hot_bytes"])),
    ]
    cells = "".join(
        f"<div class='tile'><div class='v'>{v}</div><div class='k'>{k}</div></div>"
        for k, v in tiles)
    return f"<div class='kpis'>{cells}</div>"


_GLYPH = {FAILED: "&times;", REFUSED: "!", SPIKED: "&#916;"}

# Every state, in display order, with the terse label the calendar tooltip uses.
# Legend, calendar bar, and tooltip all read their order from here so they
# cannot drift apart as states are added.
_STATES = [(NEW, "new"), (SPIKED, "large change"), (UNCHANGED, "unchanged"),
           (FAILED, "failed"), (REFUSED, "refused"), (MISSING, "no attempt")]

_LEGEND = {NEW: "new data", SPIKED: "large change — worth a look",
           UNCHANGED: "pulled, unchanged", FAILED: "failed",
           REFUSED: "refused — needs a look", MISSING: "no attempt"}


def _legend():
    cells = "".join(
        f"<span class='li'><span class='cell {s}'>{_GLYPH.get(s, '')}"
        f"</span> {_LEGEND[s]}</span>" for s, _ in _STATES)
    return f"<div class='legend'>{cells}</div>"


def _matrix(data):
    dates = data["dates"]
    # Header: month label on the 1st (or the first column), day-of-month per column.
    mons, days_r = [], []
    for i, d in enumerate(dates):
        dd = d[8:10]
        mons.append(f"<th class='mon'>{d[:7][5:]}/{d[:4][2:]}</th>"
                    if i == 0 or dd == "01" else "<th></th>")
        cls = " class='today'" if d == data["today"] else ""
        days_r.append(f"<th{cls}>{int(dd)}</th>")
    rows = [f"<tr><th class='slug'></th>{''.join(mons)}</tr>",
            f"<tr><th class='slug'></th>{''.join(days_r)}</tr>"]
    for slug in data["slugs"]:
        tds = []
        for d in dates:
            state, tip = _cell(data, slug, d)
            tds.append(f"<td><span class='cell {state}' data-tip=\"{html.escape(tip)}\">"
                       f"{_GLYPH.get(state, '')}</span></td>")
        rows.append(f"<tr><th class='slug'>{html.escape(slug)}</th>{''.join(tds)}</tr>")
    return f"<div class='scroll'><table class='matrix'>{''.join(rows)}</table></div>"


def _calendar(data):
    total = len(data["slugs"])
    # Per-day tallies across all cities.
    tallies = {d: Counter(_cell(data, slug, d)[0] for slug in data["slugs"])
               for d in data["dates"]}

    months = {}  # "YYYY-MM" -> [iso dates]
    for d in data["dates"]:
        months.setdefault(d[:7], []).append(d)

    out = ["<div class='cals'>"]
    for mon, ds in months.items():
        first = date.fromisoformat(ds[0])
        out.append(f"<div class='cal'><div class='calh'>{first.strftime('%B %Y')}</div>")
        out.append("<div class='calgrid'>")
        out.extend(f"<div class='dow'>{w}</div>" for w in
                   ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"))
        out.extend("<div></div>" * first.weekday())  # pad to the first date shown
        for d in ds:
            t = tallies[d]
            tip = (f"{d}: " + ", ".join(f"{t[s]} {label}" for s, label in _STATES)
                   + f" (of {total})")
            bar = "".join(f"<i class='{s}' style='flex:{t[s]}'></i>"
                          for s, _ in _STATES if t[s])
            # Worst-first: whatever most wants a person is what the cell says.
            if t[REFUSED]:
                summary = f"<span class='warnct'>{t[REFUSED]}!</span>"
            elif t[FAILED]:
                summary = f"<span class='bad'>{t[FAILED]}&times;</span>"
            elif t[SPIKED]:
                summary = f"<span class='spikect'>{t[SPIKED]}&#916;</span>"
            elif t[NEW] + t[UNCHANGED]:
                summary = f"{t[NEW] + t[UNCHANGED]}&#10003;"
            else:
                summary = "&mdash;"
            today = " today" if d == data["today"] else ""
            out.append(
                f"<div class='day{today}' data-tip='{html.escape(tip)}'>"
                f"<span class='dn'>{int(d[8:10])}</span>"
                f"<span class='bar'>{bar}</span><span class='ct'>{summary}</span></div>")
        out.append("</div></div>")
    out.append("</div>")
    return "".join(out)


_TIERS = [("hot", "hot files"), ("cold", "restic repo"),
          ("catalog", "catalog"), ("other", "other")]


def _storage_section(data):
    st = data["storage"]
    total = st["total"]
    out = [f"<p class='sub'>{_human_bytes(total)} on disk under the vault root.</p>",
           "<div class='tiers'>"]
    for key, label in _TIERS:
        v = st["tiers"][key]
        pct = (v / total * 100) if total else 0
        out.append(f"<i class='{key}' style='flex:{v or 0.0001}' "
                   f"data-tip='{label}: {_human_bytes(v)} ({pct:.1f}%)'></i>")
    out.append("</div><div class='tierkeys'>")
    for key, label in _TIERS:
        out.append(f"<span class='li'><i class='sw {key}'></i> {label} "
                   f"<b>{_human_bytes(st['tiers'][key])}</b></span>")
    out.append("</div>")
    out.append("<h3>New bytes per day</h3>")
    out.append(_growth(st))
    out.append(f"<p class='sub'>{_human_bytes(st['added_total'])} of new content over "
               f"{len(st['added'])} days &middot; ~{_human_bytes(st['per_year'])}/year "
               f"at that rate.</p>")

    out.append(f"<h3>Catalog vs disk ({len(st['warn'])})</h3>")
    if st["warn"]:
        out.append("<ul class='warn'>")
        out.extend(f"<li>{html.escape(w)}</li>" for w in st["warn"])
        out.append("</ul>")
    else:
        out.append("<p class='sub'>Every hot snapshot is on disk at its recorded "
                   "size, and nothing unaccounted-for is.</p>")
    return "".join(out)


def _growth(st):
    peak = max(st["added"].values()) or 1
    bars = []
    for d, v in st["added"].items():
        tip = f"{d}: {_human_bytes(v)} new" if v else f"{d}: nothing new"
        bars.append(f"<i style='height:{max(v / peak * 100, 1.5):.1f}%' "
                    f"class='{'z' if not v else ''}' data-tip='{tip}'></i>")
    return (f"<div class='growth'><div class='gbars'>{''.join(bars)}</div>"
            f"<div class='gaxis'><span>{list(st['added'])[0]}</span>"
            f"<span>peak {_human_bytes(peak)}</span>"
            f"<span>{list(st['added'])[-1]}</span></div></div>")


def _unmeasured(data):
    """How much of the window the rewrite test could not cover.

    Only the third test can go unrun, and only where a snapshot predates
    sketching, so this shrinks to nothing a couple of pulls after a city joins.
    While it is not nothing, it has to be said: the other two tests reading
    quiet is not the same as the day being clean.
    """
    n = data["unmeasured"]
    if not n:
        return ""
    cities = len({slug for slug, _ in n})
    return (f"<p class='sub'>Rewrites unchecked on {len(n)} changed city-day"
            f"{'s' if len(n) != 1 else ''} "
            f"({cities} of {len(data['slugs'])} cities): one side of "
            f"the comparison predates sketching, so those days are unmeasured "
            f"rather than known quiet. The feature-count and byte tests still "
            f"covered them. Hover a cell to see which.</p>")


def _open_count(data):
    """Flagged days nobody has answered yet -- the heading is a to-do count."""
    return sum(1 for k in data["spikes"] if k not in data["reviews"])


def _spikes(data):
    open_, done = [], []
    for (slug, d), why in sorted(data["spikes"].items(),
                                 key=lambda kv: kv[0][1], reverse=True):
        (done if (slug, d) in data["reviews"] else open_).append((slug, d, why))

    if not data["spikes"]:
        return (f"<p class='sub'>No city's content moved by more than "
                f"{SPIKE_FEATURES:.0%} in a day over this window.</p>"
                + _unmeasured(data))

    out = []
    if open_:
        out.append("<p class='sub'>These days are recorded and readable -- unlike a "
                   "refusal, nothing was thrown away, and consumers will pull them "
                   "normally. They are listed because the content moved far enough "
                   "that it is worth deciding whether the source published something "
                   "wrong, or whether it really did change this much and consumers "
                   "need reimporting. Answer one with "
                   "<code>addressvault review &lt;city&gt; &lt;day&gt; "
                   "&lt;verdict&gt; -m &quot;what it was&quot;</code>.</p>")
        out.append("<table class='fails spikes'><tr><th>city</th><th>day</th>"
                   "<th>what moved</th></tr>")
        for slug, d, why in open_:
            out.append(f"<tr><td>{html.escape(slug)}</td><td>{html.escape(d)}</td>"
                       f"<td class='err'>{html.escape(why)}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p class='sub'>Every large change in this window has been "
                   "looked at.</p>")

    if done:
        # Kept, not hidden: the day did move, and the answer is worth more than
        # the question was. When the same city moves the same way again, this is
        # the row that says what it turned out to be last time.
        out.append(f"<h3>Answered ({len(done)})</h3>")
        out.append("<table class='fails spikes'><tr><th>city</th><th>day</th>"
                   "<th>verdict</th><th>what it was</th></tr>")
        for slug, d, _why in done:
            rv = data["reviews"][(slug, d)]
            out.append(f"<tr><td>{html.escape(slug)}</td><td>{html.escape(d)}</td>"
                       f"<td><span class='verdict {html.escape(rv['state'])}'>"
                       f"{html.escape(rv['state'])}</span></td>"
                       f"<td>{html.escape(rv['detail'] or '')}</td></tr>")
        out.append("</table>")

    out.append(_unmeasured(data))
    return "".join(out)


def _failures(data):
    out = []
    # First, because it is the only part of this section that needs a person:
    # nothing here clears itself the way an unreachable source does.
    if data["standing"]:
        out.append(f"<h3 class='needs'>Refused — needs a look "
                   f"({len(data['standing'])})</h3>")
        out.append("<p class='sub'>The source answered and the data was thrown away. "
                   "Either it is serving damage, or it really did change this much. "
                   "Nothing is recorded for these days until one or the other is "
                   "settled; a pull that passes clears the state by itself.</p>")
        out.append("<table class='fails refused'><tr><th>city</th><th>day</th>"
                   "<th>when</th><th>reason</th></tr>")
        for slug, d, detail, at in data["standing"]:
            out.append(f"<tr><td>{html.escape(slug)}</td><td>{html.escape(d or '')}</td>"
                       f"<td>{html.escape((at or '')[:16])}</td>"
                       f"<td class='err'>{html.escape(detail)}</td></tr>")
        out.append("</table>")
    if data["current"]:
        out.append(f"<h3>Cities whose last pull failed ({len(data['current'])})</h3>")
        out.append("<table class='fails'><tr><th>city</th><th>day</th>"
                   "<th>when</th><th>error</th></tr>")
        for slug, d, detail, at in data["current"]:
            out.append(f"<tr><td>{html.escape(slug)}</td><td>{html.escape(d or '')}</td>"
                       f"<td>{html.escape((at or '')[:16])}</td>"
                       f"<td class='err'>{html.escape(detail)}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p class='sub'>No city is currently in a failed state.</p>")
    out.append("<h3>Failure log</h3>")
    if data["log"]:
        out.append("<table class='fails'><tr><th>city</th><th>day</th>"
                   "<th>when</th><th>what</th><th>error</th></tr>")
        for r in data["log"]:
            refused = r["state"] == "refused"
            out.append(f"<tr{' class=refused' if refused else ''}>"
                       f"<td>{html.escape(r['slug'])}</td>"
                       f"<td>{html.escape(r['date'] or '')}</td>"
                       f"<td>{html.escape((r['created_at'] or '')[:16])}</td>"
                       f"<td>{'refused' if refused else 'failed'}</td>"
                       f"<td class='err'>{html.escape(r['detail'] or '')}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p class='sub'>Empty -- failures are logged from July 2026 on; "
                   "before that only each city's most recent error survives (above).</p>")
    return "".join(out)


_CSS = """<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink2: #52514e;
  --muted: #898781; --grid: #e1e0d9; --border: rgba(11,11,11,.10);
  --good: #0ca30c; --good-wash: rgba(12,163,12,.22); --bad: #d03b3b;
  /* "refused" is not a worse "failed", it is a different thing: amber reads as
     attention-wanted next to red's something-broke. */
  --warn: #c07800; --warn-wash: rgba(192,120,0,.16);
  /* "spiked" is a third thing again -- the data is good and consumable, it just
     moved a lot. Violet keeps it off the red/amber "something is wrong" scale
     while still standing out from a wall of green. */
  --spike: #7b5cd6; --spike-wash: rgba(123,92,214,.16);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink2: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --border: rgba(255,255,255,.10);
    --good-wash: rgba(12,163,12,.30);
    --warn: #e2a03f; --warn-wash: rgba(226,160,63,.20);
    --spike: #a68cf0; --spike-wash: rgba(166,140,240,.20);
  }
}
* { box-sizing: border-box; }
body { margin: 0 auto; padding: 24px; max-width: 1100px; background: var(--page);
  color: var(--ink); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { font-size: 20px; margin: 0; } h2 { font-size: 16px; margin: 28px 0 10px; }
h3 { font-size: 13.5px; margin: 16px 0 6px; color: var(--ink2); }
.sub { color: var(--muted); margin: 2px 0 0; font-size: 12.5px; }
.kpis { display: flex; gap: 10px; flex-wrap: wrap; margin: 18px 0 6px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 16px; min-width: 96px; }
.tile .v { font-size: 22px; font-weight: 600; }
.tile .k { font-size: 11.5px; color: var(--muted); }
.legend { display: flex; gap: 18px; margin: 10px 0 4px; font-size: 12.5px;
  color: var(--ink2); flex-wrap: wrap; }
.li { display: inline-flex; align-items: center; gap: 6px; }
.cell { display: inline-flex; width: 15px; height: 15px; border-radius: 4px;
  align-items: center; justify-content: center; font-size: 11px; line-height: 1; }
.cell.new { background: var(--good); }
.cell.unchanged { background: var(--good-wash); border: 1px solid var(--good); }
.cell.failed { background: var(--bad); color: #fff; font-weight: 700; }
.cell.refused { background: var(--warn); color: #fff; font-weight: 700; }
.cell.spiked { background: var(--spike); color: #fff; font-weight: 700; }
.cell.missing { border: 1px solid var(--grid); }
.scroll { overflow-x: auto; background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px; }
.matrix { border-collapse: collapse; }
.matrix th { font-weight: 400; color: var(--muted); font-size: 10.5px; padding: 0 1px;
  text-align: center; font-variant-numeric: tabular-nums; }
.matrix th.mon { text-align: left; color: var(--ink2); font-weight: 600; }
.matrix th.today { color: var(--ink); font-weight: 700; }
.matrix th.slug { text-align: right; padding-right: 8px; font-size: 12px;
  color: var(--ink2); white-space: nowrap; position: sticky; left: 0;
  background: var(--surface); }
.matrix td { padding: 1px; }
.cals { display: flex; gap: 16px; flex-wrap: wrap; }
.cal { background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px; }
.calh { font-size: 13px; font-weight: 600; margin-bottom: 8px; }
.calgrid { display: grid; grid-template-columns: repeat(7, 44px); gap: 3px; }
.dow { font-size: 10px; color: var(--muted); text-align: center; }
.day { border: 1px solid var(--grid); border-radius: 5px; padding: 3px 4px;
  min-height: 44px; display: flex; flex-direction: column; gap: 2px; }
.day.today { border-color: var(--ink2); }
.dn { font-size: 10px; color: var(--muted); }
.bar { display: flex; height: 5px; border-radius: 2px; overflow: hidden; gap: 1px; }
.bar i { display: block; }
.bar .new { background: var(--good); } .bar .unchanged { background: var(--good-wash); }
.bar .failed { background: var(--bad); } .bar .missing { background: var(--grid); }
.bar .refused { background: var(--warn); } .bar .spiked { background: var(--spike); }
.ct { font-size: 10px; color: var(--ink2); font-variant-numeric: tabular-nums; }
.ct .bad { color: var(--bad); font-weight: 600; }
.ct .warnct { color: var(--warn); font-weight: 700; }
.ct .spikect { color: var(--spike); font-weight: 700; }
.tiers { display: flex; height: 22px; gap: 2px; border-radius: 6px; overflow: hidden;
  background: var(--surface); border: 1px solid var(--border); }
.tiers i { display: block; }
.tierkeys { display: flex; gap: 18px; flex-wrap: wrap; margin: 8px 0 0;
  font-size: 12.5px; color: var(--ink2); }
.tierkeys b { color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; }
.sw { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }
.hot { background: var(--good); } .cold { background: #4b8fd6; }
.catalog { background: var(--muted); } .other { background: var(--grid); }
.growth { background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px; }
.gbars { display: flex; align-items: flex-end; gap: 2px; height: 90px; }
.gbars i { flex: 1; background: var(--good); border-radius: 2px 2px 0 0; min-width: 3px; }
.gbars i.z { background: var(--grid); }
.gaxis { display: flex; justify-content: space-between; margin-top: 5px;
  font-size: 10.5px; color: var(--muted); }
.warn { margin: 6px 0 0; padding-left: 18px; font-size: 12.5px; color: var(--ink2); }
.warn li { margin-bottom: 2px; }
.fails { border-collapse: collapse; width: 100%; background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px; font-size: 12.5px; }
.fails th { text-align: left; color: var(--muted); font-weight: 500; }
.fails th, .fails td { padding: 5px 10px; border-bottom: 1px solid var(--grid);
  vertical-align: top; }
.fails td.err { font-family: ui-monospace, Consolas, monospace; font-size: 11.5px;
  color: var(--ink2); word-break: break-all; }
h3.needs { color: var(--warn); }
.fails.refused { border-color: var(--warn); border-left: 3px solid var(--warn); }
.verdict { font-weight: 600; font-size: 11.5px; padding: 1px 6px; border-radius: 3px;
  background: var(--spike-wash); color: var(--spike); white-space: nowrap; }
.verdict.real { background: var(--good-wash); color: var(--good); }
.fails tr.refused td { background: var(--warn-wash); }
.fails.spikes { border-left: 3px solid var(--spike); }
#tip { position: fixed; z-index: 9; background: var(--ink); color: var(--page);
  padding: 5px 9px; border-radius: 6px; font-size: 12px; max-width: 380px;
  pointer-events: none; }
</style>"""

_JS = """<script>
const tip = document.getElementById('tip');
document.addEventListener('mouseover', e => {
  const t = e.target.closest('[data-tip]');
  if (!t) { tip.hidden = true; return; }
  tip.textContent = t.dataset.tip; tip.hidden = false;
});
document.addEventListener('mousemove', e => {
  if (tip.hidden) return;
  const x = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8);
  const y = Math.min(e.clientY + 14, innerHeight - tip.offsetHeight - 8);
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
});
</script>"""
