import json
from pathlib import Path

import pytest

from rsrc import cli
from rsrc.channels import check

FIX = Path(__file__).parent / "fixtures"
VIDEO = (FIX / "linkedin_video.html").read_bytes()
POST = (FIX / "linkedin_post.html").read_bytes()
ALIAS = "https://ca.linkedin.com/posts/our-recent-segment-on-cnbc-covers-all-things-ugcPost-7442323735535202304-YPvZ/?trk=example"
URL = check.LINKEDIN_POST


def read(capsys, url, answer):
    calls = []

    def send(method, address, headers, body):
        calls.append(address)
        assert method == "GET" and headers == {"User-Agent": "research-sources"} and body is None
        return answer

    ctx = cli.Context(environ={}, transport=send, runner=lambda *_: pytest.fail("opened browser"))
    code = cli.main(["linkedin", "read", url], ctx)
    return code, json.loads(capsys.readouterr().out), calls


def test_video_alias(capsys):
    code, out, calls = read(capsys, ALIAS, (200, VIDEO))
    assert code == 0 and calls == [ALIAS.split("?")[0].replace("//ca.", "//www.")]
    post, first, second = out["items"]
    assert post["author"] == "Aaru" and "7442325358646853632" in post["url"]
    assert post["id"] == "7442323735535202304" and post["text"].endswith("Squawk Box team.")
    assert post["raw"]["transcript"] and post["raw"]["duration"] == "PT12M19S"
    assert post["published_at"] == "2026-03-24T21:43:57.172Z"
    assert post["engagement"] == {"reactions": 450, "comments": 29}
    assert first["author"] == "Reader 1" and first["engagement"] == {"reactions": 8}
    assert second["published_at"] and second["text"]
    assert "2 of 29" in out["meta"]["notes"][0]


def test_social_post(capsys):
    code, out, calls = read(capsys, URL, (200, POST))
    post = out["items"][0]
    assert code == 0 and calls == [URL]
    assert post["text"].endswith("https://pika.art/blog") and post["author"] == "Pika"
    assert post["published_at"] == "2024-06-05T23:03:12.908Z"
    assert out["items"][1]["raw"]["post_id"] == post["id"]


@pytest.mark.parametrize("url", ["https://linkedin.com/in/alex", "https://linkedin.com/company/x",
                                 "https://linkedin.com/search/results/all", "https://linkedin.com/feed/",
                                 "https://linkedin.com/jobs/view/20", URL.replace(".com", ".com.evil.test")])
def test_reject_before_request(capsys, url):
    code, out, calls = read(capsys, url, (200, POST))
    assert code == 2 and out["error"]["code"] == "usage" and not calls


@pytest.mark.parametrize("answer,expected", [
    ((404, (FIX / "linkedin_missing.html").read_bytes()), "not_found"),
    ((307, {}, b""), "blocked"),
    ((302, {"Location": "/authwall"}, b""), "auth_failed"),
    ((200, b"<title>Sign in | LinkedIn</title>"), "auth_failed"),
    ((200, b'<meta name="pageKey" content="checkpoint_challenge">'), "blocked"),
    ((403, b""), "blocked"), ((999, b""), "blocked"), ((429, b""), "rate_limited"),
    ((200, b"<html>"), "blocked"), ((200, b""), "upstream_error"),
    ((200, VIDEO), "url_mismatch"),
    ((200, POST.replace(b"7204256477832753153", b"7204256477832753154", 1)), "url_mismatch"),
    ((200, POST.replace(b"data-featured-activity-urn=", b"data-unrelated=")), "upstream_error"),
    ((200, POST.replace(b'"articleBody"', b'"summary"')), "upstream_error"),
])
def test_unreadable(capsys, answer, expected):
    code, out, calls = read(capsys, URL, answer)
    assert code == 1 and calls == [URL] and not out["items"] and not out["ok"]
    assert out["error"]["code"] == expected
    if expected in ("blocked", "auth_failed", "rate_limited"):
        assert "Wait" in out["error"]["message"]
