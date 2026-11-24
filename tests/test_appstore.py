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
    catalog = recording("app")
    catalog["results"][0].update(description="Send private messages.", releaseNotes="Fixed a bug.")
    code, out = run(capsys, ["search", "Signal"], catalog)
    a = out["items"][0]
    assert code == 0 and a["id"] == "874139669" and a["title"] == "Signal - Private Messenger"
    assert a["engagement"]["rating"] == recording("app")["results"][0]["averageUserRating"]
    assert a["author"] and a["engagement"]["ratings"] and a["raw"]["version"]
    assert a["text"] is None


@pytest.mark.parametrize("target", ["Signal - Private Messenger", "874139669", "https://apps.apple.com/us/app/id874139669"])
def test_read(capsys, target):
    code, out = run(capsys, ["read", target, "--limit", "2"], recording("app"), recording("rss"))
    app, first, second = out["items"]
    assert code == 0 and app["raw"]["kind"] == "app"
    assert first["id"] == "14813499075" and first["published_at"] == "2026-11-20T17:25:52Z"
    assert first["raw"]["version"] == "8.38" and first["engagement"]["helpful_votes"] == 0
    assert first["text"] and first["engagement"]["rating"] == 5 and first["published_at"] >= second["published_at"]


@pytest.mark.parametrize("facts,expected", [
    ({"formattedPrice": "Free", "primaryGenreName": "Social Networking", "version": "8.38",
      "releaseDate": "2014-07-29T17:00:00Z", "minimumOsVersion": "15.0", "releaseNotes": "Fixed a bug."},
     "Free · Social Networking · Version 8.38 · Released 2014-07-29 · Requires OS 15.0\n\n"
     "Send private messages.\n\nWhat's new in 8.38\nFixed a bug."),
    ({"primaryGenreName": None, "version": None}, "Send private messages."),
])
def test_listing_text(capsys, facts, expected):
    catalog = recording("app")
    catalog["results"][0].update(description="Send private messages.", **facts)
    code, out = run(capsys, ["read", "874139669", "--limit", "1"], catalog, recording("rss"))
    assert code == 0 and out["items"][0]["text"] == expected


@pytest.mark.parametrize("country,title,limit", [
    ("fr", "iTunes Store : Avis d’utilisateurs", 1),
    ("de", "iTunes Store: Rezensionen", 2),
])
def test_localized_reviews(capsys, country, title, limit):
    catalog = json.loads(json.dumps(recording("app")).replace("/us/", f"/{country}/"))
    rss = json.loads(json.dumps(recording("rss")).replace("/us/", f"/{country}/"))
    feed = rss["feed"]
    feed["title"]["label"] = title
    ids = [entry["id"]["label"] for entry in feed["entry"][:limit]]
    if limit == 1:
        feed["entry"] = feed["entry"][0]
    code, out = run(capsys, ["read", "874139669", "--country", country, "--limit", str(limit)], catalog, rss)
    assert code == 0 and out["error"] is None
    assert [item["id"] for item in out["items"]] == ["874139669", *ids]


def test_ambiguous_name_returns_choices_without_reading(capsys):
    r = recording("app")
    r["results"][0]["description"] = "Send private messages."
    r["results"] += [{**r["results"][0], "trackName": "Signal companion"}]
    r["resultCount"] = 2
    code, out = run(capsys, ["read", "Signal"], r)
    assert code == 2 and len(out["items"]) == 2
    assert all(item["text"] is None for item in out["items"])


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


def test_rss_page_after_last_has_no_entries(capsys):
    first = recording("rss")
    feed = first["feed"]
    feed["entry"] = feed["entry"][0]
    last = next(link["attributes"] for link in feed["link"] if link["attributes"]["rel"] == "last")
    last["href"] = last["href"].replace("page=10", "page=1")
    end = json.loads(json.dumps(first))
    feed = end["feed"]
    del feed["entry"]
    feed["id"]["label"] = feed["id"]["label"].replace("page=1", "page=2")
    for link in feed["link"]:
        attrs = link["attributes"]
        if attrs["rel"] == "self":
            attrs["href"] = feed["id"]["label"]
        else:
            attrs["href"] = attrs["href"].replace("urlDesc=/customerreviews/page=1", "urlDesc=/customerreviews/page=2")
    count = json.loads(json.dumps(wiki_count(1)).replace("324715238", "874139669"))
    code, out = run(capsys, ["read", "874139669"], recording("app"), first, end, count)
    assert code == 0 and out["error"] is None
    assert [item["id"] for item in out["items"]] == ["874139669", "14813499075"]
    assert "Apple reports 1 written reviews in US." in out["meta"]["notes"]


def test_empty_rss_confirmed_by_count_or_filled_from_rows(capsys):
    a, f = recording("wiki"), recording("empty_feed")
    a["results"][0]["description"] = "Browse the free encyclopedia."
    code, out = run(capsys, ["read", "324715238"], a, f, wiki_count(0))
    assert code == 0 and out["error"] is None
    item, = out["items"]
    assert item["id"] == "324715238" and item["text"].endswith("\n\nBrowse the free encyclopedia.")
    assert out["meta"]["notes"][-1] == "Wikipedia (324715238) has no written reviews in US."
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


def test_a_read_never_requests_the_write_review_link_it_reads(capsys):
    count = json.loads(json.dumps(wiki_count(1)).replace("324715238", "874139669"))
    feed = json.loads(json.dumps(recording("empty_feed")).replace("324715238", "874139669"))
    pending = iter([recording("app"), feed, count, recording("rows")])
    requested = []

    def transport(method, url, headers, body):
        requested.append(url)
        return 200, json.dumps(next(pending, {})).encode()

    cli.main(["appstore", "read", "874139669"], cli.Context(transport=transport, sleep=lambda _: None))
    assert count["writeUserReviewUrl"] not in requested
    assert json.loads(capsys.readouterr().out)["ok"]
