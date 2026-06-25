"""addressvault CLI -- a thin wrapper over the Vault library.

    addressvault seed <datasets_dir>                 # import sources from datasets/*.toml
    addressvault sources [--json]
    addressvault pull <slug> [--force]               # fetch + dedup + record
    addressvault pull-due                             # pull every source due today, then sweep
    addressvault serve [--interval N]                # run the self-scheduler
    addressvault snapshots <slug> [--from D --to D --tier hot|cold] [--json]
    addressvault data <slug> [<date>|latest] [-o PATH|-]   # stream bytes (thaw-or-error if cold)
    addressvault thaw <slug> <date> [--ttl-hours N]  # copy a cold day back to hot
    addressvault sweep [--keep-days N]               # cool aged hot copies into restic
    addressvault stats [--json]

Set ADDRESSVAULT_DIR (or pass --dir) to the vault folder.
"""

import argparse
import json
import shutil
import sys

from addressvault.vault import Archived, Vault


def _print_snap(s):
    extra = f"  <- {s.unchanged_since}" if s.unchanged_since else ""
    print(f"  {s.slug} {s.date}  {s.tier:<4} {s.features:>10,} feat  {s.bytes:>12,} B{extra}")


def cmd_seed(vault, args):
    srcs = vault.seed(args.datasets_dir)
    print(f"  seeded {len(srcs)} source(s) from {args.datasets_dir}")


def cmd_sources(vault, args):
    srcs = vault.sources()
    if args.json:
        print(json.dumps([s.__dict__ for s in srcs], indent=2))
        return
    for s in srcs:
        flag = "" if s.enabled else " (disabled)"
        print(f"  {s.slug:<20} {s.access:<7} {s.provider}{flag}")


def cmd_pull(vault, args):
    snap = vault.pull(args.slug, force=args.force)
    _print_snap(snap)


def cmd_pull_due(vault, args):
    from addressvault import scheduler
    pulled = scheduler.pull_due(vault, keep_days=args.keep_days)
    print(f"  pulled {len(pulled)} due source(s)")


def cmd_serve(vault, args):
    from addressvault import scheduler
    scheduler.serve(vault, interval=args.interval, keep_days=args.keep_days)


def cmd_snapshots(vault, args):
    snaps = vault.snapshots(args.slug, frm=getattr(args, "from"), to=args.to, tier=args.tier)
    if args.json:
        print(json.dumps([s.__dict__ for s in snaps], indent=2))
        return
    if not snaps:
        print(f"  no snapshots for {args.slug}")
    for s in snaps:
        _print_snap(s)


def cmd_data(vault, args):
    try:
        p = vault.path(args.slug, args.date)
    except Archived as e:
        print(f"  {e}", file=sys.stderr)
        print(f"  thaw it: addressvault thaw {args.slug} {args.date}", file=sys.stderr)
        sys.exit(1)
    if args.out == "-":
        with open(p, "rb") as f:
            shutil.copyfileobj(f, sys.stdout.buffer)
    else:
        shutil.copyfile(p, args.out)
        print(f"  {p} -> {args.out}", file=sys.stderr)


def cmd_thaw(vault, args):
    snap = vault.thaw(args.slug, args.date, ttl_hours=args.ttl_hours)
    print(f"  thawed {snap.slug} {snap.date} (hot until TTL)")


def cmd_sweep(vault, args):
    swept = vault.sweep(keep_days=args.keep_days)
    print(f"  swept {len(swept)} snapshot(s) to cold")


def cmd_stats(vault, args):
    st = vault.stats()
    if args.json:
        print(json.dumps(st, indent=2))
        return
    print(f"  sources={st['sources']} snapshots={st['snapshots']} "
          f"hot={st['hot']} cold={st['cold']} hot_bytes={st['hot_bytes']:,}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="addressvault", description="Tiered store for raw city dumps")
    p.add_argument("--dir", help="Vault folder (default: ADDRESSVAULT_DIR)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("seed").add_argument("datasets_dir")
    sp = sub.add_parser("sources"); sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("pull"); sp.add_argument("slug"); sp.add_argument("--force", action="store_true")
    sp = sub.add_parser("pull-due"); sp.add_argument("--keep-days", type=int, default=2)
    sp = sub.add_parser("serve")
    sp.add_argument("--interval", type=int, default=3600); sp.add_argument("--keep-days", type=int, default=2)

    sp = sub.add_parser("snapshots")
    sp.add_argument("slug"); sp.add_argument("--from", dest="from"); sp.add_argument("--to")
    sp.add_argument("--tier", choices=["hot", "cold"]); sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("data")
    sp.add_argument("slug"); sp.add_argument("date", nargs="?", default="latest")
    sp.add_argument("-o", "--out", default="-", help="output path, or - for stdout")

    sp = sub.add_parser("thaw")
    sp.add_argument("slug"); sp.add_argument("date"); sp.add_argument("--ttl-hours", type=int, default=24)

    sp = sub.add_parser("sweep"); sp.add_argument("--keep-days", type=int, default=2)
    sp = sub.add_parser("stats"); sp.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    vault = Vault(dir=args.dir)
    handlers = {
        "seed": cmd_seed, "sources": cmd_sources, "pull": cmd_pull,
        "pull-due": cmd_pull_due, "serve": cmd_serve, "snapshots": cmd_snapshots,
        "data": cmd_data, "thaw": cmd_thaw, "sweep": cmd_sweep, "stats": cmd_stats,
    }
    handlers[args.command](vault, args)


if __name__ == "__main__":
    main()
