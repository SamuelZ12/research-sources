"""Select the local Chrome instance used by X commands."""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from rsrc import chrome
from rsrc.channels.x.auth import CookieError, chrome_settings, chrome_settings_path

ORIGIN = "https://x.com"
STARTED = "Started Google Chrome with the dedicated rsrc profile."


def save(environ, endpoint, managed):
    path = chrome_settings_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as f:
        json.dump({"backend": "x-chrome", "managed_endpoint" if managed else "endpoint": endpoint}, f)
    os.replace(f.name, path)


def executable():
    bundle = Path("Google Chrome.app/Contents/MacOS/Google Chrome")
    candidates = [Path("/Applications") / bundle, Path.home() / "Applications" / bundle]
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if root := os.environ.get(key):
            candidates.append(Path(root) / "Google/Chrome/Application/chrome.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("google-chrome") or shutil.which("google-chrome-stable")


def active(profile):
    try:
        port, path = (profile / "DevToolsActivePort").read_text().splitlines()
        endpoint = f"http://127.0.0.1:{int(port)}"
        if chrome.discover(endpoint).path == path:
            return endpoint
    except (OSError, ValueError, chrome.ChromeError):
        pass


def profile_path(environ):
    return Path(environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "rsrc/chrome-profile"


def managed_browser(environ, signin=False):
    profile = profile_path(environ)
    endpoint = active(profile)
    if endpoint and not signin:
        return endpoint, False
    binary = executable()
    if not binary:
        raise CookieError("Google Chrome is not installed. Install Google Chrome, then run rsrc x connect.")
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    args = [binary, f"--user-data-dir={profile.absolute()}", "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
            ORIGIN if signin else "about:blank"]
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    for _ in range(100):
        if ready := active(profile):
            return ready, not endpoint
        time.sleep(.1)
    raise chrome.ChromeError(chrome.UNREACHABLE)


def resolve(environ):
    config = chrome_settings(environ)
    if not config:
        raise CookieError("Chrome is not configured. Run rsrc x connect.")
    endpoint, managed = config
    if managed:
        ready, started = managed_browser(environ)
        if ready != endpoint:
            save(environ, ready, True)
        return ready, started
    return endpoint, False
