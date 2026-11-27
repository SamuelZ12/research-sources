import json
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlsplit


import pytest

from rsrc.channels.x.search import default_until, windows

ENV = {"AUTH_TOKEN": "TOKENSENTINEL", "CT0": "CSRFSENTINEL", "RSRC_X_ENV": "/nonexistent"}


def post(post_id, created=None):
    legacy = {"full_text": f"post {post_id}"}
    if created:
        legacy["created_at"] = created.strftime("%a %b %d %H:%M:%S +0000 %Y")
    return {"rest_id": post_id, "core": {"user_results": {"result": {"core": {"screen_name": "a"}}}},
            "legacy": legacy}


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class FakeSearch:
    """X SearchTimeline stand-in. `pages(query, cursor, count) -> (ids, next_cursor)`; an id may be
    an `(id, created_datetime)` pair."""

    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def __call__(self, method, url, headers, body):
        variables = json.loads(parse_qs(urlsplit(url).query)["variables"][0])
        self.requests.append(variables)
        ids, cursor = self.pages(variables["rawQuery"], variables.get("cursor"), variables["count"])
        entries = [{"content": {"itemContent": {"tweet_results": {"result": post(*i if isinstance(i, tuple) else (i,))}}}}
                   for i in ids]
        if cursor:
            entries.append({"content": {"cursorType": "Bottom", "value": cursor}})
        data = {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {
            "instructions": [{"type": "TimelineAddEntries", "entries": entries}]}}}}}
        return 200, json.dumps(data).encode()


@pytest.fixture
def run(run_x, tmp_path):
    """`rsrc x search --backend x-graphql ARGV`: returns (exit code, item ids in order, envelope)."""
    def run(argv, fake):
        code, env, _ = run_x(["search", "--backend", "x-graphql", *argv], fake,
                             {**ENV, "XDG_CACHE_HOME": str(tmp_path)})
        return code, [i["id"] for i in env["items"]], env

    return run


# --- window arithmetic ---------------------------------------------------------

def test_windows_are_newest_first_with_short_final_window():
    assert windows(date(2026, 10, 6), date(2026, 11, 5), 7) == [
        (date(2026, 10, 29), date(2026, 11, 5)),
        (date(2026, 10, 22), date(2026, 10, 29)),
        (date(2026, 10, 15), date(2026, 10, 22)),
        (date(2026, 10, 8), date(2026, 10, 15)),
        (date(2026, 10, 6), date(2026, 10, 8)),
    ]


def test_window_larger_than_range_is_one_window():
    assert windows(date(2026, 11, 1), date(2026, 11, 3), 7) == [(date(2026, 11, 1), date(2026, 11, 3))]


def test_until_defaults_to_tomorrow_utc_so_today_is_included():
    late_evening_toronto = datetime(2026, 11, 6, 3, 30, tzinfo=timezone.utc)
    assert default_until(late_evening_toronto) == date(2026, 11, 7)


# --- `rsrc x search` windowing -------------------------------------------------------------

def test_each_window_queries_its_utc_range_newest_first(run):
    # X ignores until: (seen live) but honors until_time:, so bounds are UTC midnights in Unix seconds.
    fake = FakeSearch(lambda q, cursor, count: ([q[-10:]], None))
    code, ids, _ = run(['"Letta Code"', "--since", "2026-10-20", "--until", "2026-11-05"], fake)
    assert code == 0
    assert [r["rawQuery"] for r in fake.requests] == [
        '"Letta Code" since_time:1793232000 until_time:1793836800',
        '"Letta Code" since_time:1792627200 until_time:1793232000',
        '"Letta Code" since_time:1792454400 until_time:1792627200',
    ]


def test_until_alone_adds_until_time_without_windowing(run):
    fake = FakeSearch(lambda q, cursor, count: (["1"], None))
    run(["q", "--until", "2026-11-05"], fake)
    assert [r["rawQuery"] for r in fake.requests] == ["q until_time:1793836800"]


def test_window_pages_until_per_window_limit(run):
    counter = iter(range(1000))
    fake = FakeSearch(lambda q, cursor, count: ([str(next(counter)) for _ in range(count)], f"c{cursor}"))
    _, ids, _ = run(["q", "--per-window", "30"], fake)
    assert [r["count"] for r in fake.requests] == [20, 10]
    assert len(ids) == 30


def test_paging_stops_when_cursor_repeats(run):
    fake = FakeSearch(lambda q, cursor, count: ([f"{cursor}-1"], "same"))
    run(["q", "--per-window", "100"], fake)
    assert len(fake.requests) == 2


def test_paging_stops_when_page_adds_nothing_new(run):
    fake = FakeSearch(lambda q, cursor, count: (["1", "2"], f"c{cursor}"))
    _, ids, _ = run(["q", "--per-window", "100"], fake)
    assert ids == ["1", "2"]
    assert len(fake.requests) == 2


def test_posts_are_deduplicated_across_windows(run):
    fake = FakeSearch(lambda q, cursor, count: (["dup", q[-10:]], None))
    _, ids, _ = run(["q", "--since", "2026-10-29", "--until", "2026-11-05", "--window-days", "3"], fake)
    assert ids.count("dup") == 1
    assert len(ids) == 4


