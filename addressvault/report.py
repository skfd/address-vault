"""Static HTML status report: what the vault holds, per city per day.

``addressvault report`` renders one self-contained HTML file from the catalog --
no server, open it in a browser. Four views:

* a city x day matrix (new data / pulled-but-unchanged / failed / no attempt),
* a month calendar of per-day totals (the "did the scheduler run" view),
* storage: real bytes on disk per tier, daily growth, and catalog-vs-disk drift,
* the failure log (durable ``jobs`` rows plus each city's current lease state).

"Failed" for a past day is only as good as the job log: failures are recorded
in ``jobs`` from July 2026 on, so older gaps stay ambiguous "no attempt".

Storage numbers come from stat()ing the vault, never from ``restic stats`` --
the cold figure is therefore the repo's size on disk, not its dedup ratio.
"""

import html
import os
from datetime import date, datetime, timedelta, timezone

from addressvault import archive, config

# Cell states, in legend order.
NEW, UNCHANGED, FAILED, REFUSED, MISSING = \
    "new", "unchanged", "failed", "refused", "missing"


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

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "today": today.isoformat(),
        "dates": dates, "slugs": slugs, "snaps": snaps, "fails": fails,
        "refused": refused, "current": current, "standing": standing,
        "log": log, "stats": cat.stats(),
        "storage": _storage(cat, root, dates),
    }


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
        if s["unchanged_since"]:
            return UNCHANGED, (f"{slug} {d}: pulled, unchanged since "
                               f"{s['unchanged_since']} ({s['features']:,} features)")
        return NEW, (f"{slug} {d}: new data, {s['features']:,} features, "
                     f"{_human_bytes(s['bytes'])} ({tier})")
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


_GLYPH = {FAILED: "&times;", REFUSED: "!"}


def _legend():
    items = [
        (NEW, "new data"), (UNCHANGED, "pulled, unchanged"),
        (FAILED, "failed"), (REFUSED, "refused — needs a look"),
        (MISSING, "no attempt"),
    ]
    cells = "".join(
        f"<span class='li'><span class='cell {s}'>{_GLYPH.get(s, '')}"
        f"</span> {label}</span>" for s, label in items)
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
    tallies = {}
    for d in data["dates"]:
        n = u = f = x = 0
        for slug in data["slugs"]:
            state, _ = _cell(data, slug, d)
            if state == NEW: n += 1
            elif state == UNCHANGED: u += 1
            elif state == FAILED: f += 1
            elif state == REFUSED: x += 1
        tallies[d] = (n, u, f, x, total - n - u - f - x)

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
            n, u, f, x, m = tallies[d]
            tip = (f"{d}: {n} new, {u} unchanged, {f} failed, {x} refused, "
                   f"{m} no attempt (of {n + u + f + x + m})")
            bar = "".join(
                f"<i class='{cls}' style='flex:{v}'></i>"
                for cls, v in ((NEW, n), (UNCHANGED, u), (FAILED, f),
                               (REFUSED, x), (MISSING, m)) if v)
            summary = f"<span class='warnct'>{x}!</span>" if x else \
                      (f"<span class='bad'>{f}&times;</span>" if f else
                       (f"{n + u}&#10003;" if n + u else "&mdash;"))
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
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink2: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --border: rgba(255,255,255,.10);
    --good-wash: rgba(12,163,12,.30);
    --warn: #e2a03f; --warn-wash: rgba(226,160,63,.20);
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
.bar .refused { background: var(--warn); }
.ct { font-size: 10px; color: var(--ink2); font-variant-numeric: tabular-nums; }
.ct .bad { color: var(--bad); font-weight: 600; }
.ct .warnct { color: var(--warn); font-weight: 700; }
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
.fails tr.refused td { background: var(--warn-wash); }
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
