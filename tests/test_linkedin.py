import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from rsrc import cli, proc
from rsrc.channels import linkedin as li

ID = "7444787076895637504"
URL = "https://www.linkedin.com/feed/update/urn:li:activity:" + ID + "/"


@pytest.fixture
def setup(tmp_path):
    ctx = cli.Context(environ={"HOME": str(tmp_path)})
    folder = li.config_dir(ctx.environ)
    folder.mkdir(parents=True)
    (folder / "linkedin.env").write_text("enabled=true\ndaily_limit=2")
    return folder, ctx, SimpleNamespace(url=URL)


@pytest.mark.parametrize("url", ["https://linkedin.com/in/alex", "https://linkedin.com/company/x",
                                 "https://linkedin.com/jobs/view/20", "https://linkedin.com.evil.test/posts/a"])
def test_usage_before_browser(setup, url):
    _, ctx, args = setup
    args.url = url
    ctx.runner = lambda *_: pytest.fail("browser opened")
    assert li.run(args,ctx)["error"]["code"] == "usage"


def test_off_every_action(setup, capsys):
    folder, ctx, _ = setup
    (folder / "linkedin.env").unlink()
    ctx.runner = lambda *_: pytest.fail("browser opened")
    assert cli.main(["linkedin", "read", URL], ctx) == 2
    assert json.loads(capsys.readouterr().out)["error"]["message"] == li.OFF


def reply(value):
    return proc.Result(0, "RSRC_LINKEDIN " + json.dumps({"loads": 1, **value}), "")


def test_count_before_dispatch_and_lock(setup):
    folder, ctx, args = setup
    started, finish = Event(), Event()
    def run(*_):
        assert li.state(folder)["used"] == 2
        started.set()
        assert finish.wait(3)
        return reply({"error":"rate_limited","lock":"HTTP 429"})
    ctx.runner = run
    with ThreadPoolExecutor() as pool:
        first = pool.submit(li.run,args,ctx)
        assert started.wait(3)
        assert li.run(args,ctx)["error"]["code"] == "rate_limited"
        finish.set()
        assert first.result()["error"]["code"] == "rate_limited"
    assert "HTTP 429" in li.run(args,ctx)["error"]["message"]


def test_signin_locks_channel(setup):
    folder,ctx,args = setup
    ctx.runner = lambda *_: reply({"error":"auth_failed","lock":"sign-in required"})
    assert li.run(args,ctx)["error"]["code"] == "auth_failed"
    assert "sign-in required" in (folder/"linkedin.locked").read_text()


def test_gap_cap(setup):
    folder,ctx,args = setup
    ctx.runner = lambda *_: reply({"error":"not_found","message":"Post unavailable"})
    assert li.run(args,ctx)["error"]["code"] == "not_found"
    assert "minimum gap" in li.run(args,ctx)["error"]["message"]
    row = li.state(folder)
    row["used"] = 2
    li.store(folder/"linkedin-usage.json",json.dumps(row))
    assert "Resets" in li.run(args,ctx)["error"]["message"]


def test_crash_stays_closed(setup):
    folder,ctx,args = setup
    def crash(*_):
        raise proc.Timeout()
    ctx.runner = crash
    assert li.run(args,ctx)["error"]["code"] == "timeout"
    assert li.state(folder)["used"] == 2
    assert li.run(args,ctx)["error"]["code"] == "blocked"


def test_explicit_check_is_local(setup, capsys):
    _, ctx, _ = setup
    ctx.runner = ctx.transport = lambda *_: pytest.fail("external request")
    assert cli.main(["check", "linkedin"], ctx) == 0
    raw = json.loads(capsys.readouterr().out)["items"][0]["raw"]
    assert raw == {"enabled": True, "locked": False, "daily_limit": 2, "used": 0}


def test_mismatched_post_never_succeeds(setup):
    _, ctx, args = setup
    ctx.runner = lambda *_: reply({"item": {"id": "other"}})
    result = li.run(args, ctx)
    assert not result["ok"] and not result["items"]
    assert result["error"]["code"] == "upstream_error"


def test_rendered_post_and_comments(setup):
    from pathlib import Path

    folder, ctx, args = setup
    fixture = Path(__file__).parent / "fixtures/linkedin_public_post.json"
    page = json.loads(fixture.read_text())
    args.url = page["item"]["url"]
    ctx.runner = lambda *_: reply(page)
    out = li.run(args, ctx)
    assert out["ok"] and out["items"][0]["author"] == "Aaru"
    assert out["items"][0]["raw"]["time_source"] == "id_approximate"
    assert out["items"][0]["engagement"] == {"reactions": 450, "comments": 29, "reposts": 26}
    assert [i["raw"]["kind"] for i in out["items"]] == ["post"] + ["comment"] * 4
    assert li.state(folder)["used"] == 1


def test_explicit_page_timestamp(setup):
    _, ctx, args = setup
    date = "2026-07-01T12:00:00Z"
    ctx.runner = lambda *_: reply({"item": dict(id=ID, author="A company", text="Example",
                                               published_at=date, raw={"kind":"post"})})
    item = li.run(args,ctx)["items"][0]
    assert item["published_at"] == date and item["raw"]["time_source"] == "page"
