import argparse
import json
import re
from pathlib import Path

import pytest

from rsrc import cli, proc
from rsrc.channels import appstore, ashby, bluesky, check, hn, linkedin, polymarket, producthunt, reddit, x, youtube

FIXTURES = Path(__file__).parent / "fixtures"
LOGIN_WALL = (FIXTURES / "login_wall.html").read_bytes()

HTTP_COMMANDS = {
    linkedin: ["linkedin", "read", check.LINKEDIN_POST],
    hn: ["hn", "search", "q"],
    polymarket: ["polymarket", "search", "q"],
    appstore: ["appstore", "search", "q"],
    bluesky: ["bluesky", "search", "q"],
    producthunt: ["producthunt", "search", "q"],
    ashby: ["ashby", "search", "quartz"],
}
NOT_HTTP = {
    x: "FxTwitter's 403 and 404 carry post states, and the signed routes report 401 and 403 as auth_failed",
    reddit: "pages load in Ego; the one HTTP request reads a share-link redirect",
    youtube: "yt-dlp runs as a subprocess",
    check: "runs the probes of the other channels",
}


def test_every_channel_is_in_the_contract():
    unlisted = [m.__name__ for m in cli.CHANNELS if m not in HTTP_COMMANDS and m not in NOT_HTTP]
    assert not unlisted, (f"{unlisted}: add each to HTTP_COMMANDS in tests/test_contract.py and pass its "
                          "responses through rsrc.http.raise_refusal, or to NOT_HTTP with the reason it reads no JSON API")


def no_programs(argv, timeout, **kwargs):
    raise proc.Missing(f"{argv[0]} not found")


JSON, PAGE = {"Content-Type": "application/json"}, {"Content-Type": "text/html"}