def test_request_cap_stops_cleanly_keeping_printed_posts(run):
    fake = FakeSearch(lambda q, cursor, count: ([q[-10:]], None))
    code, ids, env = run(["q", "--since", "2026-10-01", "--until", "2026-11-05", "--max-requests", "2"],
                         fake)
    assert code == 0
    assert len(fake.requests) == 2 and len(ids) == 2
    assert env["ok"] is True and env["meta"]["truncated"] == "request_cap"


def test_posts_outside_the_window_are_dropped_and_paging_continues(run):
    # X has ignored until: near today and returned only the newest posts.
    pages = {None: ([("a", utc(2026, 11, 6, 3)), ("b", utc(2026, 11, 6, 2))], "c1"),
             "c1": ([("c", utc(2026, 11, 4, 12)), ("d", utc(2026, 10, 31, 12))], None)}
    fake = FakeSearch(lambda q, cursor, count: pages[cursor])
    code, ids, env = run(["q", "--since", "2026-11-01", "--until", "2026-11-05"], fake)
    assert code == 0 and ids == ["c"]
    assert env["meta"]["notes"] == ["3 post(s) outside their date window dropped"]


def test_window_bounds_are_utc_days_with_exclusive_until(run):
    edge = [("first", utc(2026, 11, 1, 0, 0, 0)), ("last", utc(2026, 11, 4, 23, 59, 59)),
            ("after", utc(2026, 11, 5, 0, 0, 0)), ("before", utc(2026, 10, 31, 23, 59, 59))]
    fake = FakeSearch(lambda q, cursor, count: (edge, None))
    _, ids, _ = run(["q", "--since", "2026-11-01", "--until", "2026-11-05"], fake)
    assert ids == ["first", "last"]


def test_post_dropped_from_one_window_is_kept_by_its_own_window(run):
    fake = FakeSearch(lambda q, cursor, count: ([("x", utc(2026, 10, 31, 12))], None))
    _, ids, _ = run(["q", "--since", "2026-10-29", "--until", "2026-11-05", "--window-days", "3"], fake)
    assert ids == ["x"]


def test_until_alone_drops_newer_posts(run):
    fake = FakeSearch(lambda q, cursor, count: ([("new", utc(2026, 11, 6)), ("old", utc(2026, 1, 1))], None))
    _, ids, _ = run(["q", "--until", "2026-11-05"], fake)
    assert ids == ["old"]


def test_query_with_its_own_dates_conflicts_with_since(run):
    fake = FakeSearch(lambda q, cursor, count: ([], None))
    code, _, env = run(["q since:2026-01-01", "--since", "2026-10-01"], fake)
    assert code == 2 and fake.requests == []
    assert env["error"]["code"] == "usage" and "since:" in env["error"]["message"]


def test_query_with_its_own_timestamps_conflicts_with_until(run):
    fake = FakeSearch(lambda q, cursor, count: ([], None))
    code, _, env = run(["q until_time:1793836800", "--until", "2026-11-05"], fake)
    assert code == 2 and fake.requests == []
    assert env["error"]["code"] == "usage" and "until_time:" in env["error"]["message"]


def test_since_must_be_before_until(run):
    fake = FakeSearch(lambda q, cursor, count: ([], None))
    code, _, env = run(["q", "--since", "2026-11-05", "--until", "2026-11-05"], fake)
    assert code == 2 and fake.requests == []
    assert env["error"] == {"code": "usage", "message": "--since must be before --until"}


def test_bad_date_is_a_usage_error(run):
    fake = FakeSearch(lambda q, cursor, count: ([], None))
    code, _, env = run(["q", "--since", "2026-13-01"], fake)
    assert code == 2 and fake.requests == [] and env["error"]["code"] == "usage"


def test_limit_and_remaining_page_size(run):
    ids = iter(range(80))
    fake = FakeSearch(lambda q, c, n: ([str(next(ids)) for _ in range(n)], f"{c}x"))
    _, posts, doc = run(["q", "--limit", "30"], fake)
    assert len(posts) == 30 and [r["count"] for r in fake.requests] == [20, 10]
    assert not doc["meta"]["notes"]


def test_limit_across_windows(run):
    fake = FakeSearch(lambda q, c, n: ([f"{q}-{i}" for i in range(n)], None))
    _, posts, _ = run(["q", "--limit", "5", "--per-window", "3", "--since", "2026-10-01",
                       "--until", "2026-11-01"], fake)
    assert len(posts) == 5 and [r["count"] for r in fake.requests] == [3, 2]


@pytest.mark.parametrize(("options", "reason"), [
    (["--per-window", "2"], "per-window limit"),
    (["--max-requests", "1"], "request cap"),
    ([], "backend repeated its cursor")])
def test_short_limit_explains_why(run, options, reason):
    fake = FakeSearch(lambda q, c, n: ([f"{c}-1", f"{c}-2"], "same"))
    _, posts, doc = run(["q", "--limit", "30", *options], fake)
    assert len(posts) < 30 and reason in doc["meta"]["notes"][-1]
