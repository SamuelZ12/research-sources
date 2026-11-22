"""Search an explicitly selected Ego Lite session, in a command-owned task space."""
import json
import subprocess
import uuid
from pathlib import Path

from rsrc.channels.x import search
from rsrc.channels.x.auth import CookieError
from rsrc.http import CapReached, HttpError

SCRIPT = Path(__file__).with_suffix(".js")
MESSAGES = {
    "auth_failed": "Ego Lite has no usable X login. Open X in Ego Lite and sign in.",
    "blocked": "X blocked access or requested verification in Ego Lite. Stop here.",
    "rate_limited": "X rate limited this account. Stop here.",
    "upstream_error": "X returned an unrecognized SearchTimeline response.",
    "timeout": "Ego Lite timed out. The search may have reached X.",
}


class BrowserError(HttpError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def execute(script):
    try:
        proc = subprocess.run(["ego-browser", "nodejs"], input=script, text=True, capture_output=True, timeout=25)
    except FileNotFoundError:
        raise CookieError("ego-browser is missing. Install Ego Lite with: open https://lite.ego.app") from None
    except subprocess.TimeoutExpired:
        raise BrowserError("timeout", MESSAGES["timeout"]) from None
    if proc.returncode:
        raise BrowserError("blocked", "Ego Lite is unavailable or browser control stopped. "
                           "To open it on macOS run: open -a 'ego lite'")
    return (proc.stdout + "\n" + proc.stderr).splitlines()


class Searcher(search.Searcher):
    page_size = search.PAGE_SIZE

    def __init__(self, client, environ):
        super().__init__(client, None, search.query_id_cache(environ))
        self.space = "rsrc X search " + uuid.uuid4().hex
        self.cleanup_needed = False
        self.first = True
        self.lock = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            raise CookieError("the Ego Lite X backend currently supports macOS") from None
        lock_path = Path.home() / ".cache/rsrc/x-browser.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = lock_path.open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise CookieError("another X browser command is still running; wait for it to finish") from None
        return self

    def __exit__(self, typ, value, tb):
        try:
            if self.cleanup_needed:
                execute(f"const space = (await listTaskSpaces()).find(s => s.name === {json.dumps(self.space)});"
                        "if (space?.ownership === 'agent') {"
                        " const task = await taskSpace(space.id); await task.finish({keep: []}); }")
        finally:
            self.lock.close()

    def _fetch(self, query, product, cursor, query_id, count):
        cost = 2 if self.first else 1
        if self.client.requests_made + cost > self.client.max_requests:
            if self.first:
                raise CookieError("x-browser needs --max-requests 2 or higher for navigation and search")
            raise CapReached()
        variables = dict(rawQuery=query, product=product, querySource="typed_query", count=count)
        if cursor:
            variables["cursor"] = cursor
        config = dict(space=self.space, first=self.first, variables=variables, features=search.FEATURES,
                      application=search.BEARER, id=query_id)
        self.client.requests_made += cost
        self.cleanup_needed = True
        try:
            lines = execute("const INPUT = " + json.dumps(config) + ";\n" + SCRIPT.read_text())
        except CookieError:
            self.cleanup_needed = False
            self.client.requests_made -= cost
            raise
        self.first = False
        marked = [line[7:] for line in lines if line.startswith("RSRC_X ")]
        if len(marked) != 1:
            raise BrowserError("blocked", "Ego Lite did not return a search response. Check browser control.")
        data = json.loads(marked[0])
        if data.get("dispatched") is False:
            self.client.requests_made -= 1
        if data.get("handedOff"):
            self.cleanup_needed = False
            raise BrowserError("blocked", "Complete the X verification in the Ego Lite page handed to you.")
        if code := data.get("error"):
            if code == "query_id_rejected":
                raise search.SearchError(f"SearchTimeline ID {query_id} rejected", query_id_rejected=True)
            raise BrowserError(code, MESSAGES[code])
        records = [search.to_record(tweet) for tweet in data["tweets"]]
        for record in records:
            record["source"] = "x-browser"
        return records, data["cursor"]
