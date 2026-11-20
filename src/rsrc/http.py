"""The single path for network calls: request spacing, a hard request cap,
and error messages that never contain request headers (cookies)."""

import http.client
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

TIMEOUT_S = 20


class HttpError(Exception):
    """A request failed. `code` is the envelope error code for the kind of failure: subclasses
    set their own, so no caller works it out from the message text."""

    code = "upstream_error"


class NetworkError(HttpError):
    """No HTTP response arrived (DNS, connection, TLS, timeout)."""

    code = "network_error"


class AuthError(HttpError):
    code = "auth_failed"


class RateLimited(HttpError):
    code = "rate_limited"


class NotFound(HttpError):
    code = "not_found"


class CapReached(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # surface the 3xx instead of resending headers to the Location host


_follow_redirects = urllib.request.build_opener()
_no_redirects = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method, url, headers, body):
    headers = headers or {}
    opener = _no_redirects if any(k.lower() == "cookie" for k in headers) else _follow_redirects
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=TIMEOUT_S) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Client:
    def __init__(self, max_requests=None, min_interval=1.0, transport=urllib_transport,
                 clock=time.monotonic, sleep=time.sleep):
        self.max_requests = max_requests
        self.min_interval = min_interval
        self.requests_made = 0
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._last = None

    def request(self, method, url, headers=None, body=None) -> tuple[int, bytes]:
        """Return (status, body) for any HTTP status; raise NetworkError if no response arrives."""
        if self.max_requests is not None and self.requests_made >= self.max_requests:
            raise CapReached(f"request cap of {self.max_requests} reached")
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self.requests_made += 1
        try:
            return self._transport(method, url, headers, body)
        except (OSError, http.client.HTTPException) as e:
            reason = getattr(e, "reason", None) or e
            raise NetworkError(f"{urlsplit(url).netloc}: network error: {reason}") from None
        finally:
            self._last = self._clock()
