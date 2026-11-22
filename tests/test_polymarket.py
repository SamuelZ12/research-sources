"""`rsrc polymarket` against the recorded gamma answers (`public-search?q=Claude`, pages 1 and 2)."""

import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from rsrc import cli
from rsrc.channels import polymarket
from rsrc.envelope import ITEM_KEYS

FIXTURES = Path(__file__).parent / "fixtures"
PAGE_1 = (FIXTURES / "polymarket_search_p1.json").read_bytes()  # 5 events, pagination.hasMore true
PAGE_2 = (FIXTURES / "polymarket_search_p2.json").read_bytes()  # 5 more events
EMPTY = (FIXTURES / "polymarket_search_empty.json").read_bytes()  # a search matching nothing: no `events` key

EVENT_KEYS = {"id", "slug", "title", "description", "startDate", "endDate", "createdAt", "updatedAt", "active",
              "closed", "volume", "volume24hr", "volume1wk", "volume1mo", "liquidity", "commentCount", "markets"}
MARKET_KEYS = {"id", "question", "slug", "outcomes", "outcomePrices", "volume", "liquidity", "endDate", "active",
               "closed", "lastTradePrice", "bestBid", "bestAsk", "oneDayPriceChange", "oneWeekPriceChange",
               "oneMonthPriceChange"}

OPUS_BY = "1071679"  # "Next Claude Opus (5.6+) released by...?": 3 markets, 1 closed
OPUS_ON = "1274168"  # "Next Claude Opus (5.6+) released on...?": 32 markets, 16 closed


def events() -> list[dict]:
    return json.loads(PAGE_1)["events"] + json.loads(PAGE_2)["events"]


def event(event_id: str) -> dict:
    """A fresh copy of one recorded event, safe to change."""
    [found] = [e for e in events() if e["id"] == event_id]
    return copy.deepcopy(found)


def market(record: dict, market_id: str) -> dict:
    [found] = [m for m in record["markets"] if m["id"] == market_id]
    return found


# the raw record

def test_record_has_exactly_the_event_and_market_keys():
    for recorded in events():
        record = polymarket.record(recorded)
        assert set(record) == EVENT_KEYS
        assert record["markets"] and all(set(m) == MARKET_KEYS for m in record["markets"])


def test_record_passes_values_on_as_the_api_sent_them():
    recorded = event(OPUS_BY)
    record = polymarket.record(recorded)
    assert record["slug"] == "next-claude-opus-released-byptptpt-20260923144500000"
    assert record["description"] == recorded["description"] and len(record["description"]) == 1524
    assert record["volume"] == 37278.927921 and record["commentCount"] == 0
    by_november = market(record, "4895504")
    assert by_november["volume"] == "28262.265372"  # a market's volume arrives as decimal text
    assert by_november["endDate"] == "2026-11-30T23:59:00Z" and by_november["oneMonthPriceChange"] == -0.37


def test_outcomes_and_prices_become_lists_with_prices_as_numbers():
    by_november = market(polymarket.record(event(OPUS_BY)), "4895504")
    assert by_november["outcomes"] == ["Yes", "No"]
    assert by_november["outcomePrices"] == [0.315, 0.685]


def test_a_field_the_api_lacks_is_null():
    recorded = event(OPUS_BY)
    del recorded["commentCount"]
    record = polymarket.record(recorded)
    assert record["commentCount"] is None
    closed = market(record, "5130976")  # the API sent no liquidity, bestBid for this closed market
    assert closed["liquidity"] is None and closed["bestBid"] is None and closed["closed"] is True


def test_markets_keeps_every_market_in_the_apis_order_closed_ones_included():
    recorded = event(OPUS_ON)
    record = polymarket.record(recorded)
    assert [m["id"] for m in record["markets"]] == [m["id"] for m in recorded["markets"]]
    assert len(record["markets"]) == 32 and sum(m["closed"] for m in record["markets"]) == 16


def test_an_event_without_markets_has_null_markets():
    recorded = event(OPUS_BY)
    del recorded["markets"]
    assert polymarket.record(recorded)["markets"] is None