@pytest.mark.parametrize("answer, expected", [
    ((429, JSON, b"{}"), "rate_limited"),
    ((401, JSON, b"{}"), "auth_failed"),
    ((403, JSON, b"{}"), "blocked"),
    ((200, PAGE, LOGIN_WALL), "blocked"),
    ((503, PAGE, LOGIN_WALL), "blocked"),
], ids=["429", "401", "403", "200-page", "503-page"])
@pytest.mark.parametrize("channel", HTTP_COMMANDS, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_a_refusal_has_one_error_code_on_every_channel(capsys, channel, answer, expected):
    ctx = cli.Context(environ={}, transport=lambda *request: answer, runner=no_programs, sleep=lambda s: None)
    cli.main(HTTP_COMMANDS[channel], ctx)
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == expected, (
        f"{channel.__name__} answered {answer[0]} with {out['error']}; pass the response through "
        "rsrc.http.raise_refusal before reading it")


def test_x_thread_stops_on_a_rate_limit(run_x):
    calls = []

    def transport(method, url, headers, body):
        calls.append(url)
        return 429, JSON, b"{}"

    code, out, _ = run_x(["thread", "1917421753820930420"], transport)
    assert code == 1 and not out["ok"] and out["error"]["code"] == "rate_limited"
    assert calls == ["https://api.fxtwitter.com/2/thread/1917421753820930420"]
    assert out["meta"]["attempts"][0]["requests"] == 1


CAPPED_COMMANDS = {
    ("x", "search"): ["x", "search", "q", "--backend", "fxtwitter"],
    ("x", "search", "x-graphql"): ["x", "search", "q", "--backend", "x-graphql"],
    ("x", "search", "x-chrome"): ["x", "search", "q", "--backend", "x-chrome"],
    ("x", "timeline", "x-chrome"): ["x", "timeline", "jack", "--backend", "x-chrome"],
    ("x", "timeline"): ["x", "timeline", "jack", "--backend", "fxtwitter"],
    ("x", "timeline", "x-graphql"): ["x", "timeline", "jack", "--backend", "x-graphql"],
    ("bluesky", "search"): ["bluesky", "search", "q"],
    ("bluesky", "read"): ["bluesky", "read", "at://bsky.app/app.bsky.feed.post/3l6oveex3ii2l"],
    ("bluesky", "replies"): ["bluesky", "replies", "at://bsky.app/app.bsky.feed.post/3l6oveex3ii2l"],
    ("bluesky", "timeline"): ["bluesky", "timeline", "bsky.app"],
    ("producthunt", "search"): ["producthunt", "search", "q"],
    ("producthunt", "read"): ["producthunt", "read", "linear"],
    ("producthunt", "leaderboard"): ["producthunt", "leaderboard", "--date", "2026-11-21"],
    ("appstore", "read"): ["appstore", "read", "874139669"],
}


def commands(parser, path=()):
    subparsers = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subparsers:
        yield path, parser
    for name, command in ((n, c) for s in subparsers for n, c in s.choices.items()):
        yield from commands(command, (*path, name))


def commands_with(option):
    return [path for path, parser in commands(cli.build_parser())
            if any(option in a.option_strings for a in parser._actions)]


def test_every_capped_command_is_in_the_contract():
    unlisted = [c for c in commands_with("--max-requests") if c not in CAPPED_COMMANDS]
    assert not unlisted, f"{unlisted}: add each to CAPPED_COMMANDS in tests/test_contract.py"


@pytest.mark.parametrize("answer", [(404, {}, b"{}"), (500, {}, b"{}"), OSError("connection reset")],
                         ids=["404", "500", "network"])
@pytest.mark.parametrize("cap", [1, 2, 3])
@pytest.mark.parametrize("command", CAPPED_COMMANDS, ids="-".join)
def test_the_cap_never_hides_a_failure(capsys, tmp_path, command, cap, answer):
    def transport(*request):
        if isinstance(answer, Exception):
            raise answer
        return answer

    environ = {"AUTH_TOKEN": "TOKENSENTINEL", "CT0": "CSRFSENTINEL", "XDG_CACHE_HOME": str(tmp_path),
               "RSRC_X_ENV": str(tmp_path / "missing-env")}
    ctx = cli.Context(environ=environ, transport=transport, runner=no_programs, sleep=lambda s: None)
    cli.main([*CAPPED_COMMANDS[command], "--max-requests", str(cap)], ctx)
    out = json.loads(capsys.readouterr().out)
    assert not out["ok"] and out["meta"]["truncated"] is None, (
        f"every answer failed, but the cap ended the run as {out['error'] or 'a result'} "
        f"(truncated={out['meta']['truncated']}). A retry loop must iterate client.tries(...) and then "
        "raise the failure it was retrying")


def recorded(name):
    return json.loads((FIXTURES / name).read_text())


def answer(data):
    return 200, json.dumps(data).encode()


def twice(data, *path):
    rows = data
    for key in path:
        rows = rows[key]
    rows.extend(json.loads(json.dumps(rows)))
    return data


def mcp(name):
    data = twice(recorded(f"producthunt/{name}.json"), "result", "structuredContent", "data", "results")
    return {**data, "id": 1}


LIMITED_COMMANDS = {
    ("hn", "search"): (["hn", "search", "q"], lambda: [recorded("hn_search.json")]),
    ("polymarket", "search"): (["polymarket", "search", "q"], lambda: [recorded("polymarket_search_p1.json")]),
    ("appstore", "search"): (["appstore", "search", "q"],
                             lambda: [{**twice(recorded("appstore/wiki.json"), "results"), "resultCount": 2}]),
    ("bluesky", "search"): (["bluesky", "search", "q"], lambda: [twice(recorded("bluesky_search.json"), "posts")]),
    ("producthunt", "search"): (["producthunt", "search", "q"], lambda: [mcp("search")]),
    ("producthunt", "leaderboard"): (["producthunt", "leaderboard", "--date", "2026-11-21"], lambda: [mcp("board")]),
    ("ashby", "search"): (["ashby", "search", "quartz"], lambda: [recorded("ashby/board.json")]),
}
LIMIT_COUNTS_PART = {
    ("x", "search"): "unique posts across date windows; test_x_windows.py covers --limit",
    ("x", "replies"): "replies after the post itself",
    ("x", "timeline"): "posts after the account item",
    ("bluesky", "replies"): "replies after the post itself",
    ("bluesky", "timeline"): "posts after the profile item",
    ("appstore", "read"): "reviews after the app item",
    ("reddit", "search"): "posts; --with-comments adds comments",
}
NO_FIXTURE = {
    ("youtube", "search"): "yt-dlp is asked for exactly N results (ytsearchN)",
}


def test_every_limited_command_is_in_the_contract():
    listed = LIMITED_COMMANDS.keys() | LIMIT_COUNTS_PART.keys() | NO_FIXTURE.keys()
    unlisted = [c for c in commands_with("--limit") if c not in listed]
    assert not unlisted, (f"{unlisted}: add each to LIMITED_COMMANDS in tests/test_contract.py with an answer "
                          "holding more rows than --limit 1, or say what its limit counts")


@pytest.mark.parametrize("command", LIMITED_COMMANDS, ids="-".join)
def test_limit_holds_when_the_source_sends_more(capsys, command):
    argv, answers = LIMITED_COMMANDS[command]
    replies = iter(map(answer, answers()))
    ctx = cli.Context(environ={}, transport=lambda *request: next(replies), runner=no_programs, sleep=lambda s: None)
    cli.main([*argv, "--limit", "1"], ctx)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"], out["error"]
    assert len(out["items"]) == 1, (f"{len(out['items'])} items for --limit 1: the source sent more rows than "
                                    "asked for, so slice the rows to the limit before building items")


def test_every_option_says_what_it_does_and_its_default():
    missing = []
    for _, command in commands(cli.build_parser()):
        for option in command._actions:
            if not option.option_strings or isinstance(option, argparse._HelpAction):
                continue
            text = command._get_formatter()._expand_help(option) if option.help else ""
            value = re.escape(str(option.default))
            if not text or option.default not in (None, False) and not re.search(
                    rf"default:? {value}\b|\b{value} \(default\)", text):
                missing.append(f"{command.prog} {option.option_strings[0]}")
    assert not missing, (f"{missing}: give each option help that ends with (default %(default)s) "
                         "when it has a default")
