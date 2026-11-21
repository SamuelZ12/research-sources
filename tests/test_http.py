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


def make_client(responses, **kwargs):
    calls = []
    clock = FakeClock()

    def transport(method, url, headers, body):
        calls.append((method, url))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    client = Client(transport=transport, clock=clock.time, sleep=clock.sleep, **kwargs)
    return client, calls, clock


def test_request_cap_stops_before_calling_network():
    client, calls, _ = make_client([(200, b"{}")] * 3, max_requests=2)
    client.request("GET", "https://x.com/a")
    client.request("GET", "https://x.com/b")
    with pytest.raises(CapReached):
        client.request("GET", "https://x.com/c")
    assert len(calls) == 2


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
