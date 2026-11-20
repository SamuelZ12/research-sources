import json
from urllib.parse import urlsplit

import pytest

from rsrc.http import Client
from rsrc.channels.x.search import QUERY_IDS, SearchError, Searcher

COOKIES = ("TOKENSENTINEL", "CSRFSENTINEL")
QUERY_ID_CACHE = "query-ids.json"
ONE_POST = {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {"instructions": [{"entries": [
    {"content": {"itemContent": {"tweet_results": {"result": {
        "rest_id": "1", "core": {"user_results": {"result": {"core": {"screen_name": "a"}}}},
        "legacy": {"full_text": "t"}}}}}}]}]}}}}}
VALIDATION_FAILED = {"errors": [{"message": "bad", "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}]}


class FakeX:
    """Serves SearchTimeline responses per query ID; any other URL is a test failure."""

    def __init__(self, working_ids=(), search_status=404):
        self.working_ids = set(working_ids)
        self.search_status = search_status
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((url, headers or {}))
        parts = urlsplit(url)
        if parts.path.endswith("/SearchTimeline"):
            query_id = parts.path.split("/")[-2]
            if query_id in self.working_ids:
                return 200, json.dumps(ONE_POST).encode()
            if self.search_status == 400:
                return 400, json.dumps(VALIDATION_FAILED).encode()
            return 404, b""
        raise AssertionError(f"unexpected request: {url}")

    def search_ids(self):
        return [urlsplit(u).path.split("/")[-2] for u, _ in self.calls if u.split("?")[0].endswith("/SearchTimeline")]


def searcher(fake, tmp_path, max_requests=None):
    client = Client(transport=fake, min_interval=0, max_requests=max_requests)
    return Searcher(client, COOKIES, tmp_path / "query-ids.json")


def test_cached_id_is_tried_first(tmp_path):
    (tmp_path / "query-ids.json").write_text(json.dumps({"SearchTimeline": "CACHED"}))
    fake = FakeX(working_ids={"CACHED"})
    searcher(fake, tmp_path).page("q")
    assert fake.search_ids() == ["CACHED"]


def test_falls_through_baked_ids_and_caches_the_one_that_works(tmp_path):
    fake = FakeX(working_ids={QUERY_IDS[2]})
    records, _ = searcher(fake, tmp_path).page("q")
    assert [r["id"] for r in records] == ["1"]
    assert fake.search_ids() == list(QUERY_IDS[:3])
    assert json.loads((tmp_path / "query-ids.json").read_text()) == {"SearchTimeline": QUERY_IDS[2]}


def test_working_id_is_reused_for_later_pages(tmp_path):
    fake = FakeX(working_ids={QUERY_IDS[1]})
    s = searcher(fake, tmp_path)
    s.page("q")
    s.page("q", cursor="c")
    assert fake.search_ids() == [QUERY_IDS[0], QUERY_IDS[1], QUERY_IDS[1]]


def test_all_ids_rejected_explains_the_manual_fix_without_other_requests(tmp_path):
    fake = FakeX(working_ids=set())
    with pytest.raises(SearchError) as e:
        searcher(fake, tmp_path).page("q")
    message = str(e.value)
    assert "every SearchTimeline query ID was rejected" in message
    assert "/i/api/graphql/<id>/SearchTimeline" in message
    assert str(tmp_path / QUERY_ID_CACHE) in message
    assert fake.search_ids() == list(QUERY_IDS)
    assert not (tmp_path / QUERY_ID_CACHE).exists()


def test_hand_written_cache_id_recovers_after_rotation(tmp_path):
    fake = FakeX(working_ids={"NEW_ID-1"})
    with pytest.raises(SearchError):
        searcher(fake, tmp_path).page("q")
    (tmp_path / QUERY_ID_CACHE).write_text('{"SearchTimeline": "NEW_ID-1"}\n')
    records, _ = searcher(fake, tmp_path).page("q")
    assert [r["id"] for r in records] == ["1"]
    assert fake.search_ids()[-1] == "NEW_ID-1"


def test_corrupt_cache_is_ignored(tmp_path):
    (tmp_path / "query-ids.json").write_text("{not json")
    fake = FakeX(working_ids={QUERY_IDS[0]})
    searcher(fake, tmp_path).page("q")
    assert fake.search_ids() == [QUERY_IDS[0]]


def test_unwritable_cache_does_not_break_search(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    fake = FakeX(working_ids={QUERY_IDS[1]})
    client = Client(transport=fake, min_interval=0)
    records, _ = Searcher(client, COOKIES, locked / "rsrc" / "x-query-ids.json").page("q")
    assert [r["id"] for r in records] == ["1"]
