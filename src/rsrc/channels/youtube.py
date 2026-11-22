"""YouTube via the `yt-dlp` program: `search` (the results page, one run) and `read` (one video's
details and the transcript of its original audio, taken from its caption track).

Every yt-dlp run goes through `ctx.runner` with fixed flags and a four-variable environment: no config
file, no cookie, no login, no named player client, no retry, and no proxy flag or variable (yt-dlp
then asks the operating system for one). A rate limit or a block stops the command at once. Which
caption track is the original audio's is decided in `youtube_original`.
"""

import http.client
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import getproxies

from rsrc import envelope as env
from rsrc import proc
from rsrc.channels import youtube_original as policy

CHANNEL = "youtube"
BACKEND = "yt-dlp"
SOURCE = "youtube"
WATCH = "https://www.youtube.com/watch?v="
# No player client is named: a forced client made every video warn, and a warning ends a read.
FIXED = ("--ignore-config", "--no-cookies-from-browser", "--retries", "0", "--extractor-retries", "0")
# No proxy variable is passed on. With none named yt-dlp asks the operating system, so on macOS a
# proxy set in System Settings would still be used.
PASSED_ENV = ("PATH", "HOME", "LANG", "TMPDIR")
SEARCH_TIMEOUT = 60
META_TIMEOUT = 30
CAPTION_TIMEOUT = 30
AUDIO_TIMEOUT = 30
ASR_TIMEOUT = 60
READ_CEILING = 100  # seconds for a whole `read`
MIB = 1024 * 1024
# The metadata is read whole (11.1 MB for a dubbed video). Two ways to shrink it were ruled out on
# 2026-11-14: `-O` with a field list still carries the caption map, and `youtube:skip=translated_subs`
# skips only the translations of uploaded subtitles.
META_LIMIT = 32 * MIB  # bytes of metadata read from yt-dlp before the run is stopped
MAX_AUDIO = 50 * MIB  # bytes of audio given to speech recognition
MAX_CAPTION = 4 * MIB  # bytes of caption file read
MODEL = "mlx-community/Qwen3-ASR-1.7B-8bit"
MAX_LIMIT = 20
DEFAULT_WORDS = 5000
MAX_WORDS = 40000
STDERR_TAIL = 200
WARNING_HEAD = 200

# The fields of yt-dlp's metadata kept in a read item's `raw`. The rest (hundreds of kilobytes of
# formats, thumbnails and caption rows, with addresses that hold the caller's IP address) is dropped.
RECORD = ("id", "title", "description", "channel", "channel_id", "channel_url", "uploader_id",
          "channel_follower_count", "timestamp", "upload_date", "duration", "view_count", "like_count",
          "comment_count", "language", "live_status", "availability", "age_limit", "categories", "tags", "chapters")

# Phrases yt-dlp prints on stderr, lower case. yt-dlp reports a failure only as a non-zero exit
# status and this text.
_RATE_LIMIT = ("http error 429", "too many requests", "rate-limit")
_AGE_CHECK = ("confirm your age",)
_BLOCK = ("http error 401", "http error 403", "sign in to confirm", "not a bot", "captcha", "authentication required")
_GONE = ("video unavailable", "private video", "has been removed")
# What `raw.transcript.status` says when the caption or audio run fails; any other code is upstream_error.
_RUN_STATUS = {"rate_limited": "source_blocked", "blocked": "source_blocked", "timeout": "timeout"}

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_BLANK = re.compile(r"[\x00-\x20\x7f]")  # urlsplit silently drops tabs and newlines inside a URL
_HOSTS = ("www.youtube.com", "youtube.com", "m.youtube.com")
_ID_PATH = re.compile(r"/(shorts|live|embed)/([^/]+)")
_EMBED_PLAYERS = ("videoseries", "live_stream")  # under /embed/: a playlist's and a channel's player, not a video
# An address in yt-dlp's stderr. The last two forms are a stream host and a stream path printed without
# a scheme, as an HTTP library words a failed connection.
_URL = re.compile(r"https?://\S+|\S*googlevideo\.com\S*|\S*[?&]ip=\S*", re.IGNORECASE)


