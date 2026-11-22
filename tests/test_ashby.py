import json
from pathlib import Path

import pytest

from rsrc import cli

BOARD = (Path(__file__).parent / "fixtures/ashby/board.json").read_bytes()


@pytest.fixture
def run(capsys):
    def invoke(*args, body=BOARD, status=200, board="quartz"):
        calls = []

        def transport(method, url, headers, data):
            calls.append((method, url, headers, data))
            return status, body

        code = cli.main(["ashby", *args], cli.Context(transport=transport))
        out = json.loads(capsys.readouterr().out)
        assert len(calls) == (0 if code == 2 else 1)
        if calls:
            assert calls[0] == ("GET", f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true",
                                {"User-Agent": "research-sources"}, None)
        return code, out
    return invoke


def test_listing_and_filters(run):
    code, out = run("search", "https://jobs.ashbyhq.com/quartz/role-1?source=irrelevant")
    assert code == 0 and len(out["items"]) == 2
    first, second = out["items"]
    assert first["raw"]["isRemote"] and "compensation" not in first["raw"]
    assert first["published_at"].endswith("Z") and first["text"] is None
    assert first["author"] == second["author"] == "quartz"
    assert second["raw"]["compensation"]["compensationTierSummary"] == "$100K – $120K"
    assert run("search", "quartz", "--title", "DESIGN wid", "--location", "boston",
               "--team", "Product", "--since", "2020-01-01")[1]["items"] == [second]
    assert run("search", "quartz", "--since", "2099-01-01")[0] == 66
    assert run("search", "quartz", "--limit", "1")[1]["meta"]["truncated"]


@pytest.mark.parametrize("args", [("role-0", "--board", "quartz"), ("https://jobs.ashbyhq.com/quartz/role-0",)])
def test_read(run, args):
    code, out = run("read", *args)
    assert code == 0
    assert out["items"][0]["text"] == "The role\n• Build widgets.\n• Test & ship."
    assert "descriptionHtml" in out["items"][0]["raw"]


@pytest.mark.parametrize(("status", "body", "error"), [
    (404, b"Not found", "not_found"), (403, b"Forbidden", "blocked"), (429, b"", "rate_limited"),
    (200, b"<html>Challenge</html>", "upstream_error"), (200, b"", "upstream_error"),
    (200, b"{}", "upstream_error"), (200, b'{"jobs":[{}]}', "upstream_error"),
    (200, BOARD.replace(b"/quartz/", b"/other/"), "url_mismatch"),
])
def test_failure(run, status, body, error):
    code, out = run("search", "quartz", status=status, body=body)
    assert code == 1 and out["error"]["code"] == error and not out["items"]


def test_empty_unlisted_and_missing_role(run):
    assert run("search", "quartz", body=b'{"jobs":[]}')[0] == 66
    unlisted = BOARD.replace(b'"isListed": true', b'"isListed": false')
    assert run("search", "quartz", body=unlisted)[0] == 66
    assert run("read", "role-1", "--board", "quartz", body=unlisted)[0] == 0
    assert run("read", "absent", "--board", "quartz")[1]["error"]["code"] == "not_found"


def test_bad_input_and_company_name(run):
    assert run("read", "role-0")[0] == 2
    assert run("search", "https://example.org/quartz")[0] == 2
    code, out = run("search", "Quartz Labs", status=404, board="Quartz%20Labs")
    assert code == 1 and "careers page" in out["error"]["message"]