@pytest.mark.parametrize("prices", ["not json", '{"Yes": "0.3"}', '["0.3", "high"]', '["NaN", "0.5"]',
                                    '["0.3", true]', None, 5])
def test_prices_that_cannot_be_parsed_are_null_and_the_rest_of_the_event_is_kept(prices):
    recorded = event(OPUS_BY)
    recorded["markets"][0]["outcomePrices"] = prices
    record = polymarket.record(recorded)
    assert record["markets"][0]["outcomePrices"] is None
    assert record["markets"][0]["outcomes"] == ["Yes", "No"]
    assert record["markets"][1]["outcomePrices"] == [0.84, 0.16]


@pytest.mark.parametrize("outcomes", ["Yes or No", "[1, 2]", '"Yes"', None])
def test_outcomes_that_are_not_a_list_of_names_are_null(outcomes):
    recorded = event(OPUS_BY)
    recorded["markets"][0]["outcomes"] = outcomes
    assert polymarket.record(recorded)["markets"][0]["outcomes"] is None


# the text lines

def text_of(recorded: dict) -> str | None:
    return polymarket.text(polymarket.record(recorded))


def test_text_has_one_line_per_open_market_with_prices_and_end_date():
    assert text_of(event(OPUS_BY)) == (
        "Next Claude Opus (5.6+) released by November 30, 2026?: Yes 31.5%, No 68.5% (ends 2026-11-30)\n"
        "Next Claude Opus (5.6+) released by December 31, 2026?: Yes 84.0%, No 16.0% (ends 2026-12-31)")


def test_text_puts_the_highest_volume_market_first():
    lines = text_of(event("1098253")).splitlines()  # the API's order is 1510, 1520, 1530, 1540, 1550
    scores = [line.split("at least ")[1][:4] for line in lines]
    assert scores == ["1530", "1510", "1540", "1520", "1550"]


def test_a_closed_or_inactive_market_gets_no_line():
    recorded = event(OPUS_BY)
    assert "October 31" not in text_of(recorded)  # recorded as closed
    recorded["markets"][0]["active"] = False
    assert text_of(recorded).splitlines() == [
        "Next Claude Opus (5.6+) released by December 31, 2026?: Yes 84.0%, No 16.0% (ends 2026-12-31)"]


def test_more_than_ten_open_markets_are_cut_to_ten_lines_and_a_count():
    recorded = event(OPUS_ON)  # 16 open markets
    lines = text_of(recorded).splitlines()
    assert len(lines) == 11 and lines[-1] == "6 more markets"
    top_ten = sorted((m for m in recorded["markets"] if not m["closed"]),
                     key=lambda m: float(m["volume"]), reverse=True)[:10]
    assert [line.split("?: ")[0] + "?" for line in lines[:10]] == [m["question"] for m in top_ten]


def test_exactly_ten_open_markets_have_no_count_line():
    recorded = event(OPUS_ON)
    still_open = [m for m in recorded["markets"] if not m["closed"]]
    for extra in still_open[10:]:
        extra["closed"] = True
    assert len(text_of(recorded).splitlines()) == 10


@pytest.mark.parametrize("field, value", [("outcomePrices", "not json"), ("outcomePrices", '["0.3"]'),
                                          ("outcomes", None)])
def test_a_market_whose_prices_cannot_be_parsed_gets_its_question_alone(field, value):
    recorded = event(OPUS_BY)
    recorded["markets"][0][field] = value
    assert text_of(recorded).splitlines()[0] == "Next Claude Opus (5.6+) released by November 30, 2026?"


def test_the_end_date_is_the_utc_date_and_is_left_out_when_missing():
    recorded = event(OPUS_BY)
    recorded["markets"][0]["endDate"] = "2026-11-30T20:00:00-05:00"
    del recorded["markets"][1]["endDate"]
    assert text_of(recorded).splitlines() == [
        "Next Claude Opus (5.6+) released by November 30, 2026?: Yes 31.5%, No 68.5% (ends 2026-12-01)",
        "Next Claude Opus (5.6+) released by December 31, 2026?: Yes 84.0%, No 16.0%"]