def register(subparsers) -> None:
    parser = subparsers.add_parser("youtube", help="YouTube video search, details and transcript via yt-dlp (no key)")
    actions = parser.add_subparsers(dest="youtube_action", metavar="ACTION", required=True)

    search = actions.add_parser("search", help="search videos (the results page: no exact date, no likes)")
    search.add_argument("query", metavar="QUERY")
    search.add_argument("--limit", type=int, default=10, help=f"videos, 1-{MAX_LIMIT} (default 10)")
    search.set_defaults(run=run_search, action="search")

    read = actions.add_parser("read", help="one video's details and the transcript of its original audio")
    read.add_argument("video", metavar="URL_OR_ID")
    read.add_argument("--words", type=int, default=DEFAULT_WORDS,
                      help=f"transcript words returned, 1-{MAX_WORDS} (default {DEFAULT_WORDS})")
    read.set_defaults(run=run_read, action="read")


class _Failed(Exception):
    def __init__(self, code: str, message: str, rejected: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.rejected = rejected or []


class _DirectFailed(_Failed):
    """The direct caption GET failed in a way yt-dlp's own downloader may not: the read falls back to it.

    Never raised for a rate limit, a login or bot check, a timeout or an oversized file."""


class _YtDlp:
    """Counts a command's yt-dlp runs and direct caption request in its one `meta.attempts` entry.

    `ceiling`: seconds the whole command may take; each run gets what is left of it at most."""

    def __init__(self, ctx, ceiling: float | None = None):
        self.runner = ctx.runner or proc.run
        self.environ = {key: ctx.environ[key] for key in PASSED_ENV if key in ctx.environ}
        self.runs = 0
        self.started = time.monotonic()
        self.ceiling = ceiling

    def left(self, limit: float) -> float:
        """`limit`, or what is left of the ceiling when that is less; 0 or below means no time is left."""
        if self.ceiling is None:
            return limit
        return min(limit, self.started + self.ceiling - time.monotonic())

    def need(self, *programs: str) -> None:
        """`config` unless each program is on the PATH the runs get. Makes no run."""
        for name in programs:
            if shutil.which(name, path=self.environ.get("PATH", "")) is None:
                raise _Failed("config", f"{name} not found on PATH")

    def run(self, args: list[str], timeout: float, *, one_video: bool = False, **limits) -> proc.Result:
        """One run that exited 0. Raises _Failed, whose `code` is the envelope code; nothing is retried.

        `one_video`: the run names one video, so yt-dlp saying it is unavailable means `not_found`.
        `limits`: `proc.run`'s `stdout_limit` and `file_limit`."""
        timeout = self.left(timeout)
        if timeout <= 0:
            raise _Failed("timeout", f"the {self.ceiling:g} s for the whole command ran out before a yt-dlp run")
        self.runs += 1
        try:
            result = self.runner(["yt-dlp", *FIXED, *args], timeout, env=self.environ, **limits)
        except proc.Missing:
            raise _Failed("config", "yt-dlp not found on PATH") from None
        except proc.Timeout:
            raise _Failed("timeout", f"yt-dlp did not finish within {timeout:g} s") from None
        except proc.Overflow:
            raise _Failed("upstream_error",
                          f"yt-dlp printed more than {limits['stdout_limit'] // MIB} MiB; stopped reading") from None
        if result.code != 0:
            raise _Failed(*classify(result, one_video=one_video))
        return result

    def attempt(self, outcome: str) -> dict:
        return env.attempt(BACKEND, outcome, self.runs, env.elapsed_ms(self.started))


def _tail(stderr: str) -> str:
    """The last 200 characters of stderr for a message, with every address removed first: a
    stream or caption address holds the caller's IP address."""
    return _URL.sub("<url>", stderr).strip()[-STDERR_TAIL:]


def classify(result: proc.Result, *, one_video: bool = False) -> tuple[str, str]:
    """Error code and message for a yt-dlp run that exited non-zero.

    The age check is tested before the block phrases: "Sign in to confirm your age" would
    otherwise read as a block, and it is one video's restriction, not a refusal of this client."""
    tail = _tail(result.stderr)
    low = result.stderr.lower()

    def has(phrases) -> bool:
        return any(phrase in low for phrase in phrases)

    if has(_RATE_LIMIT):
        return "rate_limited", f"YouTube rate limit; stopped without retrying: {tail}"
    if has(_AGE_CHECK):
        return "upstream_error", f"age-restricted video: YouTube asks for a login, which rsrc never uses: {tail}"
    if has(_BLOCK):
        return "blocked", f"YouTube refused the request (bot check or login wall); stopped without retrying: {tail}"
    if one_video and has(_GONE):
        return "not_found", f"YouTube has no such video to show: {tail}"
    message = f"yt-dlp exited with code {result.code}"
    return "upstream_error", f"{message}: {tail}" if tail else message


def run_search(args, ctx) -> dict:
    input_ = {"query": args.query, "limit": args.limit}
    if not args.query.strip():
        return env.usage(CHANNEL, "search", "QUERY is empty")
    if not 1 <= args.limit <= MAX_LIMIT:
        return env.usage(CHANNEL, "search", f"--limit must be between 1 and {MAX_LIMIT}, got {args.limit}")
    ytdlp = _YtDlp(ctx)
    try:
        # No deno here: a search reads the results page and opens no video, so no JavaScript runs.
        ytdlp.need("yt-dlp")
        result = ytdlp.run(["--flat-playlist", "--dump-json", "--extractor-args", "youtubetab:approximate_date",
                            f"ytsearch{args.limit}:{args.query}"], SEARCH_TIMEOUT)
        records = _search_lines(result.stdout)
    except _Failed as failure:
        return env.envelope(CHANNEL, "search", input_, BACKEND, meta={"attempts": [ytdlp.attempt(failure.code)]},
                            error=env.error(failure.code, failure.message))
    items = [_search_item(r) for r in records]
    notes = []
    if any(item["published_at"] for item in items):
        notes.append("Search published_at dates are approximate, derived from the ages shown by YouTube.")
    return env.envelope(CHANNEL, "search", input_, BACKEND, items,
                        meta={"attempts": [ytdlp.attempt("ok" if items else "no_results")], "notes": notes})


def _search_lines(stdout: str) -> list[dict]:
    """The results yt-dlp printed, one JSON object per line, each video once, in its order."""
    records: dict[str, dict] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):  # RecursionError: nesting too deep for the JSON parser
            record = None
        video_id = record.get("id") if isinstance(record, dict) else None
        if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
            raise _Failed("upstream_error", "yt-dlp printed a search result that is not a video record")
        records.setdefault(video_id, record)
    return list(records.values())


