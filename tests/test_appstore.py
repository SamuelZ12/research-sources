import json
from pathlib import Path

import pytest

from rsrc import cli

FIXTURES = Path(__file__).parent / "fixtures/appstore"


def recording(name):
    return json.loads((FIXTURES / f"{name}.json").read_bytes())


def run(capsys, argv, *responses):
    pending = iter(responses)

    def transport(method, url, headers, body):
        assert method == "GET" and body is None
        result = next(pending)
        if isinstance(result, tuple):
            return result
        return 200, json.dumps(result).encode()

    code = cli.main(["appstore", *argv], cli.Context(transport=transport, sleep=lambda _: None))
    assert list(pending) == []
    return code, json.loads(capsys.readouterr().out)


def test_search(capsys):
    code, out = run(capsys, ["search", "Signal"], recording("app"))
    a = out["items"][0]
    assert code == 0 and a["id"] == "874139669" and a["title"] == "Signal - Private Messenger"
    assert a["engagement"]["rating"] == recording("app")["results"][0]["averageUserRating"]
    assert a["author"] and a["engagement"]["ratings"] and a["raw"]["version"]


@pytest.mark.parametrize("target", ["Signal - Private Messenger", "874139669", "https://apps.apple.com/us/app/id874139669"])
def test_read(capsys, target):
    code, out = run(capsys, ["read", target, "--limit", "2"], recording("app"), recording("rss"))
    app, first, second = out["items"]
    assert code == 0 and app["raw"]["kind"] == "app"
    assert first["id"] == "14813499075" and first["published_at"] == "2026-11-20T17:25:52Z"
    assert first["raw"]["version"] == "8.38" and first["engagement"]["helpful_votes"] == 0
    assert first["text"] and first["engagement"]["rating"] == 5 and first["published_at"] >= second["published_at"]


def test_ambiguous_name_returns_choices_without_reading(capsys):
    r = recording("app")
    r["results"] += [{**r["results"][0], "trackName": "Signal companion"}]
    r["resultCount"] = 2
    code, out = run(capsys, ["read", "Signal"], r)
    assert code == 2 and len(out["items"]) == 2


@pytest.mark.parametrize("stage", ["catalog", "feed", "review", "shape"])
def test_identity_and_format_drift_fail_closed(capsys, stage):
    a, f = recording("app"), recording("rss")
    if stage == "catalog":
        responses = [recording("wiki")]
    else:
        responses = [a, f]
        if stage == "feed":
            f["feed"]["id"]["label"] += "x"
        elif stage == "review":
            f["feed"]["entry"][1]["link"]["attributes"]["href"] += "&id=1"
        else:
            del f["feed"]["entry"]
    code, out = run(capsys, ["read", "874139669"], *responses)
    assert code == 1 and out["error"]["code"] == ("upstream_error" if stage == "shape" else "url_mismatch")


def wiki_count(total):
    d = recording("count")
    d.update(adamId=324715238, totalNumberOfReviews=total)
    d["writeUserReviewUrl"] = d["writeUserReviewUrl"].replace("6448311069", "324715238")
    return d


def test_empty_rss_confirmed_by_count_or_filled_from_rows(capsys):
    a, f = recording("wiki"), recording("empty_feed")
    code, out = run(capsys, ["read", "324715238"], a, f, wiki_count(0))
    assert code == 66 and not out["items"] and "no written reviews" in out["meta"]["notes"][-1]
    signal = recording("app")
    feed = json.loads(json.dumps(f).replace("324715238", "874139669"))
    count = json.loads(json.dumps(wiki_count(1)).replace("324715238", "874139669"))
    code, out = run(capsys, ["read", "874139669"], signal, feed, count, recording("rows"))
    assert "&#39;" not in out["items"][1]["text"] and "'" in out["items"][1]["text"]
    assert code == 0 and out["items"][1]["raw"]["version"] is None
    assert out["items"][0]["raw"]["storefront_review_count"] == 1


def test_native_count_or_row_from_wrong_country(capsys):
    a, f = recording("wiki"), recording("empty_feed")
    for extra in [[recording("zero")], [wiki_count(1), {"userReviewList": []}]]:
        code, out = run(capsys, ["read", "324715238"], a, f, *extra)
        assert code == 1 and not out["ok"]


def test_cap_before_and_after_first_review(capsys):
    for cap in (1, 2):
        responses = [recording("app"), recording("rss")][:cap]
        code, out = run(capsys, ["read", "874139669", "--max-requests", str(cap)], *responses)
        assert out["meta"]["truncated"] == "request_cap" and code == (1 if cap == 1 else 0)
        if cap == 1:
            assert out["error"]["code"] == "upstream_error"


def test_unavailable_vs_missing(capsys):
    missing = recording("missing")
    for reference, phrase in [(missing, "not found"), (recording("app"), "unavailable in CN")]:
        code, out = run(capsys, ["read", "874139669", "--country", "cn"], missing, reference)
        assert code == 1 and out["error"]["code"] == "not_found" and phrase in out["error"]["message"]
    code, out = run(capsys, ["search", "zzzz"], missing)
    assert code == 66 and out["items"] == []


@pytest.mark.parametrize("response,expected", [((429, b""), "rate_limited"), ((403, b""), "blocked"),
                                               ((200, (FIXTURES.parent / "login_wall.html").read_bytes()), "blocked"), ({}, "upstream_error")])
def test_failure_stops_at_first_response(capsys, response, expected):
    code, out = run(capsys, ["read", "874139669"], recording("app"), response)
    assert code == 1 and out["error"]["code"] == expected


def test_failure_on_later_page_keeps_reviews(capsys):
    code, out = run(capsys, ["read", "874139669"], recording("app"), recording("rss"), (429, b""))
    assert code == 1 and out["error"]["code"] == "rate_limited"
    assert [r["id"] for r in out["items"]] == ["874139669", "14813499075", recording("rss")["feed"]["entry"][1]["id"]["label"]]


def test_next_rss_page_checked_before_returning_rows(capsys):
    second = recording("rss")
    second["feed"]["id"]["label"] = second["feed"]["id"]["label"].replace("page=1", "page=2")
    for link in second["feed"]["link"]:
        if link["attributes"]["rel"] == "self":
            link["attributes"]["href"] = second["feed"]["id"]["label"]
    second["feed"]["entry"][0]["id"]["label"] = "123"
    code, out = run(capsys, ["read", "874139669", "--limit", "3"], recording("app"), recording("rss"), second)
    assert code == 0 and sorted(x["id"] for x in out["items"][1:]) == sorted(["123", "14813499075", second["feed"]["entry"][1]["id"]["label"]])


def test_read_help_shows_budget(capsys):
    assert cli.main(["appstore", "read", "--help"]) == 0
    assert "maximum reviews (default 30)" in capsys.readouterr().out
