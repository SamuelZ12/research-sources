"""Read one rendered LinkedIn post under a persistent, per-user allowance."""
import fcntl
import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from rsrc import envelope as env, proc
from rsrc.channels import reddit

SCRIPT = Path(__file__).with_name("linkedin_page.js")
OFF = ("LinkedIn is off. Reads load LinkedIn pages as the account signed in to your browser. "
       "LinkedIn's terms restrict automated access and your account can be restricted. "
       "To opt in yourself, set enabled=true in ~/.config/rsrc/linkedin.env.")
GAP = 30
BACKEND = "ego-linkedin"
UTC = timezone.utc


class Failure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def config_dir(environ):
    return Path(environ.get("HOME", str(Path.home()))) / ".config/rsrc"


def settings(folder):
    try:
        lines = (folder / "linkedin.env").read_text().splitlines()
    except FileNotFoundError:
        lines = []
    values = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    try:
        limit = int(values.get("daily_limit", "20"))
        if limit < 1:
            raise ValueError
    except ValueError:
        raise Failure("config", "Set daily_limit to a positive integer in linkedin.env.") from None
    return values.get("enabled") == "true", limit


def target(value):
    try:
        u = urlsplit(value)
        path = unquote(u.path).rstrip("/")
        host_ok = re.fullmatch(r"(?:(?:www|[a-z]{2})\.)?linkedin\.com", u.netloc, re.I)
        m = re.fullmatch(r"/posts/[\w-]*?(?:activity|ugcPost|share)-(\d{19})(?:-[\w-]+)?", path, re.ASCII)
        m = m or re.fullmatch(r"/feed/update/urn:li:(?:activity|ugcPost|share):(\d{19})", path)
        if u.scheme == "https" and host_ok and m:
            return m[1], "https://www.linkedin.com" + path + "/"
    except ValueError:
        pass
    raise Failure("usage", "This command reads posts. Supply a LinkedIn post or feed update URL.")


def locked(reason):
    return (f"LinkedIn is locked ({reason}). Open LinkedIn yourself in Ego Lite and resolve it, "
            "then manually remove ~/.config/rsrc/linkedin.locked.")


def state(folder):
    path = folder / "linkedin-usage.json"
    try:
        row = json.loads(path.read_text())
        if (type(row["used"]) is not int or row["used"] < 0 or not isinstance(row["day"], str)
                or not math.isfinite(row["last"])):
            raise ValueError
        return row
    except FileNotFoundError:
        return {"day": "", "used": 0, "last": 0}
    except (KeyError, ValueError, TypeError):
        raise Failure("config", "LinkedIn usage state is unreadable. Inspect linkedin-usage.json manually.") from None


def store(path, data):
    temp = path.with_suffix(".tmp")
    with temp.open("w") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def register(subparsers):
    p = subparsers.add_parser("linkedin", help="read a LinkedIn post (opt-in)")
    actions = p.add_subparsers(dest="action", required=True)
    read = actions.add_parser("read")
    read.add_argument("url")
    read.set_defaults(run=run)


def run(args, ctx):
    attempts, notes, items = [], [], []
    backend = None
    error = None
    folder = config_dir(ctx.environ)
    try:
        enabled, limit = settings(folder)
        if not enabled:
            raise Failure("config", OFF)
        identity, url = target(args.url)
        with (folder / "linkedin.guard").open("a") as guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Failure("rate_limited", "A LinkedIn read is in progress. Wait for it to finish.") from None
            marker = folder / "linkedin.locked"
            if marker.exists():
                raise Failure("blocked", locked(marker.read_text().strip()))
            now = datetime.now(UTC)
            day = now.date().isoformat()
            row = state(folder)
            used = row["used"] if day == row["day"] else 0
            if used >= limit:
                reset = (now.date() + timedelta(days=1)).isoformat()
                raise Failure("rate_limited", f"LinkedIn daily allowance used. Resets {reset} 00:00 UTC.")
            wait = math.ceil(row["last"] + GAP - now.timestamp())
            if wait > 0:
                raise Failure("rate_limited", f"LinkedIn minimum gap. Wait {wait} seconds.")
            # An interrupted or unobserved navigation requires human inspection.
            store(marker, "previous read did not finish")
            # Reserve room for one post-to-post redirect before releasing any document.
            reserved = min(2, limit - used)
            usage_path = folder / "linkedin-usage.json"
            store(usage_path, json.dumps(dict(day=day, used=used + reserved, last=now.timestamp())))
            name = "rsrc linkedin " + uuid.uuid4().hex
            inp = dict(url=url, id=identity, space=name, allowance=reserved, gap=GAP, day=day, owner_pid=os.getpid())
            script = "const INPUT = " + json.dumps(inp) + ";\n" + SCRIPT.read_text()
            start = time.monotonic()
            backend = BACKEND
            try:
                result = ctx.runner(["ego-browser", "nodejs", "-e", script], 90)
            except proc.Missing:
                marker.unlink()
                raise Failure("config", "Ego Lite is unavailable. Install ego-browser and start Ego Lite.") from None
            except proc.Timeout:
                raise Failure("timeout", locked("previous read did not finish")) from None
            line = reddit.result_line(result, "RSRC_LINKEDIN ")
            if result.code or not line:
                raise Failure("blocked", locked("previous read did not finish"))
            page = json.loads(line[len("RSRC_LINKEDIN "):])
            backend = page.get("backend", BACKEND)
            loads = page["loads"]
            if type(loads) is not int or not 0 <= loads <= reserved:
                raise Failure("upstream_error", locked("invalid load receipt"))
            store(usage_path, json.dumps(dict(day=day, used=used + loads, last=time.time())))
            code = page.get("error")
            attempts.append(env.attempt(backend, code or "ok", loads, env.elapsed_ms(start)))
            if page.get("lock"):
                store(marker, page["lock"])
                raise Failure(code, locked(page["lock"]))
            if code:
                marker.unlink()
                raise Failure(code, page["message"])
            item = page["item"]
            if item["id"] != identity or not item["text"] or not item["author"]:
                raise Failure("upstream_error", "Incomplete browser response. " + locked("previous read did not finish"))
            if not item.get("published_at"):
                d = datetime.fromtimestamp((int(identity) >> 22) / 1000, UTC)
                item["published_at"] = d.isoformat(timespec="seconds")
                notes.append("Approximate UTC time from the requested post ID. The page supplied no publication timestamp.")
            item["raw"]["time_source"] = "id_approximate" if notes else "page"
            items.append(env.item(**item, source="linkedin"))
            items.extend(env.item(**comment, source="linkedin") for comment in page.get("comments", []))
            marker.unlink()
    except Failure as exc:
        error = env.error(exc.code, str(exc))
    return env.envelope("linkedin", "read", None, backend, items, error=error,
                        meta={"attempts": attempts, "notes": notes})
