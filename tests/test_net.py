"""The link gate: offline/metered detection and the wait that defers a pull
until the host has a usable connection."""

import socket
import subprocess
from datetime import time as _time

import pytest

from addressvault import net
from addressvault.net import Metered, Offline, wait_for_link

UP = {"is_online": lambda: True, "is_metered": lambda: False}


@pytest.fixture(autouse=True)
def _no_cache():
    """Probes memoise for PROBE_TTL; stop one test's verdict reaching the next."""
    net.reset_cache()


def test_usable_link_returns_immediately_without_sleeping():
    slept = []
    wait_for_link(**UP, sleep=slept.append, clock=lambda: _clock("12:00"))
    assert slept == []  # never entered the loop


@pytest.mark.parametrize("reason,probes,expected", [
    ("metered", {"is_online": lambda: True, "is_metered": lambda: True}, Metered),
    ("offline", {"is_online": lambda: False, "is_metered": lambda: False}, Offline),
])
def test_gives_up_at_cutoff_naming_the_reason(reason, probes, expected):
    now = _clock("22:45")  # already past the 22:30 cutoff
    with pytest.raises(expected) as e:
        wait_for_link(cutoff=_time(22, 30), sleep=lambda s: None,
                      clock=lambda: now, **probes)
    assert e.value.reason == reason
    assert reason in str(e.value)


def test_waits_while_offline_then_proceeds():
    calls = iter([False, False, True])  # down twice, then the link returns
    slept = []
    wait_for_link(poll=900, is_online=lambda: next(calls), is_metered=lambda: False,
                  sleep=slept.append, clock=lambda: _clock("18:00"))
    assert slept == [900, 900]  # two 15-min waits, then returned


def test_a_tether_coming_up_mid_wait_switches_reason_without_restarting():
    # Offline, then the hotspot appears: still not pullable, but now for the
    # other reason. One gate re-evaluates both each cycle rather than running
    # two waits back to back.
    online = iter([False, True, True])
    metered = iter([True, False])  # asked only once the link is up
    slept = []
    wait_for_link(poll=900, is_online=lambda: next(online),
                  is_metered=lambda: next(metered),
                  sleep=slept.append, clock=lambda: _clock("18:00"))
    assert slept == [900, 900]


def test_no_wait_raises_at_once():
    slept = []
    with pytest.raises(Offline):
        wait_for_link(wait=False, is_online=lambda: False, is_metered=lambda: False,
                      sleep=slept.append, clock=lambda: _clock("12:00"))
    assert slept == []  # a lease holder must never block here


def test_offline_is_checked_before_metered():
    # A down link reports no connection profile, which the metered probe's
    # fail-open rule reads as "unmetered" -- the false green light that let a
    # pull walk into a dead resolver. Offline must win.
    assert net.link_state(is_online=lambda: False, is_metered=lambda: False) == "offline"


def test_online_probes_resolvers_and_fails_closed(monkeypatch):
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no route")))
    assert net.online() is False


def test_online_stops_at_the_first_resolver_that_answers(monkeypatch):
    tried = []

    class _Sock:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _connect(addr, timeout=None):
        tried.append(addr)
        return _Sock()

    monkeypatch.setattr(socket, "create_connection", _connect)
    assert net.online() is True
    assert tried == [(net.RESOLVERS[0], 443)]


def test_probe_result_is_reused_within_the_ttl(monkeypatch):
    # pull_due asks the gate once per source; an offline run must not probe the
    # network once per city.
    calls = []
    monkeypatch.setattr(net, "_probe_online", lambda: calls.append(1) or False)
    assert [net.online() for _ in range(30)] == [False] * 30
    assert len(calls) == 1


def test_metered_fails_open_on_probe_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("powershell not found")
    monkeypatch.setattr(subprocess, "run", boom)
    assert net.metered() is False


@pytest.mark.parametrize("out,expected", [
    ("Fixed\n", True), ("Variable\n", True),
    ("Unrestricted\n", False), ("", False), ("Unknown\n", False),
])
def test_metered_reads_networkcost(monkeypatch, out, expected):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""))
    assert net.metered() is expected


class _clock:
    """A fixed HH:MM wall clock; only ``.time()`` is used by wait_for_link."""
    def __init__(self, hhmm):
        h, m = hhmm.split(":")
        self._t = _time(int(h), int(m))

    def time(self):
        return self._t
