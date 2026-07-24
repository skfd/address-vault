"""Metered-connection detection and the wait that defers a pull until the link
is unmetered."""

import subprocess
from datetime import time as _time

import pytest

from addressvault import net
from addressvault.net import Metered, wait_for_unmetered


def test_unmetered_returns_immediately_without_sleeping():
    slept = []
    wait_for_unmetered(is_metered=lambda: False, sleep=slept.append,
                       clock=lambda: _clock("12:00"))
    assert slept == []  # never entered the loop


def test_waits_while_metered_then_proceeds():
    calls = iter([True, True, False])  # metered twice, then the link clears
    slept = []
    wait_for_unmetered(poll=900, is_metered=lambda: next(calls),
                       sleep=slept.append, clock=lambda: _clock("18:00"))
    assert slept == [900, 900]  # two 15-min waits, then returned


def test_gives_up_at_cutoff():
    now = _clock("22:45")  # already past the 22:30 cutoff
    with pytest.raises(Metered):
        wait_for_unmetered(cutoff=_time(22, 30), is_metered=lambda: True,
                           sleep=lambda s: None, clock=lambda: now)


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
    """A fixed HH:MM wall clock; only ``.time()`` is used by wait_for_unmetered."""
    def __init__(self, hhmm):
        h, m = hhmm.split(":")
        self._t = _time(int(h), int(m))

    def time(self):
        return self._t
