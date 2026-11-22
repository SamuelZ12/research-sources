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


class Blocked(HttpError):
    code = "blocked"


def raise_refusal(status: int, body: bytes, source: str, hint: str = "") -> None:
    """Raise the error for an answer that means the same from every JSON source: HTTP 429, 401 or 403,
    or an HTML page in place of data. Handle a source's own statuses, such as a 404, before calling."""
    hint = f". {hint}" if hint else ""
    if status == 429:
        raise RateLimited(f"{source} rate limit (HTTP 429); stopped without retrying{hint}")
    if status == 401:
        raise AuthError(f"{source} requires authentication (HTTP 401){hint}")
    if status == 403:
        raise Blocked(f"{source} refused access (HTTP 403){hint}")
    if body.lstrip()[:1] == b"<":
        raise Blocked(f"{source} returned a page instead of data (HTTP {status}){hint}")


class CapReached(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # surface the 3xx instead of resending headers to the Location host


_follow_redirects = urllib.request.build_opener()
_no_redirects = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method, url, headers, body):
    status, _, data = _urllib_response(method, url, headers, body)
    return status, data


def _urllib_response(method, url, headers, body):
    headers = headers or {}
    opener = _no_redirects if any(k.lower() == "cookie" for k in headers) else _follow_redirects
    return _open(opener, method, url, headers, body)


def _open(opener, method, url, headers, body):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        response = opener.open(request, timeout=TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, response.headers, response.read()


def single_hop_transport(method, url, headers, body):
    """Return the response headers and body, including for an unfollowed redirect."""
    return _open(_no_redirects, method, url, headers, body)


def redirect_transport(method, url, headers, body):
    """A transport that never follows a redirect: returns (status, the `Location` header as bytes),
    with empty bytes when the response has no `Location`. For reading where a link points."""
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with _no_redirects.open(request, timeout=TIMEOUT_S) as response:
            return response.status, (response.headers.get("Location") or "").encode()
    except urllib.error.HTTPError as e:
        return e.code, (e.headers.get("Location") or "").encode()


class Client:
    """`min_interval` has no default: seconds between this client's requests, chosen per source.
    A scraped site needs a pause; one official API answering a two-call read does not."""

    def __init__(self, *, min_interval, max_requests=None, transport=urllib_transport,
                 clock=time.monotonic, sleep=time.sleep):
        self.max_requests = max_requests
        self.min_interval = min_interval
        self.requests_made = 0
        self._transport = _urllib_response if transport is urllib_transport else transport
        self._clock = clock
        self._sleep = sleep
        self._last = None

    @property
    def spent(self) -> bool:
        return self.max_requests is not None and self.requests_made >= self.max_requests

    def tries(self, attempts):
        """Yield each of `attempts` while the request cap allows another request. The first always runs,
        so a cap spent beforehand still raises CapReached. A retry loop iterates this and, when it runs
        out, raises the failure it was retrying: the cap never hides that failure."""
        for number, attempt in enumerate(attempts):
            if number and self.spent:
                return
            yield attempt

    def request(self, method, url, headers=None, body=None) -> tuple[int, bytes]:
        """Return (status, body) for any HTTP status; raise NetworkError if no response arrives."""
        status, _, data = self.request_with_headers(method, url, headers, body)
        return status, data

    def request_with_headers(self, method, url, headers=None, body=None):
        """Return (status, headers, body). Legacy pair transports supply empty headers."""
        if self.max_requests is not None and self.requests_made >= self.max_requests:
            raise CapReached(f"request cap of {self.max_requests} reached")
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self.requests_made += 1
        try:
            response = self._transport(method, url, headers, body)
            if len(response) == 2:
                status, data = response
                return status, {}, data
            return response
        except (OSError, http.client.HTTPException) as e:
            reason = str(getattr(e, "reason", None) or e)
            for key, value in (headers or {}).items():
                if key.lower() == "authorization" and any(part in reason for part in value.split()[1:] or [value]):
                    reason = "authenticated request failed"
            raise NetworkError(f"{urlsplit(url).netloc}: network error: {reason}") from None
        finally:
            self._last = self._clock()