@pytest.mark.parametrize("end", ["soon", "", "2026-13-45T00:00:00Z", 1795996740])
def test_an_end_date_that_is_not_a_date_is_left_out(end):
    recorded = event(OPUS_BY)
    recorded["markets"][0]["endDate"] = end
    assert text_of(recorded).splitlines()[0] == (
        "Next Claude Opus (5.6+) released by November 30, 2026?: Yes 31.5%, No 68.5%")


def test_a_time_without_a_zone_is_read_as_utc():
    recorded = event(OPUS_BY)
    recorded["markets"][0]["endDate"] = "2026-11-30T23:59:00"
    assert text_of(recorded).splitlines()[0].endswith("(ends 2026-11-30)")


def test_a_market_without_a_numeric_volume_sorts_last_and_ties_keep_the_apis_order():
    recorded = event("1303591")  # 5 open markets
    for position, volume in enumerate(["n/a", "7", None, "7", "9"]):
        recorded["markets"][position]["volume"] = volume
    questions = [m["question"] for m in recorded["markets"]]
    lines = text_of(recorded).splitlines()
    assert [line.split("?: ")[0] + "?" for line in lines] == [questions[i] for i in (4, 1, 3, 0, 2)]


def test_a_market_without_a_question_gets_no_line():
    recorded = event(OPUS_BY)
    del recorded["markets"][0]["question"]
    assert text_of(recorded).splitlines() == [
        "Next Claude Opus (5.6+) released by December 31, 2026?: Yes 84.0%, No 16.0% (ends 2026-12-31)"]


def test_a_question_is_shown_on_one_line_without_edge_spaces():
    recorded = event(OPUS_BY)
    recorded["markets"][0]["question"] = "  Released by\nNovember 30?  "
    assert text_of(recorded).splitlines() == [
        "Released by November 30?: Yes 31.5%, No 68.5% (ends 2026-11-30)",
        "Next Claude Opus (5.6+) released by December 31, 2026?: Yes 84.0%, No 16.0% (ends 2026-12-31)"]


def test_an_event_with_no_open_market_has_no_text():
    recorded = event(OPUS_BY)
    for recorded_market in recorded["markets"]:
        recorded_market["closed"] = True
    assert text_of(recorded) is None
    del recorded["markets"]
    assert text_of(recorded) is None


# search: one page

