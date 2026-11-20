import os

import pytest

from rsrc.channels.x.auth import CookieError, load_cookies


def write_env(tmp_path, text, mode=0o600):
    path = tmp_path / "env"
    path.write_text(text)
    os.chmod(path, mode)
    return path


def test_reads_both_cookies_from_file(tmp_path):
    path = write_env(tmp_path, "# x cookies\nAUTH_TOKEN=tok\n\nCT0=csrf\n")
    assert load_cookies({}, path) == ("tok", "csrf")


def test_environment_overrides_file_per_key(tmp_path):
    path = write_env(tmp_path, "AUTH_TOKEN=filetok\nCT0=filecsrf\n")
    assert load_cookies({"AUTH_TOKEN": "envtok"}, path) == ("envtok", "filecsrf")


def test_export_prefix_and_quotes_are_stripped(tmp_path):
    path = write_env(tmp_path, "export AUTH_TOKEN='tok'\nCT0=\"csrf\"\n")
    assert load_cookies({}, path) == ("tok", "csrf")


def test_file_not_needed_when_environment_has_both(tmp_path):
    path = write_env(tmp_path, "AUTH_TOKEN=filetok\n", mode=0o644)
    assert load_cookies({"AUTH_TOKEN": "a", "CT0": "b"}, path) == ("a", "b")


def test_env_var_selects_file_path(tmp_path):
    path = write_env(tmp_path, "AUTH_TOKEN=tok\nCT0=csrf\n")
    assert load_cookies({"RSRC_X_ENV": str(path)}) == ("tok", "csrf")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def cookie_file(path):
    path.parent.mkdir(parents=True)
    path.write_text("AUTH_TOKEN=tok\nCT0=csrf\n")
    os.chmod(path, 0o600)
    return path


def test_default_cookie_file_is_config_rsrc_x_env(home):
    cookie_file(home / ".config" / "rsrc" / "x.env")
    assert load_cookies({}) == ("tok", "csrf")


def test_cookie_file_only_at_the_retired_path_is_ignored(home, retired):
    cookie_file(home / ".config" / retired.dir / "env")
    with pytest.raises(CookieError) as exc:
        load_cookies({})
    assert str(home / ".config" / "rsrc" / "x.env") in str(exc.value)
    assert retired.dir not in str(exc.value)


def test_retired_override_variable_is_ignored(home, retired):
    elsewhere = cookie_file(home / "elsewhere" / "env")
    with pytest.raises(CookieError):
        load_cookies({retired.var: str(elsewhere)})


def test_group_readable_file_is_refused_without_leaking_values(tmp_path):
    path = write_env(tmp_path, "AUTH_TOKEN=SECRET-TOK\nCT0=SECRET-CSRF\n", mode=0o640)
    with pytest.raises(CookieError) as exc:
        load_cookies({}, path)
    assert "chmod 600" in str(exc.value)
    assert "SECRET" not in str(exc.value)


def test_malformed_line_reports_line_number_not_content(tmp_path):
    path = write_env(tmp_path, "AUTH_TOKEN=tok\nSECRET-CSRF-NO-EQUALS\n")
    with pytest.raises(CookieError) as exc:
        load_cookies({}, path)
    assert "line 2" in str(exc.value)
    assert "SECRET" not in str(exc.value)


def test_missing_cookie_names_key_and_file(tmp_path):
    path = write_env(tmp_path, "AUTH_TOKEN=tok\n")
    with pytest.raises(CookieError) as exc:
        load_cookies({}, path)
    assert "CT0" in str(exc.value)
    assert str(path) in str(exc.value)


def test_missing_file_and_environment_is_an_error(tmp_path):
    with pytest.raises(CookieError) as exc:
        load_cookies({}, tmp_path / "absent")
    assert "AUTH_TOKEN" in str(exc.value)


def test_trailing_whitespace_is_stripped_from_values(tmp_path):
    path = write_env(tmp_path, "AUTH_TOKEN=abc123\r\nCT0=def456 \n")
    assert load_cookies({}, path) == ("abc123", "def456")


@pytest.mark.parametrize("bad", ["TOKEN\nSENTINEL", "TOKEN;SENTINEL", "“TOKENSENTINEL”"])
def test_malformed_value_names_key_without_leaking_it(bad):
    with pytest.raises(CookieError) as exc:
        load_cookies({"AUTH_TOKEN": bad, "CT0": "def456"}, "/nonexistent")
    assert "AUTH_TOKEN" in str(exc.value)
    assert "SENTINEL" not in str(exc.value)


def test_undecodable_file_is_a_cookie_error_without_leaking_content(tmp_path):
    path = tmp_path / "env"
    path.write_bytes(b"AUTH_TOKEN=SECRET\xff\nCT0=csrf\n")
    os.chmod(path, 0o600)
    with pytest.raises(CookieError) as exc:
        load_cookies({}, path)
    assert str(path) in str(exc.value)
    assert "SECRET" not in str(exc.value) and "xff" not in str(exc.value)
