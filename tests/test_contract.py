"""Behaviour every channel shares, run against every registered channel."""

import json
from pathlib import Path

import pytest

from rsrc import cli, proc
from rsrc.channels import appstore, ashby, bluesky, check, gh, hn, polymarket, producthunt, reddit, x, youtube

LOGIN_WALL = (Path(__file__).parent / "fixtures" / "login_wall.html").read_bytes()

# One command per channel that reads a JSON API through rsrc.http.Client.
HTTP_COMMANDS = {
    hn: ["hn", "search", "q"],
    polymarket: ["polymarket", "search", "q"],
    appstore: ["appstore", "search", "q"],
    bluesky: ["bluesky", "search", "q"],
    producthunt: ["producthunt", "search", "q"],
    ashby: ["ashby", "search", "quartz"],
    gh: ["gh", "search", "q"],
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


def no_gh(argv, timeout, **kwargs):
    raise proc.Missing("gh not found")


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
    ctx = cli.Context(environ={}, transport=lambda *request: answer, runner=no_gh, sleep=lambda s: None)
    cli.main(HTTP_COMMANDS[channel], ctx)
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == expected, (
        f"{channel.__name__} answered {answer[0]} with {out['error']}; pass the response through "
        "rsrc.http.raise_refusal before reading it")