def _search_item(record: dict) -> dict:
    date = _day(record.get("upload_date")) or _utc(record.get("timestamp"))
    return env.item(
        id=record["id"],
        url=WATCH + record["id"],
        title=_text(record.get("title")),
        author=_text(record.get("channel")),
        published_at=date[:10] if date else None,
        text=_text(record.get("description")),
        source=SOURCE,
        engagement=_counts(views=record.get("view_count")),
        raw=record,
    )


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _counts(**counts) -> dict | None:
    """Only the integer counts yt-dlp reported; None if there are none."""
    kept = {k: v for k, v in counts.items() if isinstance(v, int) and not isinstance(v, bool)}
    return kept or None


def run_read(args, ctx) -> dict:
    input_ = {"video": args.video, "words": args.words}
    if not 1 <= args.words <= MAX_WORDS:
        return env.usage(CHANNEL, "read", f"--words must be between 1 and {MAX_WORDS}, got {args.words}")
    video_id = parse_video(args.video)
    if video_id is None:
        return env.usage(CHANNEL, "read", f"expected a YouTube video URL or an 11-character video ID, got {args.video!r}")
    ytdlp = _YtDlp(ctx, READ_CEILING)
    notes: list[str] = []
    try:
        ytdlp.need("yt-dlp", "deno")  # without deno yt-dlp warns on every video, which reads as incomplete metadata
        info, warning = _metadata(ytdlp, video_id)
    except _Failed as failure:
        return env.envelope(CHANNEL, "read", input_, BACKEND,
                            meta={"attempts": [ytdlp.attempt(failure.code)], "rejected": failure.rejected},
                            error=env.error(failure.code, failure.message))
    text, transcript, failure = _transcript(ytdlp, video_id, info, args.words, notes, warning)
    if warning is not None:
        notes.append(f"yt-dlp warned on the metadata run: {warning}")
    # A caption or audio run that failed still leaves the details: the item is returned with the error.
    return env.envelope(CHANNEL, "read", input_, BACKEND, [_video_item(video_id, info, text, transcript)],
                        meta={"attempts": [ytdlp.attempt(failure.code if failure else "ok")], "notes": notes},
                        error=env.error(failure.code, failure.message) if failure else None)


