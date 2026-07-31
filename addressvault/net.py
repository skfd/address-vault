"""Is the current internet link usable for a pull? (Windows.)

Two host-level conditions stop a pull before it starts: the link being **down**
(no route out) and the link being **metered** (cellular/tethering, where a daily
pull moving ~590 MB is both slow and costs data). Both are properties of the
host rather than of any one source, both are knowable before a byte moves, and
both resolve the same way -- wait, then give up at a nightly cutoff so a fresh
download never starts inside the 23:00-06:00 quiet window. ``wait_for_link`` is
the single gate for both; it re-checks each cycle, so a hotspot that comes up
mid-wait moves from offline to metered without restarting the wait.

A skipped pull records no snapshot, so the source stays "due" and the next run
picks it up -- no deferred state to track.

Detection mirrors ontario-address-changes' ``daily-update.ps1``: ``Test-Online``
(a TCP probe of public resolvers) and ``Test-Metered`` (the WinRT NetworkCost
API, run from Windows PowerShell 5.1 because the projection syntax does not load
under pwsh 7). The two fail in opposite directions on purpose: metered fails
*open*, so a broken API never silently blocks updates; offline fails *closed*,
because nothing answering is the observation itself, not a detection error.
"""

import socket
import subprocess
import time
from datetime import datetime, time as _time

# Probed on 443 rather than 53: a plain reachability check that a captive portal
# or a dead default route fails, without depending on DNS.
RESOLVERS = ("1.1.1.1", "8.8.8.8", "9.9.9.9")
PROBE_TIMEOUT = 4
# ``pull_due`` asks the gate once per source, so an offline run would otherwise
# probe the network ~30 times in a few seconds. Far below any poll interval, so
# a real wait still re-probes on every cycle.
PROBE_TTL = 60

_PROBE = ("[Windows.Networking.Connectivity.NetworkInformation,"
          "Windows.Networking.Connectivity,ContentType=WindowsRuntime]::"
          "GetInternetConnectionProfile().GetConnectionCost().NetworkCostType")


class LinkUnavailable(Exception):
    """The link was still unusable when the gate gave up. ``reason`` is
    ``"offline"`` or ``"metered"``."""

    reason = None


class Offline(LinkUnavailable):
    """No route out; nothing to do but wait for the link to come back."""

    reason = "offline"


class Metered(LinkUnavailable):
    """The link is up but costs data (cellular, tethering, or a Wi-Fi flagged
    "Metered connection")."""

    reason = "metered"


_UNAVAILABLE = {"offline": Offline, "metered": Metered}

_cache = {}


def _cached(key, compute, clock=time.monotonic):
    hit = _cache.get(key)
    now = clock()
    if hit is not None and now - hit[0] < PROBE_TTL:
        return hit[1]
    value = compute()
    _cache[key] = (now, value)
    return value


def reset_cache():
    """Drop memoised probe results. Tests need this; a long-lived ``serve`` does
    not, since PROBE_TTL is shorter than any poll interval."""
    _cache.clear()


def _probe_online():
    for host in RESOLVERS:
        try:
            with socket.create_connection((host, 443), timeout=PROBE_TIMEOUT):
                return True
        except OSError:
            continue
    return False


def online():
    """True if any public resolver accepts a TCP connection.

    DNS is deliberately not probed: a resolver that fails while the link is up
    is a fetch-level error (the fetchers retry it), not a reason to defer the
    whole run. Deferring on it would also make "offline" mean two things."""
    return _cached("online", _probe_online)


def _probe_metered():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"try {{ {_PROBE} }} catch {{}}"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip() in ("Fixed", "Variable")


def metered():
    """True only if Windows reports the connection as Fixed/Variable cost
    (cellular, tethering, or a Wi-Fi flagged "Metered connection"). Any
    detection failure returns False -- fail open, never block on a broken API."""
    return _cached("metered", _probe_metered)


def link_state(*, is_online=None, is_metered=None):
    """``None`` if a pull may proceed, else why not: ``"offline"``/``"metered"``.

    Offline is tested first because a down link makes the metered probe
    meaningless -- Windows reports no connection profile, which the fail-open
    rule reads as "unmetered", i.e. exactly the false green light that let a
    pull walk into a dead resolver."""
    if not (is_online or online)():
        return "offline"
    if (is_metered or metered)():
        return "metered"
    return None


def wait_for_link(*, wait=True, poll=900, cutoff=_time(22, 30),
                  is_online=None, is_metered=None, sleep=time.sleep, clock=None):
    """Block while the link is unusable, re-checking every ``poll`` seconds,
    until it is usable (return) or the local time reaches ``cutoff`` (raise
    ``Offline``/``Metered``). Usable already: return at once, no wait.

    ``wait=False`` raises straight away instead of waiting -- for a caller that
    wants an error rather than an all-afternoon block, and for any caller
    holding a slug's lease. ``cutoff`` sits before the 23:00 quiet window so a
    download that clears the wait still starts inside the allowed hours."""
    now = clock or datetime.now
    while True:
        reason = link_state(is_online=is_online, is_metered=is_metered)
        if reason is None:
            return
        if not wait:
            raise _UNAVAILABLE[reason](f"link is {reason}")
        if now().time() >= cutoff:
            raise _UNAVAILABLE[reason](
                f"link still {reason} at cutoff {cutoff:%H:%M}")
        print(f"  link is {reason}; waiting for a usable one "
              f"(recheck in {poll // 60}m)")
        sleep(poll)
