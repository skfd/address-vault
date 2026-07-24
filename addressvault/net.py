"""Is the current internet connection metered? (Windows.)

A daily pull can move ~590 MB; on a phone hotspot that is both slow and costs
data, so ``wait_for_unmetered`` blocks the pull until the link is unmetered
again, giving up at a nightly cutoff so a fresh download never starts inside the
23:00-06:00 quiet window. A skipped pull records no snapshot, so the source
stays "due" and the next run picks it up -- no deferred state to track.

Detection mirrors ontario-address-changes' ``Test-Metered``: the WinRT
NetworkCost API, probed from Windows PowerShell 5.1 because the projection syntax
does not load under pwsh 7, and failing *open* (treated as unmetered) so a broken
API never silently blocks updates.
"""

import subprocess
import time
from datetime import datetime, time as _time

_PROBE = ("[Windows.Networking.Connectivity.NetworkInformation,"
          "Windows.Networking.Connectivity,ContentType=WindowsRuntime]::"
          "GetInternetConnectionProfile().GetConnectionCost().NetworkCostType")


class Metered(Exception):
    """The connection was still metered when the wait gave up at its cutoff."""


def metered():
    """True only if Windows reports the connection as Fixed/Variable cost
    (cellular, tethering, or a Wi-Fi flagged "Metered connection"). Any
    detection failure returns False -- fail open, never block on a broken API."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"try {{ {_PROBE} }} catch {{}}"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip() in ("Fixed", "Variable")


def wait_for_unmetered(*, poll=900, cutoff=_time(22, 30),
                       is_metered=None, sleep=time.sleep, clock=None):
    """Block while the connection is metered, re-checking every ``poll`` seconds,
    until it is unmetered (return) or the local time reaches ``cutoff`` (raise
    ``Metered``). Not metered: return at once, no wait. ``cutoff`` sits before
    the 23:00 quiet window so a download that clears the wait still starts in the
    allowed hours."""
    check = is_metered or metered
    now = clock or datetime.now
    while check():
        if now().time() >= cutoff:
            raise Metered(f"connection still metered at cutoff {cutoff:%H:%M}")
        print("  metered connection; waiting for an unmetered link "
              f"(recheck in {poll // 60}m)")
        sleep(poll)
