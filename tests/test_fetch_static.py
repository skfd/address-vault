"""The static download path: a connection dropped mid-body must not cost the
whole (~590 MB) fetch."""

import contextlib
import http.server
import socketserver
import threading

import pytest
import requests

from addressvault.fetch import static

# 1 MB: several 256 KB read chunks, so a drop at the halfway mark leaves real
# progress on disk (and a wrong resume offset shows up as a corrupt file).
BODY = b"0123456789abcdef" * 65536


@contextlib.contextmanager
def serve(body, *, drops, honor_range=True):
    """Serve ``body``, dropping the connection mid-body the first ``drops``
    requests. Yields (url, state); state["ranges"] records each Range header."""
    state = {"drops": drops, "ranges": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # keep test output clean
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            state["ranges"].append(rng)
            start = 0
            if rng and honor_range:
                start = int(rng.split("=")[1].split("-")[0])
                self.send_response(206)
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            else:
                self.send_response(200)
            chunk = body[start:]
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Last-Modified", "Wed, 01 Jul 2026 00:00:00 GMT")
            self.end_headers()
            if state["drops"] > 0:
                state["drops"] -= 1
                self.wfile.write(chunk[:len(chunk) // 2])
                self.wfile.flush()
                self.close_connection = True
                self.connection.close()
                return
            self.wfile.write(chunk)

    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/dump.geojson", state
    finally:
        httpd.shutdown()


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(static, "RETRY_WAIT", 0)


def test_resumes_after_a_dropped_connection(tmp_path):
    dest = tmp_path / "download.bin"
    with serve(BODY, drops=1) as (url, state):
        headers = static._download(url, str(dest))

    assert dest.read_bytes() == BODY
    assert headers["content_length"] == len(BODY)  # full size, not the 206 tail
    assert headers["last_modified"] == "Wed, 01 Jul 2026 00:00:00 GMT"
    assert state["ranges"] == [None, f"bytes={len(BODY) // 2}-"]  # resumed


def test_restarts_when_the_server_ignores_range(tmp_path):
    dest = tmp_path / "download.bin"
    with serve(BODY, drops=1, honor_range=False) as (url, state):
        headers = static._download(url, str(dest))

    assert dest.read_bytes() == BODY  # restarted, not appended onto the partial
    assert headers["content_length"] == len(BODY)
    assert len(state["ranges"]) == 2


def test_gives_up_after_the_retry_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(static, "RETRIES", 2)
    dest = tmp_path / "download.bin"
    with serve(BODY, drops=99) as (url, state):
        with pytest.raises(requests.RequestException):
            static._download(url, str(dest))

    assert len(state["ranges"]) == 3  # the initial attempt plus RETRIES


def test_a_failed_head_on_a_down_link_defers_instead_of_downloading(monkeypatch):
    # "Check failed, download anyway" is right for a flaky remote and wrong for
    # a dead link: the download cannot work either, and would spend the whole
    # retry budget rediscovering that. The gate answers before we fall through.
    from addressvault import net

    monkeypatch.setattr("addressvault.net.wait_for_link",
                        lambda **k: (_ for _ in ()).throw(net.Offline("link is offline")))
    with pytest.raises(net.Offline):
        static._head("http://127.0.0.1:1/nothing")  # refused: nothing listens


def test_a_failed_head_on_a_live_link_still_falls_through_to_the_download():
    # The autouse fixture leaves the gate open, so a HEAD that failed for any
    # other reason keeps the old behaviour: no headers, download it blind.
    assert static._head("http://127.0.0.1:1/nothing") == {}


def test_a_down_link_surfaces_as_link_unavailable_not_a_source_failure(
        tmp_path, monkeypatch):
    # The budget buys ~90s, nowhere near long enough to outlast an outage. On
    # exhaustion, ask why: a dead link is the gate's business, and the caller
    # must see LinkUnavailable (skip the day) rather than a download error
    # (log a failure against a source that was never reachable to begin with).
    from addressvault import net

    monkeypatch.setattr(static, "RETRIES", 1)
    monkeypatch.setattr("addressvault.net.wait_for_link",
                        lambda **k: (_ for _ in ()).throw(net.Offline("link is offline")))
    dest = tmp_path / "download.bin"
    with serve(BODY, drops=99) as (url, _state):
        with pytest.raises(net.Offline):
            static._download(url, str(dest))
