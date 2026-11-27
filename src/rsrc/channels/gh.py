import http.client
import json
import re
import time
import urllib.request
from datetime import UTC, datetime
from urllib.parse import urlencode, urljoin, urlsplit

from rsrc.http import (CapReached, Client, HttpError, NetworkError, raise_refusal, read_response,
                       single_hop_response, urllib_transport)
from rsrc import proc
from rsrc import envelope as env

CHANNEL = "gh"
BACKEND = "gh"
TIMEOUT = 60
README_CHARS = 4000
MAX_LIMIT = 1000  # GitHub search returns at most 1,000 results per query

REPO_FIELDS = ("fullName,url,description,owner,pushedAt,stargazersCount,forksCount,"
               "openIssuesCount,watchersCount,language,isArchived,isFork,createdAt,updatedAt")
ISSUE_FIELDS = ("repository,number,url,title,author,createdAt,updatedAt,closedAt,state,"
                "body,commentsCount,labels,isPullRequest")

_OWNER_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/(?!\.{1,2}$)[A-Za-z0-9._-]+")
_QUERY_TOKENS = re.compile(r'(?:"(?:\\.|[^"\\])*"|[^\s()"])+')
_ISSUE_TYPE = re.compile(r'-?(?:(?:is|type):("?)(?:issue|pr)\1|is:("?)pull-request\2)', re.I)


def register(subparsers) -> None:
    gh = subparsers.add_parser("gh", help="GitHub repositories and issues")
    actions = gh.add_subparsers(dest="gh_action", metavar="ACTION", required=True)

    repo = actions.add_parser("repo", help="one repository: stats, latest release, README")
    repo.add_argument("repo", metavar="OWNER/REPO")
    repo.set_defaults(run=run_repo, action="repo")

    search = actions.add_parser("search", help="search repositories")
    search.add_argument("query", metavar="QUERY")
    search.add_argument("--limit", type=int, default=20, help=f"repositories, 1-{MAX_LIMIT} (default %(default)s)")
    search.set_defaults(run=run_search, action="search")

    issues = actions.add_parser("issues", help="search issues")
    issues.add_argument("query", metavar="QUERY", nargs="?", default="",
                        help="use is:pr in the query to return pull requests")
    issues.add_argument("--repo", metavar="OWNER/REPO", help="search only this repository")
    issues.add_argument("--limit", type=int, default=30, help=f"issues, 1-{MAX_LIMIT} (default %(default)s)")
    issues.add_argument("--state", choices=("open", "closed", "all"), default="open",
                        help="issue state (default %(default)s)")
    issues.set_defaults(run=run_issues, action="issues")

    for command in (repo, issues):
        command.add_argument("--backend", choices=("gh",), help="use the logged-in GitHub CLI directly")


