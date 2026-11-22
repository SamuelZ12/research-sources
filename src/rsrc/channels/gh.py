"""GitHub repositories and issues via gh or the anonymous REST API."""

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from urllib.parse import urlencode

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

_OWNER_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+")


def register(subparsers) -> None:
    gh = subparsers.add_parser("gh", help="GitHub repositories and issues")
    actions = gh.add_subparsers(dest="gh_action", metavar="ACTION", required=True)

    repo = actions.add_parser("repo", help="one repository: stats, latest release, README")
    repo.add_argument("repo", metavar="OWNER/REPO")
    repo.set_defaults(run=run_repo, action="repo")

    search = actions.add_parser("search", help="search repositories")
    search.add_argument("query", metavar="QUERY")
    search.add_argument("--limit", type=int, default=20)
    search.set_defaults(run=run_search, action="search")

    issues = actions.add_parser("issues", help="search issues")
    issues.add_argument("query", metavar="QUERY")
    issues.add_argument("--repo", metavar="OWNER/REPO")
    issues.add_argument("--limit", type=int, default=30)
    issues.add_argument("--state", choices=("open", "closed", "all"), default="open")
    issues.set_defaults(run=run_issues, action="issues")


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
        self.backend = BACKEND
        self.allow_public = ctx.environ.get("GH_HOST", "github.com").lower() == "github.com"
        self.runner = ctx.runner or proc.run
        self.attempts: list[dict] = []

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

    def public(self, path, parse, *, raw=False, missing_ok=False):
        self.backend = "github-rest"
        started = time.monotonic()
        outcome = "upstream_error"
        try:
            request = urllib.request.Request("https://api.github.com/" + path, headers={
                "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
                "User-Agent": "research-sources",
            })
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                if response.headers.get_content_type() == "text/html":
                    raise _Failed("blocked", "GitHub returned an HTML page")
                value = parse(response.read().decode())
            outcome = "no_results" if value == (0, []) else "ok"
            return value
        except urllib.error.HTTPError as exc:
            with exc:
                if exc.code == 404 and missing_ok:
                    outcome = "no_results"
                    return None
                failure = _public_error(exc)
                outcome = failure.code
                raise failure from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise _Failed(outcome, "GitHub returned an incomplete or malformed response") from None
        except (OSError, http.client.HTTPException):
            outcome = "network_error"
            raise _Failed(outcome, "Could not read GitHub API response") from None
        except _Failed as failure:
            outcome = failure.code
            raise
        finally:
            self.attempts.append(env.attempt(self.backend, outcome, requests=1, ms=env.elapsed_ms(started)))


