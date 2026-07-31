"""Self-scheduling writer: the only process that pulls and sweeps.

Keeping pulls/sweeps in one loop means a single writer touches the restic repo,
so there are no lock races. Consumers reading or thawing never need this running.
``serve`` is a plain loop; ``pull_due`` is also exposed so a host can drive it
from cron instead (``addressvault pull-due``).
"""

import time
from datetime import date

from addressvault import net


def is_due(vault, src, today=None):
    """Daily cadence: due when there is no snapshot recorded for today."""
    if not src.enabled:
        return False
    day = today or date.today().isoformat()
    latest = vault.cat.latest_snapshot(src.slug)
    return not (latest and latest["date"] == day)


def pull_due(vault, today=None, keep_days=2):
    pulled = []
    for src in vault.sources():
        if not is_due(vault, src, today=today):
            continue
        try:
            pulled.append(vault.pull(src.slug, today=today))
        except net.LinkUnavailable as e:
            # Host-wide, so the remaining sources cannot fare any better; they
            # stay due for the next run. Sweeping still can (restic is local).
            print(f"  [pull] {e}; stopping after {len(pulled)} pulled")
            break
        except Exception as e:  # one bad source must not stop the rest
            print(f"  [pull] {src.slug} failed: {e}")
    vault.sweep(keep_days=keep_days, today=today)
    vault.recool_expired()
    return pulled


def serve(vault, interval=3600, keep_days=2):
    n = len(vault.sources())
    print(f"address-vault scheduler: {n} sources, every {interval}s")
    while True:
        pulled = pull_due(vault, keep_days=keep_days)
        print(f"  pulled {len(pulled)} due source(s); sleeping {interval}s")
        time.sleep(interval)