class _Failed(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _NoLogin(_Failed):
    """No local gh credentials. An upstream rejection never selects public reads."""


class _Gh:
    """Runs gh calls and records one meta.attempts entry per call."""

    def __init__(self, ctx):
        self.connection = None
        self.backend = BACKEND
        self.allow_public = ctx.environ.get("GH_HOST", "github.com").lower() == "github.com"
        self.runner = ctx.runner or proc.run
        self.attempts: list[dict] = []

        transport = self.transport if ctx.transport is urllib_transport else ctx.transport
        self.client = Client(max_requests=20, min_interval=0, transport=transport)

    def close(self):
        if self.connection is not None:
            self.connection.close()

    def _record(self, outcome: str, ms) -> None:
        self.attempts.append(env.attempt(BACKEND, outcome, ms=ms))

    def call(self, argv: list[str], *, parse=json.loads, missing_ok: bool = False):
        """stdout parsed by `parse`; None if `missing_ok` and gh reports HTTP 404."""
        start = time.monotonic()
        try:
            result = self.runner(argv, TIMEOUT)
        except proc.Missing:
            self._record("config", None)
            raise _NoLogin("config", "gh not installed (brew install gh)") from None
        except proc.Timeout:
            self._record("timeout", env.elapsed_ms(start))
            raise _Failed("timeout", f"gh did not answer within {TIMEOUT} s") from None
        ms = env.elapsed_ms(start)
        if result.code != 0:
            code, message = classify(result)
            if code == "not_found" and missing_ok:
                self._record("no_results", ms)
                return None
            self._record(code, ms)
            if code == "config" and result.code == 4 and "http " not in result.stderr.lower():
                raise _NoLogin(code, message)
            raise _Failed(code, message)
        try:
            value = parse(result.stdout)
        except (ValueError, TypeError, KeyError, AttributeError):
            self._record("upstream_error", ms)
            raise _Failed("upstream_error", "gh returned malformed or unexpected JSON") from None
        self._record("no_results" if value in ([], (0, [])) else "ok", ms)
        return value

    def signed(self, path, parse, *, raw=False, missing_ok=False):
        argv = ["gh", "api"]
        if raw:
            argv.extend(["-H", "Accept: application/vnd.github.raw"])
        return self.call([*argv, path], parse=parse, missing_ok=missing_ok)

    def transport(self, method, url, headers, body):
        target = urlsplit(url)
        if target.netloc != "api.github.com":
            return single_hop_response(method, url, headers, body)
        if self.connection is None:
            if "https" in urllib.request.getproxies() and not urllib.request.proxy_bypass(target.hostname):
                return single_hop_response(method, url, headers, body)
            self.connection = http.client.HTTPSConnection(target.hostname, timeout=TIMEOUT)
        self.connection.request(method, target.path + ("?" + target.query if target.query else ""), headers=headers)
        with self.connection.getresponse() as response:
            return read_response(response)

    def public(self, path, parse, *, raw=False, missing_ok=False):
        self.backend = "github-rest"
        started = time.monotonic()
        previous = self.client.requests_made
        outcome = "upstream_error"
        url = "https://api.github.com/" + path
        headers = {"Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
                   "User-Agent": "research-sources"}
        if path.startswith("search/"):
            headers["Accept-Encoding"] = "gzip"
        try:
            while True:
                status, response_headers, body = self.client.request_with_headers("GET", url, headers)
                if not isinstance(status, int) or not 100 <= status <= 599:
                    raise _Failed("upstream_error", "Invalid GitHub HTTP status")
                fields = {k.lower(): v for k, v in response_headers.items()}
                if status not in (301, 302, 303, 307, 308):
                    break
                location = fields.get("location")
                if not location:
                    raise _Failed("upstream_error", "Missing GitHub redirect location")
                url = urljoin(url, location)
                target = urlsplit(url)
                if target.scheme != "https" or not target.hostname or target.username is not None:
                    raise _Failed("upstream_error", "Unexpected GitHub redirect")
                if target.netloc != "api.github.com":
                    headers = {"User-Agent": "research-sources"}
            if status == 404 and missing_ok:
                outcome = "no_results"
                return None
            if status != 200:
                raise _rest_error(status, fields, body, path)
            if fields.get("content-type", "").split(";")[0].strip().lower() == "text/html":
                raise _Failed("blocked", "GitHub returned an HTML page")
            value = parse(body.decode())
            outcome = "no_results" if value == (0, []) else "ok"
            return value
        except (ValueError, TypeError, KeyError, AttributeError):
            raise _Failed(outcome, "GitHub returned an incomplete or malformed response") from None
        except CapReached:
            raise _Failed(outcome, "GitHub request cap reached") from None
        except NetworkError:
            outcome = "network_error"
            raise _Failed(outcome, "Could not read GitHub API response") from None
        except HttpError as failure:
            outcome = failure.code
            raise _Failed(outcome, str(failure)) from None
        except _Failed as failure:
            outcome = failure.code
            raise
        finally:
            self.attempts.append(env.attempt(self.backend, outcome, requests=self.client.requests_made - previous,
                                             ms=env.elapsed_ms(started)))


def _rest_error(status, headers, body, path):
    if status == 404 and path.startswith("repos/") and path.count("/") == 2:
        return _Failed("not_found", "The repository does not exist or is private. "
                       "Run `gh auth login` to read private repositories your account can access.")
    limited = status == 429 or (status == 403 and (
        headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers
        or b"rate limit" in body.lower() or b"abuse detection" in body.lower()))
    if limited:
        reset = headers.get("x-ratelimit-reset", "")
        when = "reset time unavailable"
        if reset.isascii() and reset.isdigit() and len(reset) <= 11:
            when = "resets at " + datetime.fromtimestamp(int(reset), UTC).isoformat()
        elif re.fullmatch(r"[0-9]{1,10}", headers.get("retry-after", "")):
            when = "retry after " + headers["retry-after"] + " seconds"
        return _Failed("rate_limited", f"GitHub public rate limit ({when}). Run `gh auth login` for a higher limit.")
    try:
        raise_refusal(status, body, "GitHub")
    except HttpError as refusal:
        return _Failed(refusal.code, str(refusal))
    return _Failed("not_found" if status == 404 else "upstream_error", f"GitHub returned HTTP {status}")


def classify(result: proc.Result) -> tuple[str, str]:
    """Error code and message for a failed gh call."""
    stderr = result.stderr.strip()
    low = stderr.lower()
    detail = stderr[:200]
    if "rate limit" in low or "http 429" in low:
        return "rate_limited", f"GitHub rate limit; stopped without retrying: {detail}"
    if "http 401" in low or "gh auth login" in low or "not logged" in low:
        return "config", "gh is not logged in to GitHub; run `gh auth login`"
    if "http 404" in low:
        return "not_found", f"GitHub returned 404: {detail}"
    message = f"gh exited with code {result.code}"
    return "upstream_error", f"{message}: {detail}" if detail else message


def run_repo(args, ctx) -> dict:
    requested = args.repo
    input_ = {"repo": requested}
    if not _OWNER_REPO.fullmatch(requested):
        return env.usage(CHANNEL, "repo", f"expected OWNER/REPO, got {requested!r}")

    return _route(ctx, "repo", input_, _read_repo, scoped=True, force=args.backend == BACKEND)


def _route(ctx, action, input_, read, *, scoped=False, force=False):
    gh = _Gh(ctx)
    public = None
    try:
        if scoped and gh.allow_public and not force and not ctx.environ.get("GH_HTTP_UNIX_SOCKET"):
            public = read(gh, input_, True)
            if public["ok"] and public["items"]:
                return public
            # An empty search can also be a repository the public search cannot see.
        gh.backend = BACKEND
        signed_start = len(gh.attempts)
        try:
            signed = read(gh, input_, False)
        except _NoLogin:
            if len(gh.attempts) != signed_start + 1:
                raise
            if public is not None:
                return public
            if force or not gh.allow_public:
                raise
            return read(gh, input_, True)
        if public is not None:
            error = public["error"]
            if error and error["code"] == "rate_limited":
                note = "Public GitHub allowance exhausted. Restarted with the gh login."
            else:
                reason = error["code"] if error else "no_results"
                note = f"Public GitHub returned {reason}. Restarted with the gh login."
            signed["meta"]["notes"].insert(0, note)
        return signed
    except _Failed as failure:
        return env.envelope(CHANNEL, action, input_, gh.backend, meta={"attempts": gh.attempts},
                            error=env.error(failure.code, failure.message))
    finally:
        gh.close()


def _read_repo(gh, input_, public):
    requested = input_["repo"]
    get = gh.public if public else gh.signed
    notes = []
    data = readme = release = error = None
    try:
        data = get(f"repos/{requested}", _repo_record)
        name = data["full_name"]
        if name.lower() != requested.lower():
            notes.append(f"renamed: {requested} -> {name}")
        readme = get(f"repos/{name}/readme", str, raw=True, missing_ok=True)
        release = get(f"repos/{name}/releases/latest", _release_record, missing_ok=True)
    except _NoLogin as failure:
        if data is None:
            raise
        error = env.error(failure.code, failure.message)
    except _Failed as failure:
        error = env.error(failure.code, failure.message)
    items = [] if data is None else [_repo_item(data, readme, release)]
    return env.envelope(CHANNEL, "repo", input_, gh.backend, items,
                        meta={"attempts": gh.attempts, "notes": notes}, error=error)


def _repo_item(data: dict, readme, release) -> dict:
    parts = [p for p in (data.get("description"), readme[:README_CHARS] if readme else None) if p]
    return env.item(
        id=data["full_name"],
        url=data.get("html_url"),
        title=data["full_name"],
        author=(data.get("owner") or {}).get("login"),
        published_at=data.get("pushed_at"),
        text="\n\n".join(parts) or None,
        source="gh",
        engagement=_counts(stars=data.get("stargazers_count"), forks=data.get("forks_count"),
                           open_issues=data.get("open_issues_count"),
                           watchers=data.get("subscribers_count")),
        raw={"repo": data, "latest_release": release},
    )


def run_search(args, ctx) -> dict:
    input_ = {"query": args.query, "limit": args.limit}
    problem = _check_query(args.query, args.limit)
    if problem:
        return env.usage(CHANNEL, "search", problem)
    argv = ["gh", "search", "repos", "--limit", str(args.limit), "--json", REPO_FIELDS, "--", args.query]
    return _search(ctx, "search", input_, argv, _search_repo_item)


def run_issues(args, ctx) -> dict:
    input_ = {"query": args.query, "repo": args.repo, "limit": args.limit, "state": args.state}
    if args.repo is not None and not _OWNER_REPO.fullmatch(args.repo):
        return env.usage(CHANNEL, "issues", f"--repo expects OWNER/REPO, got {args.repo!r}")
    if not args.query.strip() and not args.repo:
        return env.usage(CHANNEL, "issues", "pass a query or --repo OWNER/REPO")
    if not 1 <= args.limit <= MAX_LIMIT:
        return env.usage(CHANNEL, "issues", f"--limit must be between 1 and {MAX_LIMIT}")
    argv = ["gh", "search", "issues", "--limit", str(args.limit), "--json", ISSUE_FIELDS]
    if _has_issue_type(args.query):
        argv.append("--include-prs")
    if args.repo:
        argv += ["--repo", args.repo]
    if args.state != "all":
        argv += ["--state", args.state]
    if args.query.strip():
        argv += ["--", args.query]
    # A public issue can belong to a private project or mention a team visible to the viewer.
    scoped = bool(args.repo) and not re.search(
        r"@me\b|\b(?:repo|project|team|review-requested|team-review-requested):|\bno:project\b", args.query, re.I)
    return _search(ctx, "issues", input_, argv, _issue_item, scoped=scoped, force=args.backend == BACKEND)


def _has_issue_type(query: str) -> bool:
    return any(_ISSUE_TYPE.fullmatch(token) for token in _QUERY_TOKENS.findall(query))


def _check_query(query: str, limit: int) -> str | None:
    if not query.strip():
        return "QUERY is empty"
    if not 1 <= limit <= MAX_LIMIT:
        return f"--limit must be between 1 and {MAX_LIMIT}"
    return None


def _repo_record(stdout: str) -> dict:
    data = json.loads(stdout)
    required = {"html_url", "description", "owner", "stargazers_count", "forks_count", "open_issues_count", "pushed_at"}
    if not isinstance(data, dict) or not required.issubset(data) or not isinstance(data.get("full_name"), str):
        raise ValueError("incomplete repository record")
    if not _OWNER_REPO.fullmatch(data["full_name"]):
        raise ValueError("not a repository record")
    # GitHub adds a short-lived clone credential to a private repository's record.
    data.pop("temp_clone_token", None)
    return data


def _release_record(stdout):
    release = json.loads(stdout)
    if not isinstance(release, dict) or not release.get("tag_name"):
        raise ValueError("not a release")
    return release


def _user(user):
    if user is None:
        return None
    return {"id": user["node_id"], "login": user["login"], "type": user["type"],
            "is_bot": user["type"] == "Bot", "url": user["html_url"]}


def _public_repo(r):
    return dict(fullName=r["full_name"], url=r["html_url"], description=r["description"] or "",
                owner=_user(r["owner"]), pushedAt=r["pushed_at"], stargazersCount=r["stargazers_count"],
                forksCount=r["forks_count"], openIssuesCount=r["open_issues_count"], watchersCount=r["watchers_count"],
                language=r["language"] or "", isArchived=r["archived"], isFork=r["fork"],
                createdAt=r["created_at"], updatedAt=r["updated_at"])


def _public_issue(r):
    repo = r["repository_url"].removeprefix("https://api.github.com/repos/")
    pr = r["html_url"] == f"https://github.com/{repo}/pull/{r['number']}"
    if not _OWNER_REPO.fullmatch(repo) or (not pr and r["html_url"] != f"https://github.com/{repo}/issues/{r['number']}"):
        raise ValueError("not a GitHub issue or pull request")
    labels = [dict(id=label["node_id"], name=label["name"], color=label["color"], description=label["description"] or "")
              for label in r["labels"]]
    record = dict(
        repository={"name": repo.split("/")[1], "nameWithOwner": repo},
        number=r["number"], url=r["html_url"], title=r["title"], author=_user(r["user"]),
        createdAt=r["created_at"], updatedAt=r["updated_at"], closedAt=r["closed_at"] or "0001-01-01T00:00:00Z",
        state=r["state"], body=r["body"] or "", commentsCount=r["comments"], labels=labels, isPullRequest=pr,
    )
    pull_request = r.get("pull_request")
    if pr and isinstance(pull_request, dict) and "merged_at" in pull_request:
        record["mergedAt"] = pull_request["merged_at"]
    return record


def _rest_search(gh, action, args, *, authenticated=False):
    query = args["query"]
    limit = args["limit"]
    endpoint, convert = "repositories", _public_repo
    if action == "issues":
        endpoint, convert = "issues", _public_issue
        parts = [f"( {query} )"] if query.strip() else []
        if args["repo"]:
            parts.append("repo:" + args["repo"])
        if args["state"] != "all":
            parts.append("state:" + args["state"])
        if not _has_issue_type(query):
            parts.append("is:issue")
        query = " ".join(parts)

    def page(stdout):
        data = json.loads(stdout)
        if data["incomplete_results"] is not False or not isinstance(data["items"], list):
            raise ValueError("incomplete search")
        total = data["total_count"]
        if type(total) is not int or total < 0:
            raise ValueError("invalid total")
        return total, [convert(r) for r in data["items"]]

    records = []
    seen = set()
    size = min(100, limit)
    for number in range(1, (limit + size - 1) // size + 1):
        params = urlencode({"q": query, "per_page": size, "page": number})
        path = f"search/{endpoint}?{params}"
        if action == "issues":
            path += "&advanced_search=true"
        if authenticated:
            total, rows = gh.call(["gh", "api", path], parse=page)
        else:
            total, rows = gh.public(path, page)
        records.extend(rows)
        seen.update(r["url"] for r in rows)
        if len(seen) != len(records) or len(records) > total or len(records) < min(number * size, total, limit):
            gh.attempts[-1]["outcome"] = "upstream_error"
            raise _Failed("upstream_error", "GitHub returned an incomplete search page")
        if len(records) >= min(total, limit):
            break
    return records[:limit]


def _record_list(stdout: str) -> list[dict]:
    records = json.loads(stdout)
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ValueError("not a list of records")
    return records


def _search(ctx, action, input_, argv, to_item, *, scoped=False, force=False):
    def read(gh, input_, public):
        try:
            if public or (action == "issues" and gh.allow_public):
                records = _rest_search(gh, action, input_, authenticated=not public)
            else:
                records = gh.call(argv, parse=_record_list)
        except _NoLogin:
            raise
        except _Failed as failure:
            return env.envelope(CHANNEL, action, input_, gh.backend, meta={"attempts": gh.attempts},
                                error=env.error(failure.code, failure.message))
        return env.envelope(CHANNEL, action, input_, gh.backend, [to_item(r) for r in records],
                            meta={"attempts": gh.attempts})

    return _route(ctx, action, input_, read, scoped=scoped, force=force)


def _search_repo_item(r: dict) -> dict:
    return env.item(
        id=r.get("fullName"),
        url=r.get("url"),
        title=r.get("fullName"),
        author=(r.get("owner") or {}).get("login"),
        published_at=r.get("pushedAt"),
        text=r.get("description") or None,
        source="gh",
        engagement=_counts(stars=r.get("stargazersCount"), forks=r.get("forksCount"),
                           open_issues=r.get("openIssuesCount")),
        raw=r,
    )


def _issue_item(r: dict) -> dict:
    repo = (r.get("repository") or {}).get("nameWithOwner")
    number = r.get("number")
    return env.item(
        id=f"{repo}#{number}" if repo and number is not None else None,
        url=r.get("url"),
        title=r.get("title"),
        author=(r.get("author") or {}).get("login"),
        published_at=r.get("createdAt"),
        text=r.get("body") or None,
        source="gh",
        engagement=_counts(comments=r.get("commentsCount")),
        raw=r,
    )


def _counts(**counts) -> dict | None:
    """Only the integer counts the backend reported; None if there are none."""
    kept = {k: v for k, v in counts.items() if isinstance(v, int) and not isinstance(v, bool)}
    return kept or None