class FakeTransport:
    """Answers requests in order from canned (status, body) pairs or exceptions; records each request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[tuple] = []

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, headers, body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def queries(self) -> list[dict]:
        return [parse_qs(urlsplit(url).query) for _, url, _, _ in self.requests]


def answer(events_: list, has_more: bool = False) -> tuple[int, bytes]:
    data = {"events": events_, "pagination": {"hasMore": has_more, "totalResults": len(events_)}}
    return 200, json.dumps(data).encode()


@pytest.fixture
def rsrc(monkeypatch, capsys):
    monkeypatch.setattr(cli, "CHANNELS", [polymarket])

    def run(argv, transport, sleep=lambda s: None):
        ctx = cli.Context(environ={}, transport=transport, sleep=sleep)
        code = cli.main(["polymarket", *argv], ctx)
        out = capsys.readouterr().out
        assert len(out.splitlines()) == 1
        return code, json.loads(out)

    return run


def item_by_id(out: dict, item_id: str) -> dict:
    [found] = [i for i in out["items"] if i["id"] == item_id]
    return found


def test_polymarket_is_reachable_through_the_real_channel_list(capsys):
    code = cli.main(["polymarket", "search", ""], cli.Context(environ={}))  # usage: no request is made
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and (out["channel"], out["action"]) == ("polymarket", "search")
    assert "QUERY" in out["error"]["message"]


def test_search_makes_one_keyless_get_for_the_first_page(rsrc):
    transport = FakeTransport((200, PAGE_1))
    rsrc(["search", "Claude Code & friends", "--limit", "5"], transport)
    [(method, url, headers, body)] = transport.requests
    assert method == "GET" and body is None
    assert url.startswith("https://gamma-api.polymarket.com/public-search?")
    assert transport.queries == [{"q": ["Claude Code & friends"], "page": ["1"], "events_status": ["active"],
                                  "keep_closed_markets": ["0"], "limit_per_type": ["5"]}]
    assert {name.lower() for name in headers} == {"user-agent"}  # no cookie, no key


def test_default_limit_returns_both_recorded_pages_in_one_request(rsrc):
    recorded = events()
    transport = FakeTransport(answer(recorded, has_more=True))
    code, out = rsrc(["search", "Claude"], transport)
    assert code == 0
    assert [query["limit_per_type"] for query in transport.queries] == [["10"]]
    assert [item["raw"] for item in out["items"]] == [polymarket.record(e) for e in recorded]


def test_search_returns_one_item_per_event_in_the_apis_order(rsrc):
    code, out = rsrc(["search", "Claude", "--limit", "5"], FakeTransport((200, PAGE_1)))
    assert code == 0 and out["ok"] is True and out["error"] is None
    assert (out["channel"], out["action"], out["backend"]) == ("polymarket", "search", "gamma")
    assert out["input"] == {"query": "Claude", "limit": 5}
    assert [i["id"] for i in out["items"]] == ["1071679", "1303591", "1098253", "1274168", "1073286"]
    [attempt] = out["meta"]["attempts"]
    assert (attempt["backend"], attempt["outcome"], attempt["requests"]) == ("gamma", "ok", 1)
    assert isinstance(attempt["ms"], int)


def test_an_item_carries_the_event_its_prices_and_its_record(rsrc):
    _, out = rsrc(["search", "Claude", "--limit", "5"], FakeTransport((200, PAGE_1)))
    item = item_by_id(out, OPUS_BY)
    assert tuple(item) == ITEM_KEYS
    assert item["url"] == "https://polymarket.com/event/next-claude-opus-released-byptptpt-20260923144500000"
    assert item["title"] == "Next Claude Opus (5.6+) released by...?" and item["author"] is None
    assert item["published_at"] == "2026-09-23T23:54:54Z"
    assert item["text"] == (
        "Next Claude Opus (5.6+) released by November 30, 2026?: Yes 31.5%, No 68.5% (ends 2026-11-30)\n"
        "Next Claude Opus (5.6+) released by December 31, 2026?: Yes 84.0%, No 16.0% (ends 2026-12-31)")
    assert item["source"] == "polymarket"
    assert item["engagement"] == {"volume": 37279, "liquidity": 8251, "comments": 0}
    assert all(type(value) is int for value in item["engagement"].values())
    assert item["raw"] == polymarket.record(event(OPUS_BY))


def test_limit_cuts_the_items(rsrc):
    transport = FakeTransport((200, PAGE_1))
    code, out = rsrc(["search", "Claude", "--limit", "2"], transport)
    assert code == 0 and [i["id"] for i in out["items"]] == ["1071679", "1303591"]
    assert len(transport.requests) == 1


def test_engagement_leaves_out_what_the_event_lacks_and_is_null_without_any(rsrc):
    partial, bare = event(OPUS_BY), event("1303591")
    del partial["liquidity"]
    partial["commentCount"] = "many"
    for name in ("volume", "liquidity", "commentCount"):
        del bare[name]
    _, out = rsrc(["search", "Claude", "--limit", "2"], FakeTransport(answer([partial, bare])))
    assert item_by_id(out, OPUS_BY)["engagement"] == {"volume": 37279}
    assert item_by_id(out, "1303591")["engagement"] is None


def test_published_at_is_utc_and_null_without_a_start_date(rsrc):
    offset, missing = event(OPUS_BY), event("1303591")
    offset["startDate"] = "2026-09-23T19:54:54-04:00"
    del missing["startDate"]
    _, out = rsrc(["search", "Claude", "--limit", "2"], FakeTransport(answer([offset, missing])))
    assert item_by_id(out, OPUS_BY)["published_at"] == "2026-09-23T23:54:54Z"
    assert item_by_id(out, "1303591")["published_at"] is None


def test_published_at_is_null_when_the_start_date_is_not_a_date(rsrc):
    odd = {**event(OPUS_BY), "startDate": "soon"}
    code, out = rsrc(["search", "Claude", "--limit", "1"], FakeTransport(answer([odd])))
    assert code == 0 and out["items"][0]["published_at"] is None


def test_the_event_url_quotes_the_slug(rsrc):
    odd = {**event(OPUS_BY), "slug": "a b/c?d#e"}
    _, out = rsrc(["search", "Claude", "--limit", "1"], FakeTransport(answer([odd])))
    assert out["items"][0]["url"] == "https://polymarket.com/event/a%20b%2Fc%3Fd%23e"


def test_a_numeric_event_id_becomes_a_string(rsrc):
    numbered = event(OPUS_BY)
    numbered["id"] = 1071679
    _, out = rsrc(["search", "Claude", "--limit", "1"], FakeTransport(answer([numbered])))
    assert [i["id"] for i in out["items"]] == ["1071679"]


@pytest.mark.parametrize("body", [EMPTY, b'{"events": [], "pagination": {"hasMore": false, "totalResults": 0}}'])
def test_no_events_is_an_empty_result_with_exit_66(rsrc, body):
    code, out = rsrc(["search", "zzzzqqqxxnomatch"], FakeTransport((200, body)))
    assert code == 66 and out["ok"] is True and out["items"] == [] and out["error"] is None
    assert out["meta"]["attempts"][0]["outcome"] == "no_results"


# search: paging

def pages() -> tuple[list[dict], list[dict]]:
    return json.loads(PAGE_1)["events"], json.loads(PAGE_2)["events"]


def numbered(count: int) -> list[dict]:
    """`count` distinct events: copies of one recorded event with IDs 1, 2, …"""
    return [{**event(OPUS_BY), "id": str(n), "slug": f"event-{n}"} for n in range(1, count + 1)]


def test_the_default_limit_of_ten_takes_two_pages_in_order(rsrc):
    transport = FakeTransport((200, PAGE_1), (200, PAGE_2))
    code, out = rsrc(["search", "Claude"], transport)
    first, second = pages()
    assert code == 0 and out["input"] == {"query": "Claude", "limit": 10}
    assert [i["id"] for i in out["items"]] == [e["id"] for e in first + second]
    assert [q["page"] for q in transport.queries] == [["1"], ["2"]]
    assert all(q["q"] == ["Claude"] for q in transport.queries)
    [attempt] = out["meta"]["attempts"]
    assert (attempt["outcome"], attempt["requests"]) == ("ok", 2)
    assert out["meta"]["truncated"] is None


def test_paging_stops_once_the_limit_is_met_and_the_items_are_cut_to_it(rsrc):
    transport = FakeTransport((200, PAGE_1), (200, PAGE_2))  # page 2 says there are more
    _, out = rsrc(["search", "Claude", "--limit", "7"], transport)
    assert len(out["items"]) == 7 and len(transport.requests) == 2
    assert out["meta"]["truncated"] is None


def test_an_event_already_seen_on_an_earlier_page_is_not_repeated(rsrc):
    first, second = pages()
    transport = FakeTransport(answer(first, has_more=True), answer([first[4], *second[:4]]))
    _, out = rsrc(["search", "Claude"], transport)
    assert [i["id"] for i in out["items"]] == [e["id"] for e in first + second[:4]]


def test_the_same_id_as_a_number_and_as_text_is_one_event(rsrc):
    one, two, three = numbered(3)
    transport = FakeTransport(answer([one, two], has_more=True), answer([{**one, "id": 1}, three]))
    _, out = rsrc(["search", "Claude"], transport)
    assert [i["id"] for i in out["items"]] == ["1", "2", "3"]


def test_a_page_with_no_new_event_stops_paging(rsrc):
    first, _ = pages()
    transport = FakeTransport(answer(first, has_more=True), answer(first, has_more=True))
    code, out = rsrc(["search", "Claude"], transport)
    assert code == 0 and len(out["items"]) == 5 and len(transport.requests) == 2
    assert out["meta"]["truncated"] is None


def test_paging_stops_when_the_answer_says_there_are_no_more_pages(rsrc):
    first, _ = pages()
    transport = FakeTransport(answer(first[:3], has_more=False))
    code, out = rsrc(["search", "Claude"], transport)
    assert code == 0 and len(out["items"]) == 3 and len(transport.requests) == 1


def test_an_answer_without_paging_information_pages_until_nothing_new_arrives(rsrc):
    first, _ = pages()
    transport = FakeTransport((200, json.dumps({"events": first}).encode()),
                              (200, json.dumps({"events": []}).encode()))
    _, out = rsrc(["search", "Claude"], transport)
    assert len(out["items"]) == 5 and len(transport.requests) == 2


@pytest.mark.parametrize("has_more", [None, 0, "false"])
def test_only_a_false_has_more_ends_paging(rsrc, has_more):
    first, _ = pages()
    transport = FakeTransport(answer(first, has_more=has_more), (200, PAGE_2))
    _, out = rsrc(["search", "Claude"], transport)
    assert len(out["items"]) == 10 and len(transport.requests) == 2


def test_pages_are_requested_a_second_apart(rsrc):
    sleeps = []
    rsrc(["search", "Claude"], FakeTransport((200, PAGE_1), (200, PAGE_2)), sleep=sleeps.append)
    assert sleeps == [pytest.approx(1.0, abs=0.1)]


def test_five_requests_is_the_cap_and_stopping_there_with_more_pages_is_marked(rsrc):
    transport = FakeTransport(*(answer([e], has_more=True) for e in numbered(5)))  # a sixth request would fail
    code, out = rsrc(["search", "Claude", "--limit", "25"], transport)
    assert code == 0 and out["ok"] is True
    assert [i["id"] for i in out["items"]] == ["1", "2", "3", "4", "5"]
    assert out["meta"]["truncated"] == "request_cap"
    assert out["meta"]["attempts"][0]["requests"] == 5


def test_a_limit_met_by_the_fifth_request_is_not_marked_truncated(rsrc):
    distinct = numbered(25)
    transport = FakeTransport(*(answer(distinct[n:n + 5], has_more=True) for n in range(0, 25, 5)))
    _, out = rsrc(["search", "Claude", "--limit", "25"], transport)
    assert len(out["items"]) == 25 and out["meta"]["truncated"] is None


def test_the_last_page_at_the_cap_is_not_marked_truncated(rsrc):
    singles = numbered(5)
    transport = FakeTransport(*(answer([e], has_more=True) for e in singles[:4]), answer([singles[4]]))
    _, out = rsrc(["search", "Claude", "--limit", "25"], transport)
    assert len(out["items"]) == 5 and out["meta"]["truncated"] is None


# search: usage and failures

@pytest.mark.parametrize("argv", [
    ["search"],
    ["search", ""],
    ["search", "   "],
    ["search", "Claude", "--limit", "0"],
    ["search", "Claude", "--limit", "26"],
    ["search", "Claude", "--limit", "-1"],
    ["search", "Claude", "--limit", "ten"],
])
def test_bad_arguments_are_usage_errors_and_make_no_request(rsrc, argv):
    transport = FakeTransport()
    code, out = rsrc(argv, transport)
    assert code == 2 and out["ok"] is False and out["error"]["code"] == "usage"
    assert (out["channel"], out["action"], out["items"]) == ("polymarket", "search", [])
    assert transport.requests == []


def test_the_largest_limit_is_25(rsrc):
    code, out = rsrc(["search", "Claude", "--limit", "25"], FakeTransport(answer(numbered(3))))
    assert code == 0 and len(out["items"]) == 3


def test_http_429_is_rate_limited_and_stops_at_once(rsrc):
    transport = FakeTransport((429, b'{"error": "slow down"}'))  # a second request would fail the test
    code, out = rsrc(["search", "Claude"], transport)
    assert code == 1 and out["ok"] is False and out["items"] == []
    assert out["error"]["code"] == "rate_limited" and "429" in out["error"]["message"]
    [attempt] = out["meta"]["attempts"]
    assert (attempt["outcome"], attempt["requests"]) == ("rate_limited", 1)
    assert len(transport.requests) == 1


def test_no_response_is_a_network_error(rsrc):
    code, out = rsrc(["search", "Claude"], FakeTransport(ConnectionRefusedError("connection refused")))
    assert code == 1 and out["ok"] is False and out["items"] == []
    assert out["error"]["code"] == "network_error"
    assert out["meta"]["attempts"][0]["outcome"] == "network_error"


def without(event_id: str, field: str) -> dict:
    changed = event(event_id)
    del changed[field]
    return changed


@pytest.mark.parametrize("status, body", [
    (500, b"Internal Server Error"),
    (503, b""),
    (403, b"<html>Forbidden</html>"),
    (404, b"{}"),
    (200, b"<html>not json</html>"),
    (200, b"\xff\xfe"),
    (200, b"[]"),
    (200, b"{}"),
    (200, b'{"error": "bad request"}'),
    (200, b'{"pagination": {"hasMore": false}}'),
    (200, b'{"pagination": {"hasMore": false, "totalResults": 3}}'),
    (200, b'{"pagination": {"hasMore": false, "totalResults": false}}'),
    (200, b'{"pagination": {"hasMore": false, "totalResults": "0"}}'),
    (200, b'{"events": null, "pagination": {"hasMore": false, "totalResults": 0}}'),
    (200, b'{"events": null}'),
    (200, b'{"events": {"0": {}}}'),
    (200, b'{"events": ["1071679"]}'),
    (200, json.dumps({"events": [without(OPUS_BY, "id")]}).encode()),
    (200, json.dumps({"events": [without(OPUS_BY, "slug")]}).encode()),
    (200, json.dumps({"events": [{**event(OPUS_BY), "id": True}]}).encode()),
    (200, json.dumps({"events": [{**event(OPUS_BY), "id": ""}]}).encode()),
    (200, json.dumps({"events": [{**event(OPUS_BY), "slug": ""}]}).encode()),
    (200, json.dumps({"events": [{**event(OPUS_BY), "markets": "none"}]}).encode()),
    (200, json.dumps({"events": [{**event(OPUS_BY), "markets": ["4895504"]}]}).encode()),
])
def test_other_statuses_and_unexpected_bodies_are_upstream_errors_naming_the_status(rsrc, status, body):
    transport = FakeTransport((status, body))
    code, out = rsrc(["search", "Claude"], transport)
    assert code == 1 and out["ok"] is False and out["items"] == []
    assert out["error"]["code"] == "upstream_error" and f"HTTP {status}" in out["error"]["message"]
    [attempt] = out["meta"]["attempts"]
    assert (attempt["outcome"], attempt["requests"]) == ("upstream_error", 1)


@pytest.mark.parametrize("failure, code_", [
    ((500, b""), "upstream_error"),
    ((429, b""), "rate_limited"),
    (TimeoutError("timed out"), "network_error"),
    ((200, b'{"events": null}'), "upstream_error"),
])
def test_a_failure_on_a_later_page_keeps_the_events_already_collected(rsrc, failure, code_):
    transport = FakeTransport((200, PAGE_1), failure)  # a third request would fail the test
    code, out = rsrc(["search", "Claude"], transport)
    assert code == 1 and out["ok"] is False and out["error"]["code"] == code_
    assert [i["id"] for i in out["items"]] == [e["id"] for e in pages()[0]]
    [attempt] = out["meta"]["attempts"]
    assert (attempt["outcome"], attempt["requests"]) == (code_, 2)
