import pytest

from rsrc.http import CapReached, Client, HttpError, NetworkError

SENTINEL = "SENTINEL-COOKIE-VALUE"


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(responses, min_interval=1, **kwargs):
    calls = []
    clock = FakeClock()

    def transport(method, url, headers, body):
        calls.append((method, url))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    client = Client(min_interval=min_interval, transport=transport, clock=clock.time, sleep=clock.sleep, **kwargs)
    return client, calls, clock


def test_request_cap_stops_before_calling_network():
    client, calls, _ = make_client([(200, b"{}")] * 3, max_requests=2)
    client.request("GET", "https://x.com/a")
    client.request("GET", "https://x.com/b")
    with pytest.raises(CapReached):
        client.request("GET", "https://x.com/c")
    assert len(calls) == 2


def test_tries_stop_before_a_retry_the_cap_refuses():
    client, calls, _ = make_client([(500, b"")] * 3, max_requests=2)
    statuses = [client.request("GET", f"https://x.com/{n}")[0] for n in client.tries(range(3))]
    assert statuses == [500, 500] and len(calls) == 2


def test_the_first_try_still_meets_a_spent_cap():
    client, _, _ = make_client([(200, b"")], max_requests=1)
    client.request("GET", "https://x.com/a")
    with pytest.raises(CapReached):
        for _ in client.tries(range(3)):
            client.request("GET", "https://x.com/b")


def test_requests_are_spaced_by_min_interval():
    client, _, clock = make_client([(200, b"{}")] * 2, min_interval=1.0)
    client.request("GET", "https://x.com/a")
    clock.now += 0.25
    client.request("GET", "https://x.com/b")
    assert clock.sleeps == [pytest.approx(0.75)]


def test_first_request_does_not_wait():
    client, _, clock = make_client([(200, b"{}")], min_interval=1.0)
    client.request("GET", "https://x.com/a")
    assert clock.sleeps == []


def test_non_2xx_statuses_are_returned_not_raised():
    client, _, _ = make_client([(404, b"nope")])
    assert client.request("GET", "https://x.com/a") == (404, b"nope")


def test_network_error_message_does_not_leak_headers():
    client, _, _ = make_client([OSError("connection reset")])
    with pytest.raises(HttpError) as exc:
        client.request("GET", "https://x.com/a", headers={"cookie": f"auth_token={SENTINEL}"})
    assert "connection reset" in str(exc.value)
    assert SENTINEL not in str(exc.value)


def test_request_that_gets_no_response_raises_network_error():
    client, _, _ = make_client([OSError("connection reset")])
    with pytest.raises(NetworkError) as exc:
        client.request("GET", "https://x.com/a")
    assert str(exc.value) == "x.com: network error: connection reset"
    assert exc.value.code == "network_error"


def serve_redirect():
    """Local server: /start redirects to /landing; records every path it serves."""
    import http.server
    import threading

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _handle(self):
            seen.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/landing")
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = _handle

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def test_requests_carrying_cookies_do_not_follow_redirects():
    from rsrc.http import urllib_transport

    server, seen = serve_redirect()
    try:
        url = f"http://127.0.0.1:{server.server_port}/start"
        status, _ = urllib_transport("POST", url, {"cookie": f"auth_token={SENTINEL}"}, b"{}")
    finally:
        server.shutdown()
    assert status == 302
    assert seen == ["/start"]


def test_requests_without_cookies_still_follow_redirects():
    from rsrc.http import urllib_transport

    server, seen = serve_redirect()
    try:
        status, _ = urllib_transport("GET", f"http://127.0.0.1:{server.server_port}/start", {}, None)
    finally:
        server.shutdown()
    assert status == 200
    assert seen == ["/start", "/landing"]


def test_redirect_transport_returns_the_location_without_following_it():
    from rsrc.http import redirect_transport

    server, seen = serve_redirect()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        redirected = redirect_transport("GET", f"{base}/start", {}, None)
        direct = redirect_transport("GET", f"{base}/landing", {}, None)
    finally:
        server.shutdown()
    assert redirected == (302, b"/landing")
    assert direct == (200, b"")
    assert seen == ["/start", "/landing"]


def test_request_with_headers_shares_the_cap_and_accepts_both_transport_shapes():
    replies = iter([(403, {"X-RateLimit-Remaining": "0"}, b"limited"), (200, b"ok")])
    client = Client(max_requests=2, min_interval=0, transport=lambda *args: next(replies))
    assert client.request_with_headers("GET", "https://example.org") == (
        403, {"X-RateLimit-Remaining": "0"}, b"limited")
    assert client.request("GET", "https://example.org") == (200, b"ok")
    with pytest.raises(CapReached):
        client.request_with_headers("GET", "https://example.org")


def test_single_hop_transport_exposes_redirect_headers_without_following():
    from rsrc.http import single_hop_transport

    server, seen = serve_redirect()
    try:
        client = Client(min_interval=1, transport=single_hop_transport)
        url = f"http://127.0.0.1:{server.server_port}/start"
        status, headers, data = client.request_with_headers("GET", url, {"Authorization": "Bearer test-value"})
        assert (status, headers["Location"], data) == (302, "/landing", b"")
        assert seen == ["/start"]
    finally:
        server.shutdown()
        server.server_close()
