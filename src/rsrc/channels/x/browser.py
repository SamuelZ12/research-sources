import json
import subprocess
import uuid
from pathlib import Path

from rsrc import chrome
from rsrc.channels.x import connection, search
from rsrc.channels.x.timeline import profile_record
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


def program(path):
    return path.read_text().replace("/* PAGE */", page_program(path))


def page_program(path):
    source = path.with_name(path.stem + "_page.js").read_text()
    return source.replace("/* PROJECT_TWEET */", SCRIPT.with_name("project_tweet.js").read_text())


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
    backend = "x-browser"
    messages = MESSAGES
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
                raise CookieError(f"{self.backend} needs --max-requests 2 or higher for navigation and search")
            raise CapReached()
        variables = dict(rawQuery=query, product=product, querySource="typed_query", count=count)
        if cursor:
            variables["cursor"] = cursor
        config = dict(space=self.space, first=self.first, variables=variables, features=search.FEATURES,
                      application=search.BEARER, id=query_id)
        self.client.requests_made += cost
        try:
            data = self.evaluate(SCRIPT, config)
        except CookieError:
            self.client.requests_made -= cost
            raise
        self.first = False
        if data.get("dispatched") is False:
            self.client.requests_made -= 1
        if data.get("handedOff"):
            self.cleanup_needed = False
            raise BrowserError("blocked", "Complete the X verification in the Ego Lite page handed to you.")
        if code := data.get("error"):
            if code == "query_id_rejected":
                raise search.SearchError(f"SearchTimeline ID {query_id} rejected", query_id_rejected=True)
            raise BrowserError(code, self.messages[code])
        records = [search.to_record(tweet) for tweet in data["tweets"]]
        for record in records:
            record["source"] = self.backend
        return records, data["cursor"]

    def evaluate(self, script, config):
        self.cleanup_needed = True
        try:
            lines = execute("const INPUT = " + json.dumps(config) + ";\n" + program(script))
        except CookieError:
            self.cleanup_needed = False
            raise
        marked = [line[7:] for line in lines if line.startswith("RSRC_X ")]
        if len(marked) != 1:
            raise BrowserError("blocked", "Ego Lite did not return a response. Check browser control.")
        return json.loads(marked[0])


class ChromeSearcher(Searcher):
    backend = "x-chrome"
    messages = MESSAGES | {
        "auth_failed": "No usable X login in this Chrome profile. Open X in that profile and sign in.",
        "blocked": "X returned a challenge or blocked the request in Google Chrome. Stop here.",
        "timeout": "Google Chrome timed out. The request may have reached X.",
    }

    def __init__(self, client, environ, notes):
        super().__init__(client, environ)
        self.environ = environ
        self.notes = notes
        self.tab = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.tab:
            self.tab.__exit__(*args)

    def evaluate(self, script, config):
        if self.tab is None:
            try:
                endpoint, started = connection.resolve(self.environ)
                if started:
                    self.notes.append(connection.STARTED)
                self.tab = chrome.Tab(endpoint).__enter__()
            except chrome.ChromeError as e:
                raise CookieError(str(e)) from None
            except OSError:
                raise CookieError("Could not start Chrome or save its connection settings.") from None
            self.tab.navigate(connection.ORIGIN + "/robots.txt")
        if script == SCRIPT:
            config["path"] = "/i/api/graphql/" + config["id"] + "/SearchTimeline"
        return self.tab.evaluate("(" + page_program(script) + ")(" + json.dumps(config) + ")")


def session_for(backend, client, environ, notes):
    if backend == "x-chrome":
        return ChromeSearcher(client, environ, notes)
    return Searcher(client, environ)


def timeline(session, handle, limit):
    client = session.client
    if client.max_requests < 3:
        if session.backend == "x-chrome":
            raise CookieError("x-chrome needs --max-requests 3 or higher for navigation, lookup and timeline")
        raise CapReached("Browser timeline needs navigation, lookup and a timeline page")
    with session:
        config = dict(handle=handle, limit=limit, maxRequests=client.max_requests, space=session.space, application=search.BEARER,
                      ops=json.loads(SCRIPT.with_name("timeline_ops.json").read_text()))
        client.requests_made = client.max_requests
        try:
            data = session.evaluate(SCRIPT.with_name("timeline.js"), config)
        except CookieError:
            client.requests_made = 0
            raise
        client.requests_made = data["requests"]
        if data.get("handedOff"):
            session.cleanup_needed = False
            raise BrowserError("blocked", "Complete the X verification in the Ego Lite page handed to you.")
        if code := data.get("error"):
            raise BrowserError(code, "X returned an unrecognized account timeline" if code == "upstream_error"
                               else session.messages[code])
        rows = []
        for entry in data["tweets"]:
            record = search.to_record(entry["tweet"])
            record["source"] = session.backend
            if entry.get("reposted_by"):
                record["reposted_by"] = entry["reposted_by"]
            rows.append(record)
        account = profile_record(data["account"])
        return account, rows, data.get("capped", False)
