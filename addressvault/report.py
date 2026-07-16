"""Static HTML status report: what the vault holds, per city per day.

``addressvault report`` renders one self-contained HTML file from the catalog --
no server, open it in a browser. Three views:

* a city x day matrix (new data / pulled-but-unchanged / failed / no attempt),
* a month calendar of per-day totals (the "did the scheduler run" view),
* the failure log (durable ``jobs`` rows plus each city's current lease state).

"Failed" for a past day is only as good as the job log: failures are recorded
in ``jobs`` from July 2026 on, so older gaps stay ambiguous "no attempt".
"""

import html
import os
from datetime import date, datetime, timedelta

# Cell states, in legend order.
NEW, UNCHANGED, FAILED, MISSING = "new", "unchanged", "failed", "missing"


def build(vault, days=42, out=None, today=None):
    """Write the report and return its path."""
    data = _collect(vault.cat, days, today or date.today())
    out = out or os.path.join(vault.root, "report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(_render(data))
    return out


# --- data ---

def _collect(cat, days, today):
    frm = (today - timedelta(days=days - 1)).isoformat()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    slugs = [r["slug"] for r in cat.list_sources()]

    snaps = {}  # (slug, date) -> row
    for r in cat.conn.execute("SELECT * FROM snapshots WHERE date>=?", (frm,)):
        snaps[(r["slug"], r["date"])] = r

    fails = {}  # (slug, date) -> detail (latest failed pull job for that day)
    for r in cat.conn.execute(
        "SELECT slug, date, detail FROM jobs WHERE kind='pull' AND state='failed' "
        "AND date>=? ORDER BY created_at", (frm,)
    ):
        fails[(r["slug"], r["date"])] = r["detail"] or ""

    # A currently-failed lease is the freshest error we have (and, before the
    # job log started recording failures, the only one).
    current = []  # (slug, date, detail, updated_at)
    for r in cat.conn.execute(
        "SELECT slug, date, detail, updated_at FROM leases WHERE state='failed' "
        "ORDER BY updated_at DESC"
    ):
        current.append((r["slug"], r["date"], r["detail"] or "", r["updated_at"]))
        if r["date"] and r["date"] >= frm:
            fails.setdefault((r["slug"], r["date"]), r["detail"] or "")

    log = cat.conn.execute(
        "SELECT slug, date, detail, created_at FROM jobs "
        "WHERE kind='pull' AND state='failed' ORDER BY created_at DESC LIMIT 100"
    ).fetchall()

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "today": today.isoformat(),
        "dates": dates, "slugs": slugs, "snaps": snaps, "fails": fails,
        "current": current, "log": log, "stats": cat.stats(),
    }


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


def _legend():
    items = [
        (NEW, "new data"), (UNCHANGED, "pulled, unchanged"),
        (FAILED, "failed"), (MISSING, "no attempt"),
    ]
    cells = "".join(
        f"<span class='li'><span class='cell {s}'>{'&times;' if s == FAILED else ''}"
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
            glyph = "&times;" if state == FAILED else ""
            tds.append(f"<td><span class='cell {state}' data-tip=\"{html.escape(tip)}\">"
                       f"{glyph}</span></td>")
        rows.append(f"<tr><th class='slug'>{html.escape(slug)}</th>{''.join(tds)}</tr>")
    return f"<div class='scroll'><table class='matrix'>{''.join(rows)}</table></div>"


def _calendar(data):
    total = len(data["slugs"])
    # Per-day tallies across all cities.
    tallies = {}
    for d in data["dates"]:
        n = u = f = 0
        for slug in data["slugs"]:
            state, _ = _cell(data, slug, d)
            if state == NEW: n += 1
            elif state == UNCHANGED: u += 1
            elif state == FAILED: f += 1
        tallies[d] = (n, u, f, total - n - u - f)

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
            n, u, f, m = tallies[d]
            tip = (f"{d}: {n} new, {u} unchanged, {f} failed, {m} no attempt "
                   f"(of {n + u + f + m})")
            bar = "".join(
                f"<i class='{cls}' style='flex:{v}'></i>"
                for cls, v in ((NEW, n), (UNCHANGED, u), (FAILED, f), (MISSING, m)) if v)
            summary = f"<span class='bad'>{f}&times;</span>" if f else \
                      (f"{n + u}&#10003;" if n + u else "&mdash;")
            today = " today" if d == data["today"] else ""
            out.append(
                f"<div class='day{today}' data-tip='{html.escape(tip)}'>"
                f"<span class='dn'>{int(d[8:10])}</span>"
                f"<span class='bar'>{bar}</span><span class='ct'>{summary}</span></div>")
        out.append("</div></div>")
    out.append("</div>")
    return "".join(out)


def _failures(data):
    out = []
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
                   "<th>when</th><th>error</th></tr>")
        for r in data["log"]:
            out.append(f"<tr><td>{html.escape(r['slug'])}</td>"
                       f"<td>{html.escape(r['date'] or '')}</td>"
                       f"<td>{html.escape((r['created_at'] or '')[:16])}</td>"
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
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink2: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --border: rgba(255,255,255,.10);
    --good-wash: rgba(12,163,12,.30);
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
.ct { font-size: 10px; color: var(--ink2); font-variant-numeric: tabular-nums; }
.ct .bad { color: var(--bad); font-weight: 600; }
.fails { border-collapse: collapse; width: 100%; background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px; font-size: 12.5px; }
.fails th { text-align: left; color: var(--muted); font-weight: 500; }
.fails th, .fails td { padding: 5px 10px; border-bottom: 1px solid var(--grid);
  vertical-align: top; }
.fails td.err { font-family: ui-monospace, Consolas, monospace; font-size: 11.5px;
  color: var(--ink2); word-break: break-all; }
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