def probe(ctx, video_id: str) -> tuple[dict, dict, tuple[str, str] | None]:
    """For `rsrc check`: what `read` needs on PATH, then the metadata run every `read` starts with.

    Returns what was found, the attempt, and (code, message) unless all is well. A warning on a run
    that exits 0 is a failure here: with one, every `read` returns `incomplete_metadata`."""
    ytdlp = _YtDlp(ctx)
    found = {"video": video_id, "yt_dlp_version": None,
             "vidscribe": shutil.which("vidscribe", path=ytdlp.environ.get("PATH", "")) is not None}
    try:
        ytdlp.need("yt-dlp", "deno")
        info, warning = _metadata(ytdlp, video_id)
        version = info.get("_version")
        found["yt_dlp_version"] = _text(version.get("version")) if isinstance(version, dict) else None
        if warning is not None:
            raise _Failed("upstream_error", f"yt-dlp warned on the metadata run, so `read` would return no "
                                            f"transcript: {warning}")
    except _Failed as failure:
        return found, ytdlp.attempt(failure.code), (failure.code, failure.message)
    return found, ytdlp.attempt("ok"), None


def parse_video(value: str) -> str | None:
    """The video ID from a bare ID or the URL of one video; None for anything else (a playlist, a
    channel, another site, `http://`)."""
    value = value.strip()
    if _VIDEO_ID.fullmatch(value):
        return value
    if _BLANK.search(value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    host = parts.netloc.lower()
    if parts.scheme != "https" or host not in (*_HOSTS, "youtu.be"):
        return None
    if host == "youtu.be":
        candidates = [parts.path[1:]]
    elif parts.path == "/watch":
        candidates = parse_qs(parts.query, keep_blank_values=True).get("v", [])
    else:
        match = _ID_PATH.fullmatch(parts.path)
        player = match is not None and match[1] == "embed" and match[2] in _EMBED_PLAYERS
        candidates = [match[2]] if match and not player else []
    return candidates[0] if len(candidates) == 1 and _VIDEO_ID.fullmatch(candidates[0]) else None


def _metadata(ytdlp: _YtDlp, video_id: str) -> tuple[dict, str | None]:
    """One video's metadata, and what yt-dlp warned about while loading it (None: nothing)."""
    result = ytdlp.run(["--no-playlist", "--dump-single-json", "--skip-download", WATCH + video_id], META_TIMEOUT,
                       one_video=True, stdout_limit=META_LIMIT)
    try:
        info = json.loads(result.stdout)
    except (ValueError, RecursionError):  # RecursionError: nesting too deep for the JSON parser
        info = None
    if not isinstance(info, dict):
        tail = _tail(result.stderr)
        raise _Failed("upstream_error", "yt-dlp printed metadata that is not a JSON object" + (f": {tail}" if tail else ""))
    returned = info.get("id")
    if returned != video_id:
        if isinstance(returned, str) and _VIDEO_ID.fullmatch(returned):
            raise _Failed("url_mismatch", f"asked for video {video_id}, yt-dlp returned {returned}",
                          rejected=[{"url": WATCH + returned, "reason": "url_mismatch"}])
        raise _Failed("url_mismatch", f"asked for video {video_id}, yt-dlp returned metadata without a video ID")
    return info, _warning(result.stderr)


def _warning(stderr: str) -> str | None:
    """The first 200 characters of yt-dlp's warnings, from the first one on, as one line without addresses."""
    at = stderr.lower().find("warning:")
    return " ".join(_URL.sub("<url>", stderr[at:]).split())[:WARNING_HEAD] if at >= 0 else None


def _transcript(ytdlp: _YtDlp, video_id: str, info: dict, words: int, notes: list[str],
                warning: str | None) -> tuple[str | None, dict, _Failed | None]:
    """The transcript cut to `words`, and the item's `raw.transcript`.

    From the caption track in the video's own language; from speech recognition only when the
    video lists no caption track at all. When the policy gives no transcript the text is None, and
    the record's status and a note say why. When the caption or audio run fails the text is None,
    the status is the kind of failure, and the failure is returned for the envelope's `error`.

    A warning on the metadata run ends it there: yt-dlp can hide tracks after a failed challenge,
    so neither the caption list nor its emptiness can be trusted.

    Speech recognition never follows a failed run: after a rate limit, a login wall or a bot check
    it would be working around a block."""
    record = {"status": None, "source": None, "language": None, "track": None, "words": 0, "truncated": False}
    try:
        if warning is not None:
            raise policy.Unavailable("incomplete_metadata")
        track, source, record["language"] = policy.choose_track(info)
        if track is None:
            text, made = _recognise(ytdlp, video_id, info, record["language"])
        else:
            text = policy.clean_vtt(_caption_text(ytdlp, video_id, info, track, source, notes))
            made = {"source": f"youtube-{source}", "track": track}
        spoken = text.split()
        if not spoken:
            raise policy.Unavailable("transcript_empty")
    except policy.Unavailable as refusal:
        record["status"] = refusal.status
        notes.append(f"no transcript: {refusal.status}")
        return None, record, None
    except _Failed as failure:
        record["status"] = _RUN_STATUS.get(failure.code, "upstream_error")
        return None, record, failure
    cut = len(spoken) > words
    if cut:
        notes.append(f"transcript cut to {words} of {len(spoken)} words (--words)")
    truncated = cut or made.get("inference_truncated", False)
    record.update(made, status="partial" if truncated else "ok", words=min(len(spoken), words), truncated=truncated)
    return " ".join(spoken[:words]), record, None


def _caption_text(ytdlp: _YtDlp, video_id: str, info: dict, track: str, source: str, notes: list[str]) -> str:
    """Read the selected VTT from the same extraction that established its language.

    Keep yt-dlp's downloader for other URL shapes and for OS proxy configurations. A rate limit,
    a login or bot check, a timeout or an oversized file on the direct GET ends the read. Any
    other failure of it (a refused or expired address, a server error, a body that is not WebVTT,
    a dropped connection) falls back to one yt-dlp caption run, as before this shortcut existed,
    and a note says so.
    """
    rows = info["subtitles" if source == "uploaded" else "automatic_captions"][track]
    row = next((r for r in reversed(rows) if r.get("ext") == "vtt"), {})
    url = row.get("url")
    if isinstance(url, str) and not _BLANK.search(url):
        parts = urlsplit(url)
        if (parts.scheme == "https" and parts.netloc == "www.youtube.com" and parts.path == "/api/timedtext"
                and not ({"https", "all"} & getproxies().keys())):
            query = parse_qs(parts.query, keep_blank_values=True)
            if query.get("v") != [video_id] or "tlang" in query:
                raise _Failed("upstream_error", "caption address does not identify the selected original video track")
            headers = info.get("http_headers") or {}
            agent = headers.get("User-Agent") if isinstance(headers, dict) else None
            try:
                return _fetch_vtt(ytdlp, parts.path + "?" + parts.query, agent)
            except _DirectFailed as failure:
                notes.append(f"direct caption request failed ({failure.message}); used a yt-dlp caption run")
    return _caption_file(ytdlp, video_id, track, source)


def _fetch_vtt(ytdlp: _YtDlp, target: str, agent: str | None) -> str:
    timeout = ytdlp.left(CAPTION_TIMEOUT)
    if timeout <= 0:
        raise _Failed("timeout", "the read deadline expired before the caption request")
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPSConnection("www.youtube.com", timeout=timeout)
    ytdlp.runs += 1
    try:
        connection.request("GET", target, headers={"User-Agent": agent} if isinstance(agent, str) else {})
        sock = connection.sock
        response = connection.getresponse()
        if response.status == 429:
            raise _Failed("rate_limited", "YouTube caption request returned HTTP 429")
        if response.status != 200:
            raise _DirectFailed("upstream_error", f"YouTube caption request returned HTTP {response.status}")
        data = bytearray()
        while len(data) <= MAX_CAPTION:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Failed("timeout", "YouTube caption download timed out")
            sock.settimeout(remaining)
            chunk = response.read1(min(65536, MAX_CAPTION + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
    except TimeoutError:
        raise _Failed("timeout", "YouTube caption request timed out") from None
    except (OSError, http.client.HTTPException, ValueError):
        raise _DirectFailed("network_error", "YouTube caption request failed") from None
    finally:
        connection.close()
    if response.length:
        raise _DirectFailed("upstream_error", "YouTube caption download ended early")
    if len(data) > MAX_CAPTION:
        raise _Failed("upstream_error", "YouTube caption file exceeds the size limit")
    text = data.decode("utf-8-sig", errors="replace")
    if not re.match(r"^WEBVTT(?:[ \t\r\n]|$)", text):
        lower = text.lower()
        if any(phrase in lower for phrase in _RATE_LIMIT):
            raise _Failed("rate_limited", "YouTube returned a rate limit for the caption request")
        if any(phrase in lower for phrase in _BLOCK):
            raise _Failed("blocked", "YouTube returned a login or bot check for the caption request")
        raise _DirectFailed("upstream_error", "YouTube returned an invalid WebVTT caption file")
    return text


def _caption_file(ytdlp: _YtDlp, video_id: str, track: str, source: str) -> str:
    """One run that asks for exactly this track, written to a directory that is removed afterwards.

    The run is judged by its exit status and its file: yt-dlp warns on stderr on a good caption run."""
    with tempfile.TemporaryDirectory(prefix="rsrc-youtube-", dir=ytdlp.environ.get("TMPDIR")) as directory:
        ytdlp.run(["--no-playlist", "--write-subs" if source == "uploaded" else "--write-auto-subs",
                   "--sub-lang", track, "--sub-format", "vtt", "--skip-download",
                   "-o", os.path.join(directory, "%(id)s"), WATCH + video_id], CAPTION_TIMEOUT, one_video=True)
        captions = Path(directory, f"{video_id}.{track}.vtt")
        try:
            if captions.is_file() and captions.stat().st_size <= MAX_CAPTION:
                return captions.read_text(encoding="utf-8", errors="replace")
        except OSError:  # there, but not readable
            raise policy.Unavailable("caption_fetch_empty") from None
        raise policy.Unavailable("caption_fetch_empty")


def _recognise(ytdlp: _YtDlp, video_id: str, info: dict, language: str) -> tuple[str, dict]:
    """On-device speech recognition of the original audio: one yt-dlp run for the audio, then
    `vidscribe` with the model from the local cache. Returns the text and its record fields.

    Every limit is checked before the audio run. vidscribe makes no request, so it is not counted
    in `requests`, and its failure is a missing transcript (`local_asr_error`), not a failed read.

    Tested offline only, and it may be out of reach for an ordinary video: yt-dlp takes `language`
    from an audio track's id or an automatic caption track, so a video with one audio track and no
    captions probably has none and stops at `unknown_original_language` before it gets here
    (unverified: no live run has been made)."""
    policy.check_recognisable(info, language)
    if shutil.which("vidscribe", path=ytdlp.environ.get("PATH", "")) is None:
        raise policy.Unavailable("vidscribe_missing")
    with tempfile.TemporaryDirectory(prefix="rsrc-youtube-", dir=ytdlp.environ.get("TMPDIR")) as directory:
        audio = Path(directory, "audio.m4a")
        ytdlp.run(["--no-playlist", "-f", f"bestaudio[language={language}]", "--max-filesize", str(MAX_AUDIO),
                   "--fragment-retries", "0", "-o", str(audio), WATCH + video_id],
                  AUDIO_TIMEOUT, one_video=True, file_limit=MAX_AUDIO)
        if not audio.is_file() or not 0 < audio.stat().st_size <= MAX_AUDIO:
            raise policy.Unavailable("audio_size_limit")
        timeout = ytdlp.left(ASR_TIMEOUT)
        try:
            if timeout <= 0:
                raise proc.Timeout("no time left")
            # Offline, so the model comes from the local cache and is never downloaded. HF_HOME is
            # not passed on, so that cache must be at its default place.
            result = ytdlp.runner(
                ["vidscribe", str(audio), "-a", "-f", "txt", "-m", MODEL, "-o", os.path.join(directory, "transcript")],
                timeout, env={**ytdlp.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
        except (proc.Missing, proc.Timeout):
            raise policy.Unavailable("local_asr_error") from None
    if result.code != 0:
        raise policy.Unavailable("local_asr_error")
    return result.stdout, {"source": "local-qwen-asr", "model": MODEL, "media_duration": info["duration"],
                           "inference_truncated": "transcript cut off" in result.stderr.lower()}


def _video_item(video_id: str, info: dict, text: str | None, transcript: dict) -> dict:
    return env.item(
        id=video_id,
        url=WATCH + video_id,
        title=_text(info.get("title")),
        author=_text(info.get("channel")),
        published_at=_published(info),
        text=text,
        source=SOURCE,
        engagement=_counts(views=info.get("view_count"), likes=info.get("like_count"),
                           comments=info.get("comment_count")),
        raw={**{key: info.get(key) for key in RECORD}, "captions": policy.listed(info), "transcript": transcript},
    )


def _published(info: dict) -> str | None:
    """The upload time in UTC; the upload day when yt-dlp gives no time; else None."""
    return _utc(info.get("timestamp")) or _day(info.get("upload_date"))


def _utc(timestamp) -> str | None:
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        return None
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):  # a number no calendar holds
        return None


def _day(upload_date) -> str | None:
    if not isinstance(upload_date, str) or not re.fullmatch(r"\d{8}", upload_date):
        return None
    try:
        return datetime.strptime(upload_date, "%Y%m%d").date().isoformat()
    except ValueError:  # eight digits that are not a date
        return None
