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
    assert first["published_at"].endswith("Z")
    assert first["text"] == "Location: Europe | Workplace: Remote | Employment: FullTime | Team: Product / Engineering"
    assert first["author"] == second["author"] == "quartz"
    assert second["raw"]["compensation"]["compensationTierSummary"] == "$100K – $120K"
    assert second["text"] == ("Location: North America, Boston | Workplace: Remote | Employment: FullTime"
                              " | Team: Product / Engineering | Compensation: $100K – $120K")
    assert run("search", "quartz", "--title", "DESIGN wid", "--location", "boston",
               "--team", "Product", "--since", "2020-01-01")[1]["items"] == [second]
    assert run("search", "quartz", "--since", "2099-01-01")[0] == 66
    assert run("search", "quartz", "--limit", "1")[1]["meta"]["truncated"]


@pytest.mark.parametrize("args", [("role-0", "--board", "quartz"), ("https://jobs.ashbyhq.com/quartz/role-0",)])
def test_read(run, args):
    code, out = run("read", *args)
    assert code == 0
    assert out["items"][0]["text"] == ("Location: Europe\nWorkplace: Remote\nEmployment: FullTime\n"
                                       "Team: Product / Engineering\n\nThe role\n• Build widgets.\n• Test & ship.")
    assert "descriptionHtml" in out["items"][0]["raw"]


def test_read_paid_role_with_secondary_locations_and_one_team(run):
    board = json.loads(BOARD)
    job = board["jobs"][1]
    job.update(team=job["department"], workplaceType=None)
    job["secondaryLocations"].append({"location": "New York"})
    code, out = run("read", "role-1", "--board", "quartz", body=json.dumps(board).encode())
    item, = out["items"]
    assert code == 0
    assert item["text"] == ("Location: North America, Boston, New York\nEmployment: FullTime\n"
                            "Team: Product\nCompensation: $100K – $120K\n\nThe role\n• Build widgets.\n• Test & ship.")
    assert item["raw"] == job


def test_read_without_role_facts(run):
    body = b'{"jobs":[{"title":"Widget Engineer","isListed":true,"descriptionPlain":"Build widgets.",' \
           b'"jobUrl":"https://jobs.ashbyhq.com/quartz/role-0"}]}'
    code, out = run("read", "role-0", "--board", "quartz", body=body)
    assert code == 0 and out["items"][0]["text"] == "Build widgets."


@pytest.mark.parametrize(("status", "body", "error"), [
    (404, b"Not found", "not_found"), (403, b"Forbidden", "blocked"), (429, b"", "rate_limited"),
    (200, b"<html>Challenge</html>", "blocked"), (200, b"", "upstream_error"),
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


def test_compressed_board(capsys, monkeypatch):
    import gzip

    from email.message import Message
    from io import BytesIO
    from rsrc import http

    def read(body, limit=None, status=200, headers=True):
        if limit:
            monkeypatch.setattr(http, "MAX_DECODED_BYTES", limit)
        response = BytesIO(body)
        response.status = status
        response.headers = Message()
        if headers:
            response.headers["Content-Encoding"] = "gzip"

        def send(request, timeout):
            assert request.get_header("Accept-encoding") == "gzip"
            return response

        monkeypatch.setattr(http._no_redirects, "open", send)
        code = cli.main(["ashby", "search", "quartz"])
        return code, json.loads(capsys.readouterr().out)

    packed = gzip.compress(BOARD)
    for headers in (True, False):
        code, out = read(packed, headers=headers)
        assert code == 0 and len(out["items"]) == 2
    assert read(packed, len(BOARD))[0] == 0
    for body, limit in ((b"", None), (packed[:-1], None), (packed + b"junk", None),
                        (b"not gzip", None), (packed, len(BOARD) - 1)):
        code, out = read(body, limit)
        assert code == 1 and out["error"]["code"] == "upstream_error" and "compressed" in out["error"]["message"]
    for status, error in ((401, "auth_failed"), (403, "blocked"), (404, "not_found"), (429, "rate_limited")):
        code, out = read(packed[:-1], status=status)
        assert code == 1 and out["error"]["code"] == error and not out["items"]


@pytest.mark.parametrize("headers", [None, {"content-encoding": "x-gzip"}])
def test_injected_compressed_board(capsys, headers):
    import gzip

    packed = gzip.compress(BOARD)
    reply = (200, packed) if headers is None else (200, headers, packed)
    code = cli.main(["ashby", "search", "quartz"], cli.Context(transport=lambda *args: reply))
    assert code == 0 and len(json.loads(capsys.readouterr().out)["items"]) == 2