def _public_error(exc):
    limited = exc.code == 429 or (exc.code == 403 and (
        exc.headers.get("X-RateLimit-Remaining") == "0" or
        "Retry-After" in exc.headers or b"rate limit" in exc.read().lower()))
    if limited:
        reset = exc.headers.get("X-RateLimit-Reset", "")
        when = "reset time unavailable"
        if reset.isdigit() and len(reset) <= 11:
            when = "resets at " + datetime.fromtimestamp(int(reset), UTC).isoformat()
        elif exc.headers.get("Retry-After"):
            when = "retry after " + exc.headers["Retry-After"] + " seconds"
        return _Failed("rate_limited", f"GitHub public rate limit ({when}). Run `gh auth login` for a higher limit.")
    code = {401: "auth_failed", 403: "blocked", 404: "not_found"}.get(exc.code, "upstream_error")
    return _Failed(code, f"GitHub returned HTTP {exc.code}")


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
    if not _OWNER_REPO.fullmatch(requested) or requested.split("/")[1] in (".", ".."):
        return env.usage(CHANNEL, "repo", f"expected OWNER/REPO, got {requested!r}")

    gh = _Gh(ctx)
    notes: list[str] = []
    data = readme = release = None
    try:
        path = f"repos/{requested}"
        try:
            data = gh.call(["gh", "api", path], parse=_repo_record)
        except _NoLogin:
            if not gh.allow_public:
                raise
            data = gh.public(path, _repo_record)
        name = data["full_name"]
        if name.lower() != requested.lower():
            notes.append(f"renamed: {requested} -> {name}")
        readme_path = f"repos/{name}/readme"
        release_path = f"repos/{name}/releases/latest"
        if gh.backend == BACKEND:
            readme = gh.call(["gh", "api", "-H", "Accept: application/vnd.github.raw", readme_path],
                             parse=str, missing_ok=True)
            release = gh.call(["gh", "api", release_path], parse=_release_record, missing_ok=True)
        else:
            readme = gh.public(readme_path, str, raw=True, missing_ok=True)
            release = gh.public(release_path, _release_record, missing_ok=True)
    except _Failed as failure:
        items = [] if data is None else [_repo_item(data, readme, release)]
        return env.envelope(CHANNEL, "repo", input_, gh.backend, items,
                            meta={"attempts": gh.attempts, "notes": notes},
                            error=env.error(failure.code, failure.message))

    return env.envelope(CHANNEL, "repo", input_, gh.backend, [_repo_item(data, readme, release)],
                        meta={"attempts": gh.attempts, "notes": notes})


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
                           watchers=data.get("subscribers_count", data.get("watchers_count"))),
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
    problem = _check_query(args.query, args.limit)
    if not problem and args.repo is not None and not _OWNER_REPO.fullmatch(args.repo):
        problem = f"--repo expects OWNER/REPO, got {args.repo!r}"
    if problem:
        return env.usage(CHANNEL, "issues", problem)
    argv = ["gh", "search", "issues", "--limit", str(args.limit), "--json", ISSUE_FIELDS]
    if args.repo:
        argv += ["--repo", args.repo]
    if args.state != "all":
        argv += ["--state", args.state]
    argv += ["--", args.query]
    return _search(ctx, "issues", input_, argv, _issue_item)


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
    if not _OWNER_REPO.fullmatch(data["full_name"]) or data["full_name"].split("/")[1] in (".", ".."):
        raise ValueError("not a repository record")
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
    if not _OWNER_REPO.fullmatch(repo) or r["html_url"] != f"https://github.com/{repo}/issues/{r['number']}":
        raise ValueError("not a GitHub issue")
    labels = [dict(id=label["node_id"], name=label["name"], color=label["color"], description=label["description"] or "")
              for label in r["labels"]]
    return dict(repository={"name": repo.split("/")[1], "nameWithOwner": repo},
                number=r["number"], url=r["html_url"], title=r["title"], author=_user(r["user"]),
                createdAt=r["created_at"], updatedAt=r["updated_at"], closedAt=r["closed_at"] or "0001-01-01T00:00:00Z",
                state=r["state"], body=r["body"] or "", commentsCount=r["comments"], labels=labels, isPullRequest=False)


def _rest_search(gh, action, args, *, authenticated=False):
    query = args["query"]
    limit = args["limit"]
    endpoint, convert = "repositories", _public_repo
    if action == "issues":
        endpoint, convert = "issues", _public_issue
        query = f"( {query} )"
        if args["repo"]:
            query += " repo:" + args["repo"]
        if args["state"] != "all":
            query += " state:" + args["state"]
        query += " type:issue"

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
            raise _Failed("upstream_error", "GitHub returned an incomplete search page")
        if len(records) >= min(total, limit):
            break
    return records[:limit]


def _record_list(stdout: str) -> list[dict]:
    records = json.loads(stdout)
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ValueError("not a list of records")
    return records


def _search(ctx, action, input_, argv, to_item) -> dict:
    gh = _Gh(ctx)
    try:
        try:
            if action == "issues" and gh.allow_public:
                records = _rest_search(gh, action, input_, authenticated=True)
            else:
                records = gh.call(argv, parse=_record_list)
        except _NoLogin:
            if not gh.allow_public or len(gh.attempts) != 1:
                raise
            records = _rest_search(gh, action, input_)
    except _Failed as failure:
        return env.envelope(CHANNEL, action, input_, gh.backend, [], meta={"attempts": gh.attempts},
                            error=env.error(failure.code, failure.message))
    return env.envelope(CHANNEL, action, input_, gh.backend, [to_item(r) for r in records],
                        meta={"attempts": gh.attempts})


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
                           open_issues=r.get("openIssuesCount"), watchers=r.get("watchersCount")),
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
