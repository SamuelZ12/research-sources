import json
import re
import types

import pytest

from rsrc import cli
from rsrc import envelope as env

REAL_CHANNELS = list(cli.CHANNELS)


def _run_ok(args, ctx):
    return env.envelope("fake", "ok", {"topic": args.topic}, "fake",
                        [env.item(id="1", text=args.topic)])


def _run_empty(args, ctx):
    return env.envelope("fake", "empty", None, "fake")


def _run_fail(args, ctx):
    return env.envelope("fake", "fail", None, "fake", [env.item(id="partial")],
                        error=env.error("upstream_error", "boom"))


def _run_config(args, ctx):
    return env.envelope("fake", "config", None, "fake", error=env.error("config", "missing key"))


def _run_crash(args, ctx):
    raise RuntimeError("secret-ish text")


def _register(subparsers):
    channel = subparsers.add_parser("fake")
    actions = channel.add_subparsers(dest="action_name", required=True)
    p = actions.add_parser("ok")
    p.add_argument("topic")
    p.add_argument("--limit", type=int, default=1)
    p.set_defaults(run=_run_ok, action="ok")
    for name, fn in (("empty", _run_empty), ("fail", _run_fail), ("config", _run_config),
                     ("crash", _run_crash)):
        actions.add_parser(name).set_defaults(run=fn, action=name)


@pytest.fixture(autouse=True)
def fake_channel(monkeypatch):
    monkeypatch.setattr(cli, "CHANNELS", [types.SimpleNamespace(register=_register)])


def _main(capsys, argv):
    code = cli.main(argv, cli.Context(environ={}))
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 1, out
    return code, json.loads(lines[0])


@pytest.mark.parametrize("argv,code", [
    (["fake", "ok", "topic"], 0),
    (["fake", "empty"], 66),
    (["fake", "fail"], 1),
    (["fake", "config"], 2),
])
def test_exit_codes(capsys, argv, code):
    got, envelope = _main(capsys, argv)
    assert got == code
    assert envelope["ok"] is (code in (0, 66))


def test_partial_items_survive_failure(capsys):
    _, envelope = _main(capsys, ["fake", "fail"])
    assert envelope["items"][0]["id"] == "partial"
    assert envelope["error"]["code"] == "upstream_error"


@pytest.mark.parametrize("argv,channel,action", [
    (["nochannel", "x"], "nochannel", "x"),
    (["fake", "ok"], "fake", "ok"),
    (["fake", "ok", "topic", "--limit", "many"], "fake", "ok"),
])
def test_usage_errors(capsys, argv, channel, action):
    code, envelope = _main(capsys, argv)
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "usage"
    assert envelope["channel"] == channel and envelope["action"] == action


def test_help_exits_zero(capsys):
    assert cli.main(["--help"], cli.Context(environ={})) == 0
    assert capsys.readouterr().out.startswith("usage: rsrc")


def test_line_separator_escaped_but_round_trips(capsys):
    cli.main(["fake", "ok", "a\u2028b"], cli.Context(environ={}))
    out = capsys.readouterr().out
    assert "\u2028" not in out and "\\u2028" in out
    assert out.count("\n") == 1
    assert json.loads(out)["items"][0]["text"] == "a\u2028b"


def test_unexpected_exception_gives_envelope_without_message(capsys):
    code, envelope = _main(capsys, ["fake", "crash"])
    assert code == 1
    assert envelope["ok"] is False
    assert envelope["channel"] == "fake" and envelope["action"] == "crash"
    assert envelope["error"] == {"code": "upstream_error", "message": "internal error: RuntimeError"}
    assert "secret-ish" not in json.dumps(envelope)


@pytest.mark.parametrize("argv,channel,action", [
    (["check", "hn", "--bogus"], "check", "check"),
])
def test_usage_action_for_channels_without_action_word(capsys, monkeypatch, argv, channel, action):
    monkeypatch.setattr(cli, "CHANNELS", REAL_CHANNELS)
    code, envelope = _main(capsys, argv)
    assert code == 2
    assert envelope["error"]["code"] == "usage"
    assert envelope["channel"] == channel and envelope["action"] == action


def test_community_is_an_unknown_channel_and_nothing_runs(capsys, monkeypatch):
    monkeypatch.setattr(cli, "CHANNELS", REAL_CHANNELS)
    calls = []
    ctx = cli.Context(environ={}, runner=lambda *args, **kwargs: calls.append(("runner", args)),
                      transport=lambda *args: calls.append(("transport", args)), sleep=lambda s: None)
    code = cli.main(["community", "Claude Code"], ctx)
    envelope = json.loads(capsys.readouterr().out)
    assert code == 2 and envelope["ok"] is False and envelope["error"]["code"] == "usage"
    assert (envelope["channel"], envelope["action"]) == ("community", "Claude Code")  # as for any unknown channel
    message = envelope["error"]["message"]
    assert "invalid choice: 'community'" in message
    listed = message.split("choose from", 1)[1]
    assert set(re.findall(r"'([^']+)'", listed)) == {"x", "reddit", "hn", "linkedin", "youtube", "appstore", "check"}
    assert calls == []


def test_a_lone_surrogate_from_upstream_still_prints_valid_json(capsys):
    cli.print_envelope({"text": "a\ud800b", "warning": "w\udfffx"})
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"text": "a\ud800b", "warning": "w\udfffx"}
