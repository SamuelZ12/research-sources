"""GitHub via the `gh` CLI: `repo`, `search` (repositories) and `issues`.

Every call goes through `ctx.runner or proc.run`. rsrc never reads or prints the gh
token; `gh` uses its own stored login. A rate-limit reply stops the command at once.
"""

import json
import re
import time

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
    gh = subparsers.add_parser("gh", help="GitHub repositories and issues via the gh CLI")
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


class _Gh:
    """Runs gh calls and records one meta.attempts entry per call."""

    def __init__(self, ctx):
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
            raise _Failed("config", "gh not installed (brew install gh)") from None
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
            raise _Failed(code, message)
        try:
            value = parse(result.stdout)
        except ValueError:
            self._record("upstream_error", ms)
            raise _Failed("upstream_error", "gh returned malformed or unexpected JSON") from None
        self._record("no_results" if value == [] else "ok", ms)
        return value


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
        data = gh.call(["gh", "api", f"repos/{requested}"], parse=_repo_record)
        name = data["full_name"]
        if name.lower() != requested.lower():
            notes.append(f"renamed: {requested} -> {name}")
        readme = gh.call(["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{name}/readme"],
                         parse=str, missing_ok=True)
        release = gh.call(["gh", "api", f"repos/{name}/releases/latest"], missing_ok=True)
    except _Failed as failure:
        items = [] if data is None else [_repo_item(data, readme, release)]
        return env.envelope(CHANNEL, "repo", input_, BACKEND, items,
                            meta={"attempts": gh.attempts, "notes": notes},
                            error=env.error(failure.code, failure.message))

    return env.envelope(CHANNEL, "repo", input_, BACKEND, [_repo_item(data, readme, release)],
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
    if not isinstance(data, dict) or not isinstance(data.get("full_name"), str):
        raise ValueError("not a repository record")
    return data


def _record_list(stdout: str) -> list[dict]:
    records = json.loads(stdout)
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ValueError("not a list of records")
    return records


def _search(ctx, action, input_, argv, to_item) -> dict:
    gh = _Gh(ctx)
    try:
        records = gh.call(argv, parse=_record_list)
    except _Failed as failure:
        return env.envelope(CHANNEL, action, input_, BACKEND, [], meta={"attempts": gh.attempts},
                            error=env.error(failure.code, failure.message))
    return env.envelope(CHANNEL, action, input_, BACKEND, [to_item(r) for r in records],
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
