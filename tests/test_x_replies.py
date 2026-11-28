import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = "2117770299454177696"


def conversation():
    return json.loads((FIXTURES / "x_fx_conversation.json").read_text())


def extra_page(*ids, cursor=None):
    replies = [{"id": i, "text": f"reply {i}", "author": {"screen_name": "r"}, "replying_to": {"status": ROOT}}
               for i in ids]
    return {"code": 200, "replies": replies, "cursor": {"bottom": cursor} if cursor else {}}


class FakeFx:
    def __init__(self, pages):
        self.pages = list(pages)
        self.urls = []

    def __call__(self, method, url, headers, body):
        self.urls.append(url)
        status, data = self.pages.pop(0)
        return status, json.dumps(data).encode()

    def params(self, n):
        return {k: v[0] for k, v in parse_qs(urlsplit(self.urls[n]).query).items()}


@pytest.fixture
def run(run_x):
    """`rsrc x replies ARGV`: returns (exit code, the reply records, envelope)."""
    def run(argv, fake):
        code, env, _ = run_x(["replies", *argv], fake)
        return code, [i["raw"] for i in env["items"]], env

    return run


def test_replies_are_tied_to_the_root_and_exclude_it(run):
    fake = FakeFx([(200, conversation()), (200, extra_page())])
    code, lines, _ = run([f"https://x.com/bkdgiffug/status/{ROOT}"], fake)
    assert code == 0
    assert len(lines) == 14
    assert all(r["reply_to_id"] == ROOT and r["id"] != ROOT for r in lines)
    assert all(r["source"] == "fxtwitter" for r in lines)


def test_rejects_another_conversation_even_when_no_replies(run):
    for rows in (conversation()["replies"], []):
        code, lines, env = run(["1886192184808149383"], FakeFx([(200, conversation() | {"replies": rows})]))
        assert code == 1 and env["error"]["code"] == "url_mismatch" and not lines


def test_reply_chains_are_skipped_and_do_not_use_the_limit(run):
    page = extra_page("101", "102", "103", "106")
    page["replies"][1]["replying_to"]["status"] = "101"
    page["replies"][2]["replying_to"]["status"] = "102"
    page["replies"][3]["replying_to"]["status"] = "777"  # a reply to a deleted reply
    later = extra_page("104")
    later["replies"].insert(0, {"id": "105", "text": "reply 105", "author": {"screen_name": "r"},
                                "replying_to": {"status": "103"}})
    page["cursor"] = {"bottom": "more"}
    code, lines, _ = run([ROOT, "--limit", "2"], FakeFx([(200, page), (200, later)]))
    assert code == 0
    assert [r["id"] for r in lines] == ["101", "104"]


def test_wrong_parent_on_a_later_page_keeps_only_verified_replies(run):
    first = extra_page("101", cursor="more")
    second = extra_page("102")
    second["replies"][0]["replying_to"]["status"] = "999"
    code, lines, env = run([ROOT], FakeFx([(200, first), (200, second)]))
    assert code == 1 and env["error"]["code"] == "url_mismatch"
    assert [r["id"] for r in lines] == ["101"]


def test_reply_with_a_photo_lists_it_in_media(run):
    page = extra_page("1")
    page["replies"][0]["media"] = {"all": [{"type": "photo", "url": "https://pbs.twimg.com/media/r.jpg"}]}
    code, [reply], _ = run([ROOT], FakeFx([(200, page)]))
    assert code == 0
    assert reply["media"] == [{"type": "photo", "url": "https://pbs.twimg.com/media/r.jpg", "duration": None}]


def test_ranking_defaults_to_likes_and_accepts_recency(run):
    fake = FakeFx([(200, extra_page("1"))])
    run([ROOT], fake)
    assert fake.params(0)["ranking_mode"] == "likes"
    fake = FakeFx([(200, extra_page("1"))])
    run([ROOT, "--rank", "recency"], fake)
    assert fake.params(0)["ranking_mode"] == "recency"


def test_follows_cursor_to_next_page(run):
    first = conversation()
    fake = FakeFx([(200, first), (200, extra_page("901", "902"))])
    _, lines, _ = run([ROOT], fake)
    assert len(lines) == 16
    assert fake.params(1)["cursor"] == first["cursor"]["bottom"]


def test_limit_stops_output_and_paging(run):
    fake = FakeFx([(200, conversation())])
    _, lines, _ = run([ROOT, "--limit", "5"], fake)
    assert len(lines) == 5 and len(fake.urls) == 1


def test_page_with_no_new_replies_stops_paging(run):
    first = conversation()
    repeat = extra_page(*[r["id"] for r in first["replies"]], cursor="a-new-cursor")
    fake = FakeFx([(200, first), (200, repeat), (200, extra_page("999"))])
    _, lines, _ = run([ROOT], fake)
    assert len(lines) == 14 and len(fake.urls) == 2


def test_rate_limit_stops_immediately_with_stop_message(run):
    fake = FakeFx([(429, {})] * 3)
    code, lines, env = run([ROOT], fake)
    assert code == 1 and lines == [] and len(fake.urls) == 1
    assert env["error"]["code"] == "rate_limited" and "rate limited" in env["error"]["message"]
