import pytest

from rsrc import envelope as env


def test_item_fills_missing_keys_with_none():
    it = env.item(id="1", url="https://example.com")
    assert tuple(it) == env.ITEM_KEYS
    assert it["id"] == "1" and it["title"] is None and it["raw"] is None


def test_item_rejects_unknown_key():
    with pytest.raises(ValueError):
        env.item(bogus=1)


def test_error_rejects_unknown_code():
    with pytest.raises(ValueError):
        env.error("nope", "x")
    assert env.error("timeout", "slow") == {"code": "timeout", "message": "slow"}


def test_attempt_shape():
    assert env.attempt("gh", "ok") == {"backend": "gh", "outcome": "ok", "requests": None, "ms": None}


def test_elapsed_ms_is_whole_milliseconds_since_the_start(monkeypatch):
    monkeypatch.setattr("time.monotonic", lambda: 12.3456)
    assert env.elapsed_ms(10.0) == 2346


def test_envelope_keys_order_and_defaults():
    e = env.envelope("gh", "search", {"q": "x"}, "gh")
    assert list(e) == ["ok", "channel", "action", "input", "backend", "items", "meta", "error"]
    assert e["ok"] is True and e["items"] == [] and e["error"] is None
    assert list(e["meta"]) == list(env.META_KEYS)
    assert e["meta"] == {"attempts": [], "sources": {}, "rejected": [], "failed": [],
                         "truncated": None, "notes": []}


def test_envelope_copies_items_and_meta_is_not_shared():
    items = (env.item(id="a"),)
    first = env.envelope("c", "a", None, None, items)
    assert first["items"] == [items[0]] and isinstance(first["items"], list)
    first["meta"]["notes"].append("x")
    second = env.envelope("c", "a", None, None)
    assert second["meta"]["notes"] == []

    source = [env.item(id="b")]
    e = env.envelope("c", "a", None, None, source)
    source.append(env.item(id="c"))
    assert len(e["items"]) == 1


def test_envelope_meta_update_and_unknown_key():
    e = env.envelope("c", "a", None, None, meta={"truncated": "request_cap"})
    assert e["meta"]["truncated"] == "request_cap" and e["meta"]["notes"] == []
    with pytest.raises(ValueError):
        env.envelope("c", "a", None, None, meta={"bogus": 1})


def test_ok_is_error_is_none():
    e = env.envelope("c", "a", None, None, error=env.error("blocked", "m"))
    assert e["ok"] is False and e["error"]["code"] == "blocked"


def test_usage_envelope():
    e = env.usage("x", "search", "bad")
    assert e["ok"] is False and e["channel"] == "x" and e["action"] == "search"
    assert e["input"] is None and e["backend"] is None and e["items"] == []
    assert e["error"] == {"code": "usage", "message": "bad"}


@pytest.mark.parametrize("code,expected", [
    ("usage", 2), ("config", 2), ("auth_failed", 1), ("rate_limited", 1), ("blocked", 1),
    ("url_mismatch", 1), ("not_found", 1), ("upstream_error", 1), ("network_error", 1), ("timeout", 1),
])
def test_exit_code_for_errors(code, expected):
    assert env.exit_code(env.envelope("c", "a", None, None, error=env.error(code, "m"))) == expected


def test_exit_code_ok():
    assert env.exit_code(env.envelope("c", "a", None, None, [env.item(id="1")])) == 0
    assert env.exit_code(env.envelope("c", "a", None, None)) == 66


def test_partial_items_with_error_exit_1_and_kept():
    e = env.envelope("c", "a", None, None, [env.item(id="1")], error=env.error("rate_limited", "m"))
    assert env.exit_code(e) == 1
    assert e["items"][0]["id"] == "1"
