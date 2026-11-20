"""Load X session cookies: process environment first, then a mode-600 env file.

Cookie values never appear in error messages.
"""

import os
import re
import stat
from pathlib import Path

KEYS = ("AUTH_TOKEN", "CT0")
DEFAULT_PATH = Path("~/.config/rsrc/x.env")


class CookieError(Exception):
    code = "config"


def cookie_file(environ) -> Path:
    """Where the cookie env file is looked for: $RSRC_X_ENV if set, else ~/.config/rsrc/x.env."""
    return Path(environ.get("RSRC_X_ENV") or DEFAULT_PATH).expanduser()


def load_cookies(environ=os.environ, path=None) -> tuple[str, str]:
    """Return (auth_token, ct0). Raises CookieError with a fix-it message."""
    path = Path(path).expanduser() if path else cookie_file(environ)
    values = {k: environ[k].strip() for k in KEYS if environ.get(k, "").strip()}
    if len(values) < len(KEYS) and path.exists():
        for key, value in _read_env_file(path).items():
            if key in KEYS and value:
                values.setdefault(key, value)
    missing = [k for k in KEYS if k not in values]
    if missing:
        raise CookieError(
            f"missing {', '.join(missing)}: set them in {path} (KEY=VALUE lines, chmod 600) "
            "or the environment; copy the auth_token and ct0 cookies from a logged-in x.com browser session"
        )
    for key, value in values.items():
        # Real values are hex. Anything else (newline, quote, separator) would be echoed by
        # urllib's invalid-header error, so reject it here naming only the key.
        if not re.fullmatch(r"[0-9A-Za-z]+", value):
            raise CookieError(f"{key} has unexpected characters; re-copy the cookie value")
    return values["AUTH_TOKEN"], values["CT0"]


def _read_env_file(path: Path) -> dict[str, str]:
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise CookieError(f"{path} is accessible by other users; run: chmod 600 {path}")
    values = {}
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        # The decode error quotes the offending byte, so it is not passed on.
        raise CookieError(f"{path} could not be read as UTF-8 text; recreate it with KEY=VALUE lines") from None
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        if not sep or not key.strip():
            raise CookieError(f"{path} line {number}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values
