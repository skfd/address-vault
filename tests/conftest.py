"""Shared fixtures: a Vault on a tmp dir, and a throwaway HTTP server so the
static fetch path (download + HEAD smart-cache) runs without any network."""

import functools
import http.server
import json
import socketserver
import threading

import pytest

from addressvault.vault import Vault


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
