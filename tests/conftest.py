"""Shared fixtures: a Vault on a tmp dir, and a throwaway HTTP server so the
static fetch path (download + HEAD smart-cache) runs without any network."""

import functools
import http.server
import json
import socketserver
import threading

import pytest

from addressvault import net
from addressvault.vault import Vault


@pytest.fixture(autouse=True)
def _usable_link(monkeypatch):
    """Pulls gate on a usable link before fetching; make that gate a no-op
    everywhere so no test spawns PowerShell, opens a socket, or depends on real
    network state. Detection and the wait have their own tests (test_net.py).
    The probe cache is per-process, so clear it too or one test's stubbed
    verdict leaks into the next."""
    net.reset_cache()
    monkeypatch.setattr("addressvault.net.wait_for_link", lambda **k: None)


@pytest.fixture
def vault(tmp_path):
    return Vault(dir=str(tmp_path / "vault"))


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # keep test output clean
        pass


@pytest.fixture
def http_dir(tmp_path):
    """Serve ``tmp_path/www`` over HTTP. Yields (base_url, dir_path)."""
    d = tmp_path / "www"
    d.mkdir()
    handler = functools.partial(_QuietHandler, directory=str(d))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", d
    finally:
        httpd.shutdown()


def write_geojson(d, name, n):
    """Write an n-feature point FeatureCollection to ``d/name``."""
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": i},
         "geometry": {"type": "Point", "coordinates": [float(i), 0.0]}}
        for i in range(n)
    ]}
    (d / name).write_text(json.dumps(fc))
