"""`rsrc youtube` through rsrc.cli.main with a fake runner and the recorded yt-dlp output."""

import io
from unittest.mock import Mock
import json
from pathlib import Path

import pytest

from rsrc import cli, proc
from rsrc.channels import youtube
from rsrc.envelope import ITEM_KEYS

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = (FIXTURES / "youtube_search.jsonl").read_text(encoding="utf-8")  # ytsearch10:Claude Code, 10 lines
META_UPLOADED = (FIXTURES / "youtube_meta_uploaded.json").read_text(encoding="utf-8")  # C33hJGt-ySg: uploaded `en`
META_DUBBED = (FIXTURES / "youtube_meta_dubbed.json").read_text(encoding="utf-8")  # vlfCjWuv4es: 20 `-orig` tracks
UPLOADED_VTT = (FIXTURES / "youtube_uploaded.vtt").read_text(encoding="utf-8")  # 558 words
AUTOMATIC_VTT = (FIXTURES / "youtube_automatic.vtt").read_text(encoding="utf-8")  # 3618 words

FIXED = ["yt-dlp", "--ignore-config", "--no-cookies-from-browser", "--retries", "0", "--extractor-retries", "0"]
SEARCH_FLAGS = ["--flat-playlist", "--dump-json", "--extractor-args", "youtubetab:approximate_date"]
UPLOADED = "C33hJGt-ySg"
DUBBED = "vlfCjWuv4es"
WATCH = "https://www.youtube.com/watch?v="
FOUR = ("PATH", "HOME", "LANG", "TMPDIR")
RECORD_KEYS = ["id", "title", "description", "channel", "channel_id", "channel_url", "uploader_id",
               "channel_follower_count", "timestamp", "upload_date", "duration", "view_count", "like_count",
               "comment_count", "language", "live_status", "availability", "age_limit", "categories", "tags",
               "chapters"]
# Both recorded caption runs printed this on stderr and exited 0 with the file written.
CAPTION_WARNING = ("WARNING: The extractor specified to use impersonation for this download, but no impersonate "
                   "target is available. If you encounter errors, then see  "
                   "https://github.com/yt-dlp/yt-dlp#impersonation  for information on installing the required "
                   "dependencies\n")


def ok(stdout: str = "", stderr: str = "") -> proc.Result:
    return proc.Result(0, stdout, stderr)


def lines(*records) -> str:
    return "".join(json.dumps(record) + "\n" for record in records)


def result_line(video_id: str = "abcdefghijk", **fields) -> dict:
    return {"id": video_id, "title": "A title", "channel": "A channel", "description": "A snippet",
            "view_count": 5, "url": f"https://www.youtube.com/watch?v={video_id}", **fields}


def metadata(recorded: str = META_UPLOADED, **changes) -> str:
    """A recorded metadata run's output with some fields replaced."""
    return json.dumps({**json.loads(recorded), **changes})


def caption_run(vtt: str):
    """Answers a caption run as yt-dlp did when recorded: writes `<id>.<track>.vtt` where `-o`
    points, exits 0, and prints the impersonation warning on stderr."""
    def respond(argv):
        template = argv[argv.index("-o") + 1]
        track = argv[argv.index("--sub-lang") + 1]
        video_id = argv[-1].removeprefix(WATCH)
        Path(template.replace("%(id)s", video_id) + f".{track}.vtt").write_text(vtt, encoding="utf-8")
        return ok(stderr=CAPTION_WARNING)
    return respond


def read_uploaded():
    return FakeRunner(ok(META_UPLOADED), caption_run(UPLOADED_VTT))


def read_dubbed():
    return FakeRunner(ok(META_DUBBED), caption_run(AUTOMATIC_VTT))


class FakeRunner:
    """Answers runs in order from canned Results, exceptions, or functions of the argv; records
    argv, timeout and options."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[list[str], float, dict]] = []

    def __call__(self, argv, timeout, **options):
        self.calls.append((list(argv), timeout, options))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response(argv) if callable(response) else response

    @property
    def argvs(self):
        return [argv for argv, _, _ in self.calls]


@pytest.fixture
def environ(tmp_path):
    """An environment whose PATH holds the three programs (never started: the runner is fake) and
    one variable that must not reach a child."""
    programs = tmp_path / "bin"
    programs.mkdir()
    for name in ("yt-dlp", "deno", "vidscribe"):
        (programs / name).write_text("")
        (programs / name).chmod(0o755)
    return {"PATH": str(programs), "HOME": "/home/tester", "LANG": "en_US.UTF-8", "TMPDIR": str(tmp_path),
            "YOUTUBE_API_KEY": "sentinel-key"}


@pytest.fixture
def rsrc(monkeypatch, capsys, environ):
    monkeypatch.setattr(cli, "CHANNELS", [youtube])

    def run(argv, fake, environ=environ, transport=None):
        context = cli.Context(environ=environ, runner=fake, transport=transport or (lambda *args: (404, b"")))
        code = cli.main(["youtube", *argv], context)
        out = capsys.readouterr().out
        assert len(out.splitlines()) == 1
        return code, json.loads(out)

    return run


# search

def test_search_is_one_run_with_the_fixed_flags_and_a_four_variable_environment(rsrc, environ):
    fake = FakeRunner(ok(SEARCH))

    rsrc(["search", "Claude Code"], fake)

    [(argv, timeout, options)] = fake.calls
    assert argv == FIXED + SEARCH_FLAGS + ["ytsearch10:Claude Code"]
    assert timeout == 60
    assert options == {"env": {key: environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR")}}


def test_search_limit_is_the_number_of_results_asked_for(rsrc):
    fake = FakeRunner(ok(SEARCH))

    code, out = rsrc(["search", "-x --exec", "--limit", "3"], fake)

    assert fake.argvs[0][-1] == "ytsearch3:-x --exec"  # the query is one argument behind the prefix
    assert out["input"] == {"query": "-x --exec", "limit": 3}


def test_search_returns_one_item_per_line_in_yt_dlps_order(rsrc):
    code, out = rsrc(["search", "Claude Code"], FakeRunner(ok(SEARCH)))

    assert code == 0
    assert [item["id"] for item in out["items"]] == [
        "BGscm2FMIG4", "3TwbE7gv6qo", "QoQBzR1NIqI", "73DrNNvEKH0", "jdbOVepEtUE",
        "u2QqWkMv3Lg", "vlfCjWuv4es", "OrpR5p0ry3s", "6eBSHbLKuN0", "fl1DSmwQKKY"]
    assert all(tuple(item) == ITEM_KEYS for item in out["items"])


def test_search_item_maps_the_recorded_line(rsrc):
    code, out = rsrc(["search", "Claude Code"], FakeRunner(ok(SEARCH)))

    first = out["items"][0]
    recorded = json.loads(SEARCH.splitlines()[0])
    assert first == {
        "id": "BGscm2FMIG4",
        "url": "https://www.youtube.com/watch?v=BGscm2FMIG4",
        "title": "Claude Code: How To Build & Launch Your First Website",
        "author": "Zinho Automates",
        "published_at": "2026-11-16",
        "text": recorded["description"],
        "source": "youtube",
        "engagement": {"views": 9521},
        "raw": recorded,
    }
    assert recorded["description"].startswith("Claude Code: How To Build & Launch Your First Website. Host your")
    assert recorded["timestamp"] == 1794844800
    assert out["meta"]["notes"] == [
        "Search published_at dates are approximate, derived from the ages shown by YouTube."]
    assert all(len(item["published_at"]) == 10 for item in out["items"])


def test_search_with_only_an_approximate_timestamp_keeps_the_day(rsrc):
    record = json.loads(SEARCH.splitlines()[0])
    del record["upload_date"]
    _, out = rsrc(["search", "Claude Code"], FakeRunner(ok(lines(record))))
    assert out["items"][0]["published_at"] == "2026-11-16"
    assert "approximate" in out["meta"]["notes"][0]


@pytest.mark.parametrize("day", [None, "20260231", "2026", 20261116])
def test_search_with_no_usable_upload_date_leaves_it_null(rsrc, day):
    record = {**json.loads(SEARCH.splitlines()[0]), "upload_date": day, "timestamp": None}
    code, out = rsrc(["search", "Claude Code"], FakeRunner(ok(lines(record))))
    assert code == 0 and out["items"][0]["published_at"] is None
    assert out["meta"]["notes"] == []


def test_search_envelope_names_the_channel_and_counts_one_run(rsrc):
    code, out = rsrc(["search", "Claude Code"], FakeRunner(ok(SEARCH)))

    assert (out["ok"], out["channel"], out["action"], out["backend"], out["error"]) == (
        True, "youtube", "search", "yt-dlp", None)
    assert out["input"] == {"query": "Claude Code", "limit": 10}
    [attempt] = out["meta"]["attempts"]
    assert (attempt["backend"], attempt["outcome"], attempt["requests"]) == ("yt-dlp", "ok", 1)
    assert isinstance(attempt["ms"], int)


def test_search_url_is_built_from_the_id_also_for_a_short(rsrc):
    short = result_line("shortsID_01", url="https://www.youtube.com/shorts/shortsID_01")

    code, out = rsrc(["search", "q"], FakeRunner(ok(lines(short))))

    [item] = out["items"]
    assert item["url"] == "https://www.youtube.com/watch?v=shortsID_01"
    assert item["raw"]["url"] == "https://www.youtube.com/shorts/shortsID_01"


def test_search_keeps_a_repeated_video_once(rsrc):
    stdout = lines(result_line("aaaaaaaaaaa"), result_line("bbbbbbbbbbb"), result_line("aaaaaaaaaaa", title="again"))

    code, out = rsrc(["search", "q"], FakeRunner(ok(stdout)))

    assert [(item["id"], item["title"]) for item in out["items"]] == [("aaaaaaaaaaa", "A title"),
                                                                    ("bbbbbbbbbbb", "A title")]


@pytest.mark.parametrize("views", [None, "9521", 9521.5, True])
def test_search_engagement_is_null_unless_the_view_count_is_an_integer(rsrc, views):
    code, out = rsrc(["search", "q"], FakeRunner(ok(lines(result_line(view_count=views)))))

    assert out["items"][0]["engagement"] is None


@pytest.mark.parametrize("fields", [{"description": None}, {"description": ""}, {"description": 7},
                                    {"title": None, "channel": None}])
def test_search_fields_the_page_lacks_are_null(rsrc, fields):
    code, out = rsrc(["search", "q"], FakeRunner(ok(lines(result_line(**fields)))))

    [item] = out["items"]
    assert code == 0
    for key in fields:
        assert item[{"description": "text", "channel": "author"}.get(key, key)] is None


def test_search_with_no_lines_is_exit_66(rsrc):
    code, out = rsrc(["search", "zzzz no such video"], FakeRunner(ok("")))

    assert code == 66
    assert (out["ok"], out["items"], out["error"]) == (True, [], None)
    assert out["meta"]["attempts"][0]["outcome"] == "no_results"


@pytest.mark.parametrize("stdout", [
    "not json\n",
    "[1, 2]\n",
    lines({"title": "no id"}),
    lines(result_line("tooshort")),
    lines(result_line("twelve_chars")),
    lines(result_line("has space 1")),
    lines({"id": 12345678901}),
    lines(result_line()) + "WARNING: trailing text\n",
], ids=["not json", "not an object", "no id", "short id", "long id", "id with a space", "id is a number",
        "a good line then text"])
def test_search_line_of_the_wrong_shape_fails_the_command(rsrc, stdout):
    code, out = rsrc(["search", "q"], FakeRunner(ok(stdout)))

    assert code == 1
    assert (out["ok"], out["items"], out["error"]["code"]) == (False, [], "upstream_error")
    [attempt] = out["meta"]["attempts"]
    assert (attempt["outcome"], attempt["requests"]) == ("upstream_error", 1)


def test_search_run_that_exits_non_zero_is_an_error_not_an_empty_result(rsrc):
    code, out = rsrc(["search", "q"], FakeRunner(proc.Result(1, "", "ERROR: something broke\n")))

    assert code == 1
    assert (out["ok"], out["items"], out["error"]["code"]) == (False, [], "upstream_error")


# search: usage and setup

@pytest.mark.parametrize("argv", [["search"], ["search", ""], ["search", "  \t"],
                                  ["search", "q", "--limit", "0"], ["search", "q", "--limit", "21"],
                                  ["search", "q", "--limit", "-1"], ["search", "q", "--limit", "ten"],
                                  ["search", "q", "--time", "week"]])
def test_bad_search_arguments_are_usage_with_no_run(rsrc, argv):
    fake = FakeRunner()

    code, out = rsrc(argv, fake)

    assert code == 2
    assert (out["ok"], out["channel"], out["action"], out["error"]["code"]) == (False, "youtube", "search", "usage")
    assert fake.calls == []


@pytest.mark.parametrize("limit", ["1", "20"])
def test_search_limit_bounds_are_accepted(rsrc, limit):
    fake = FakeRunner(ok(SEARCH))

    code, out = rsrc(["search", "q", "--limit", limit], fake)

    assert code == 0 and fake.argvs[0][-1] == f"ytsearch{limit}:q"


def test_search_without_yt_dlp_on_path_is_config_with_no_run(rsrc, environ, tmp_path):
    (tmp_path / "bin" / "yt-dlp").unlink()
    fake = FakeRunner()

    code, out = rsrc(["search", "q"], fake)

    assert code == 2
    assert out["error"]["code"] == "config" and "yt-dlp" in out["error"]["message"]
    assert fake.calls == []


def test_search_looks_for_yt_dlp_only_on_the_path_it_was_given(rsrc):
    fake = FakeRunner()

    code, out = rsrc(["search", "q"], fake, environ={"HOME": "/home/tester"})  # no PATH: not this machine's

    assert (code, out["error"]["code"], fake.calls) == (2, "config", [])


def test_search_needs_no_deno(rsrc, tmp_path):
    (tmp_path / "bin" / "deno").unlink()

    code, out = rsrc(["search", "q"], FakeRunner(ok(SEARCH)))

    assert code == 0


def test_yt_dlp_vanishing_before_the_run_is_config(rsrc):
    code, out = rsrc(["search", "q"], FakeRunner(proc.Missing("yt-dlp not found")))

    assert (code, out["error"]["code"]) == (2, "config")


# search: failures (stderr hand-written from yt-dlp's messages)

RATE_LIMIT_429 = ('ERROR: [youtube:search] query "q": Unable to download API page: HTTP Error 429: Too Many Requests '
                  "(caused by <HTTPError 429: Too Many Requests>)\n")
RATE_LIMIT_SESSION = ("ERROR: [youtube] abcdefghijk: The current session has been rate-limited by YouTube for up to "
                      "an hour. It is recommended to use `-t sleep` to add a delay between video requests\n")
BOT_CHECK = ("ERROR: [youtube] abcdefghijk: Sign in to confirm you’re not a bot. Use --cookies-from-browser or "
             "--cookies for the authentication. See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ  for how to\n")
AGE_CHECK = ("ERROR: [youtube] abcdefghijk: Sign in to confirm your age. This video may be inappropriate for some "
             "users. Use --cookies-from-browser or --cookies for the authentication.\n")
FORBIDDEN = "ERROR: unable to download video data: HTTP Error 403: Forbidden\n"
UNAVAILABLE = "ERROR: [youtube] abcdefghijk: Video unavailable. This video is not available\n"


def failed(stderr: str, code: int = 1) -> proc.Result:
    return proc.Result(code, "", stderr)


# yt-dlp 2026.08.19 appends this sentence to YouTube's reason when the player answers with a captcha.
CAPTCHA = ("ERROR: [youtube] abcdefghijk: Video unavailable. YouTube is requiring a captcha challenge before "
           "playback\n")


@pytest.mark.parametrize("stderr, expected", [
    (RATE_LIMIT_429, "rate_limited"),
    ("ERROR: Unable to download API page: HTTP Error 429\n", "rate_limited"),
    (RATE_LIMIT_SESSION, "rate_limited"),
    ("ERROR: too many requests\n", "rate_limited"),
    (RATE_LIMIT_429 + BOT_CHECK, "rate_limited"),
    (BOT_CHECK, "blocked"),
    (FORBIDDEN, "blocked"),
    ("ERROR: Unable to download webpage: HTTP Error 401: Unauthorized\n", "blocked"),
    ("ERROR: [youtube] abcdefghijk: Sign in to confirm you're not a bot\n", "blocked"),
    ("ERROR: [youtube] abcdefghijk: Sign in to confirm this request\n", "blocked"),
    ("ERROR: [youtube] abcdefghijk: Show that you are not a bot\n", "blocked"),
    ("ERROR: Authentication required to view this page\n", "blocked"),
    (CAPTCHA, "blocked"),
    (AGE_CHECK, "upstream_error"),
    (UNAVAILABLE, "upstream_error"),  # `not_found` is for `read`, which names one video
    ("ERROR: [youtube] 429xxxxx403: something else broke\n", "upstream_error"),
    ("", "upstream_error"),
], ids=["HTTP 429", "HTTP 429 alone", "session rate-limited", "too many requests", "429 beside a bot check",
        "bot check", "HTTP 403", "HTTP 401", "bot check, plain quote", "sign in to confirm alone", "not a bot alone",
        "authentication required", "captcha", "age check", "video unavailable", "numbers in a video id",
        "silent failure"])
def test_search_failure_code_is_read_from_yt_dlps_stderr(rsrc, stderr, expected):
    fake = FakeRunner(failed(stderr))

    code, out = rsrc(["search", "q"], fake)

    assert code == 1
    assert (out["ok"], out["items"], out["error"]["code"]) == (False, [], expected)
    [attempt] = out["meta"]["attempts"]
    assert (attempt["outcome"], attempt["requests"]) == (expected, 1)
    assert len(fake.calls) == 1  # nothing is retried


def test_age_check_is_explained_and_not_called_a_block(rsrc):
    code, out = rsrc(["search", "q"], FakeRunner(failed(AGE_CHECK)))

    assert out["error"]["code"] == "upstream_error"
    assert "age-restricted" in out["error"]["message"] and "login" in out["error"]["message"]


def test_other_failure_carries_the_last_200_characters_of_stderr(rsrc):
    stderr = "start-marker " + "x" * 300 + " ERROR: the part that matters\n"

    code, out = rsrc(["search", "q"], FakeRunner(failed(stderr, code=2)))

    message = out["error"]["message"]
    assert out["error"]["code"] == "upstream_error"
    assert message.endswith(stderr.strip()[-200:]) and "start-marker" not in message
    assert "code 2" in message


def test_search_timeout(rsrc):
    code, out = rsrc(["search", "q"], FakeRunner(proc.Timeout("yt-dlp timed out after 60 s")))

    assert code == 1
    assert (out["error"]["code"], out["items"]) == ("timeout", [])
    [attempt] = out["meta"]["attempts"]
    assert (attempt["outcome"], attempt["requests"]) == ("timeout", 1)


def test_a_warning_on_a_search_that_exits_0_is_not_a_failure(rsrc):
    code, out = rsrc(["search", "q"], FakeRunner(ok(SEARCH, stderr="WARNING: [youtube] HTTP Error 403 on a thumbnail\n")))

    assert code == 0 and len(out["items"]) == 10


# read: details and the caption transcript

def test_read_is_a_metadata_run_then_a_run_for_the_chosen_track(rsrc, environ):
    fake = read_uploaded()

    code, out = rsrc(["read", UPLOADED], fake)

    four = {key: environ[key] for key in FOUR}
    [(meta_argv, meta_timeout, meta_options), (caption_argv, caption_timeout, caption_options)] = fake.calls
    assert meta_argv == FIXED + ["--no-playlist", "--dump-single-json", "--skip-download", WATCH + UPLOADED]
    assert (meta_timeout, meta_options) == (30, {"env": four, "stdout_limit": 32 * 1024 * 1024})
    target = caption_argv[-2]
    assert caption_argv == FIXED + ["--no-playlist", "--write-subs", "--sub-lang", "en", "--sub-format", "vtt",
                                    "--skip-download", "-o", target, WATCH + UPLOADED]
    assert Path(target).name == "%(id)s"
    assert (caption_timeout, caption_options) == (30, {"env": four})
    assert code == 0
    assert out["meta"]["attempts"] == [
        {"backend": "yt-dlp", "outcome": "ok", "requests": 2, "ms": out["meta"]["attempts"][0]["ms"]}]


def test_read_of_a_dubbed_video_asks_only_for_its_own_speech_recognition_track(rsrc):
    fake = read_dubbed()

    rsrc(["read", DUBBED], fake)

    caption_argv = fake.argvs[1]
    assert caption_argv[len(FIXED):-2] == ["--no-playlist", "--write-auto-subs", "--sub-lang", "en-orig",
                                           "--sub-format", "vtt", "--skip-download", "-o"]
    assert "--write-subs" not in caption_argv


@pytest.mark.parametrize("video, fake", [(UPLOADED, read_uploaded), (DUBBED, read_dubbed)])
def test_read_never_names_a_player_client_a_cookie_a_proxy_or_a_translated_track(rsrc, video, fake):
    fake = fake()

    rsrc(["read", video], fake)

    assert len(fake.argvs) == 2
    for argv in fake.argvs:
        target = argv[argv.index("-o") + 1] if "-o" in argv else None  # a random directory name
        joined = " ".join(arg for arg in argv[len(FIXED):] if arg != target)
        for word in ("player_client", "extractor-args", "cookies", "proxy", "username", "netrc", "tlang",
                     "--sub-langs"):
            assert word not in joined
        assert argv.count("--sub-lang") <= 1


def test_read_keeps_its_caption_file_in_a_directory_that_is_gone_afterwards(rsrc, environ):
    fake = read_uploaded()

    rsrc(["read", UPLOADED], fake)

    directory = Path(fake.argvs[1][-2]).parent
    assert directory.parent == Path(environ["TMPDIR"])  # where the child was told to keep temporary files
    assert not directory.exists()


@pytest.fixture
def caption_http(monkeypatch):
    calls = []

    def serve(payload, status=200):
        class Connection:
            def __init__(self, host, timeout):
                assert host == "www.youtube.com" and 0 < timeout <= 30
                self.sock = Mock()

            def request(self, method, target, headers):
                calls.append((method, target, headers))

            def getresponse(self):
                response = io.BytesIO(payload.encode())
                response.status = status
                response.length = None
                return response

            def close(self):
                pass

        monkeypatch.setattr(youtube, "getproxies", lambda: {})
        monkeypatch.setattr(youtube.http.client, "HTTPSConnection", Connection)
        return calls

    return serve


def with_caption_url(recording, tracks, code):
    info = json.loads(recording)
    for row in info[tracks][code]:
        row["url"] = f"https://www.youtube.com/api/timedtext?v={info['id']}&" + row["url"].lstrip("?")
    return json.dumps(info)


@pytest.mark.parametrize("video,recording,tracks,track,vtt", [
    (UPLOADED, META_UPLOADED, "subtitles", "en", UPLOADED_VTT),
    (DUBBED, META_DUBBED, "automatic_captions", "en-orig", AUTOMATIC_VTT),
])
def test_read_gets_only_the_chosen_vtt_from_the_first_extraction(
        rsrc, caption_http, video, recording, tracks, track, vtt):
    calls = caption_http(vtt)
    fake = FakeRunner(ok(with_caption_url(recording, tracks, track)))
    code, out = rsrc(["read", video], fake)
    assert code == 0 and len(fake.calls) == 1
    [(method, target, headers)] = calls
    assert method == "GET" and f"v={video}&" in target and "tlang=" not in target
    assert out["items"][0]["text"] == youtube.policy.clean_vtt(vtt)
    assert out["items"][0]["raw"]["transcript"]["track"] == track


@pytest.mark.parametrize("status,body,error", [
    (429, "", "rate_limited"),
    (200, "<html>Sign in to confirm you're not a bot</html>", "blocked"),
    (200, "x" * (youtube.MAX_CAPTION + 1), "upstream_error"),
])
def test_failed_direct_caption_get_returns_an_error_without_a_retry(rsrc, caption_http, status, body, error):
    calls = caption_http(body, status)
    fake = FakeRunner(ok(with_caption_url(META_UPLOADED, "subtitles", "en")))
    code, out = rsrc(["read", UPLOADED], fake)
    assert code == 1 and out["ok"] is False and out["error"]["code"] == error
    assert len(calls) == len(fake.calls) == 1 and out["items"][0]["text"] is None


@pytest.mark.parametrize("status,body", [
    (403, ""), (500, ""), (200, "<html>wrong response</html>"),
])
def test_a_refused_or_broken_direct_caption_get_falls_back_to_one_caption_run(rsrc, caption_http, status, body):
    calls = caption_http(body, status)
    fake = FakeRunner(ok(with_caption_url(META_UPLOADED, "subtitles", "en")), caption_run(UPLOADED_VTT))
    code, out = rsrc(["read", UPLOADED], fake)
    assert code == 0 and out["ok"] is True and len(calls) == 1 and len(fake.calls) == 2
    assert out["items"][0]["text"] == youtube.policy.clean_vtt(UPLOADED_VTT)
    [note] = [n for n in out["meta"]["notes"] if "direct caption request failed" in n]
    assert "used a yt-dlp caption run" in note


def test_read_envelope_names_the_channel_action_and_input(rsrc):
    code, out = rsrc(["read", UPLOADED], read_uploaded())

    assert (out["channel"], out["action"], out["backend"]) == ("youtube", "read", "yt-dlp")
    assert out["input"] == {"video": UPLOADED, "words": 5000}
    assert out["error"] is None and out["meta"]["truncated"] is None and out["meta"]["notes"] == []


def test_read_item_maps_the_recorded_video(rsrc):
    code, out = rsrc(["read", UPLOADED], read_uploaded())

    [item] = out["items"]
    assert tuple(item) == ITEM_KEYS
    assert {key: item[key] for key in ITEM_KEYS if key not in ("text", "raw")} == {
        "id": UPLOADED,
        "url": WATCH + UPLOADED,
        "title": "Fable 5.2 or Opus? When to use each model in Claude Code",
        "author": "Claude",
        "published_at": "2026-11-11T17:30:20Z",
        "source": "youtube",
        "engagement": {"views": 478412, "likes": 6633, "comments": 450},
    }


def test_read_text_is_the_uploaded_transcript(rsrc):
    code, out = rsrc(["read", UPLOADED], read_uploaded())

    [item] = out["items"]
    assert len(item["text"].split()) == 558
    assert "-->" not in item["text"] and "\n" not in item["text"] and "<" not in item["text"]
    assert item["raw"]["transcript"] == {"status": "ok", "source": "youtube-uploaded", "language": "en",
                                         "track": "en", "words": 558, "truncated": False}


def test_read_text_is_the_automatic_transcript_of_the_original_audio(rsrc):
    code, out = rsrc(["read", DUBBED], read_dubbed())

    [item] = out["items"]
    assert len(item["text"].split()) == 3618
    assert item["raw"]["transcript"] == {"status": "ok", "source": "youtube-automatic", "language": "en",
                                         "track": "en-orig", "words": 3618, "truncated": False}


def test_read_raw_is_the_named_record(rsrc):
    code, out = rsrc(["read", UPLOADED], read_uploaded())

    raw = out["items"][0]["raw"]
    recorded = json.loads(META_UPLOADED)
    assert list(raw) == RECORD_KEYS + ["captions", "transcript"]
    assert {key: raw[key] for key in RECORD_KEYS} == {key: recorded[key] for key in RECORD_KEYS}
    assert raw["duration"] == 208 and raw["chapters"][0] == {"start_time": 0, "title": "Intro", "end_time": 12}


def test_read_raw_lists_the_caption_tracks_without_the_translations(rsrc):
    code, uploaded = rsrc(["read", UPLOADED], read_uploaded())
    code, dubbed = rsrc(["read", DUBBED], read_dubbed())

    # 159 automatic tracks were listed for this video; all but these are machine translations
    assert uploaded["items"][0]["raw"]["captions"] == {
        "uploaded": ["en"], "automatic": ["en-US", "en-US-orig", "en-orig"], "audio_original": []}
    captions = dubbed["items"][0]["raw"]["captions"]
    assert captions["uploaded"] == [] and captions["audio_original"] == ["en-US"]
    assert len(captions["automatic"]) == 20 and all(code.endswith("-orig") for code in captions["automatic"])
    assert captions["automatic"] == sorted(captions["automatic"])


def test_read_record_has_null_for_a_field_yt_dlp_did_not_report(rsrc):
    info = json.loads(META_UPLOADED)
    del info["chapters"], info["channel_follower_count"], info["channel"]

    code, out = rsrc(["read", UPLOADED], FakeRunner(ok(json.dumps(info)), caption_run(UPLOADED_VTT)))

    [item] = out["items"]
    assert list(item["raw"]) == RECORD_KEYS + ["captions", "transcript"]
    assert item["raw"]["chapters"] is None and item["raw"]["channel_follower_count"] is None
    assert item["author"] is None


@pytest.mark.parametrize("fields, expected", [
    ({"timestamp": 1794418220, "upload_date": "20261111"}, "2026-11-11T17:30:20Z"),
    ({"timestamp": None, "upload_date": "20261111"}, "2026-11-11"),
    ({"timestamp": "1794418220", "upload_date": "20261111"}, "2026-11-11"),
    ({"timestamp": True, "upload_date": "20261111"}, "2026-11-11"),
    ({"timestamp": 10 ** 20, "upload_date": "20261111"}, "2026-11-11"),
    ({"timestamp": None, "upload_date": None}, None),
    ({"timestamp": None, "upload_date": "2026"}, None),
    ({"timestamp": None, "upload_date": "20261341"}, None),
    ({"timestamp": None, "upload_date": 20261111}, None),
])
def test_read_published_at_is_the_timestamp_else_the_upload_date(rsrc, fields, expected):
    code, out = rsrc(["read", UPLOADED], FakeRunner(ok(metadata(**fields)), caption_run(UPLOADED_VTT)))

    assert out["items"][0]["published_at"] == expected


@pytest.mark.parametrize("fields, expected", [
    ({"like_count": None, "comment_count": "450"}, {"views": 478412}),
    ({"view_count": None, "like_count": 0}, {"likes": 0, "comments": 450}),
    ({"view_count": None, "like_count": None, "comment_count": None}, None),
])
def test_read_engagement_holds_only_the_integer_counts(rsrc, fields, expected):
    code, out = rsrc(["read", UPLOADED], FakeRunner(ok(metadata(**fields)), caption_run(UPLOADED_VTT)))

    assert out["items"][0]["engagement"] == expected


def test_read_words_cuts_the_transcript_and_says_so(rsrc):
    code, whole = rsrc(["read", UPLOADED], read_uploaded())
    code, out = rsrc(["read", UPLOADED, "--words", "100"], read_uploaded())

    [item] = out["items"]
    assert code == 0
    assert item["text"].split() == whole["items"][0]["text"].split()[:100]
    assert item["raw"]["transcript"] == {"status": "partial", "source": "youtube-uploaded", "language": "en",
                                         "track": "en", "words": 100, "truncated": True}
    assert out["input"] == {"video": UPLOADED, "words": 100}
    assert out["meta"]["truncated"] is None  # that field counts items, and the one item is here
    assert out["meta"]["notes"] == ["transcript cut to 100 of 558 words (--words)"]


def test_read_words_equal_to_the_transcript_length_cuts_nothing(rsrc):
    code, out = rsrc(["read", UPLOADED, "--words", "558"], read_uploaded())

    transcript = out["items"][0]["raw"]["transcript"]
    assert (transcript["status"], transcript["words"], transcript["truncated"]) == ("ok", 558, False)
    assert out["meta"]["notes"] == []


def test_read_output_holds_no_caption_or_stream_address(rsrc, caption_http):
    caption_http(UPLOADED_VTT)
    info = json.loads(META_UPLOADED)
    address = "https://www.youtube.com/api/timedtext?v=C33hJGt-ySg&ip=203.0.113.9&signature=SIGNATURE&lang=en"
    info["subtitles"]["en"] = [{"ext": "vtt", "url": address, "name": "English"}]
    info["formats"].append({"format_id": "251", "language": "en", "language_preference": -1,
                            "url": "https://rr1---sn-abc.googlevideo.com/videoplayback?ip=203.0.113.9&sig=SIGNATURE"})
    info["url"] = "https://rr1---sn-abc.googlevideo.com/videoplayback?ip=203.0.113.9"

    code, out = rsrc(["read", UPLOADED], FakeRunner(ok(json.dumps(info))))

    printed = json.dumps(out)
    assert code == 0 and out["items"][0]["raw"]["transcript"]["status"] == "ok"
    for part in ("203.0.113.9", "SIGNATURE", "googlevideo", "timedtext"):
        assert part not in printed
    assert not {"subtitles", "automatic_captions", "formats", "url"} & set(out["items"][0]["raw"])


# read: speech recognition for a video with no caption track

MODEL = "mlx-community/Qwen3-ASR-1.7B-8bit"
MAX_AUDIO = 50 * 1024 * 1024
SPOKEN = "All right, so here we are in front of the elephants."  # hand-written: recognition has not run live
VIDSCRIBE_STDERR = "transcript: /tmp/out/transcript/audio.txt\n"


def no_captions(**changes) -> str:
    """The recorded English video (208 s) as if it listed no caption track at all."""
    return metadata(subtitles={}, automatic_captions={}, **changes)


def audio_run(size: int | None = 4096):
    """Answers the audio run: leaves a file of `size` bytes where `-o` points (None: no file), exits 0."""
    def respond(argv):
        if size is not None:
            with open(argv[argv.index("-o") + 1], "wb") as audio:
                audio.truncate(size)
        return ok()
    return respond


def recognition(vidscribe=None, info: str | None = None, size: int | None = 4096):
    return FakeRunner(ok(info or no_captions()), audio_run(size),
                      ok(SPOKEN + "\n", VIDSCRIBE_STDERR) if vidscribe is None else vidscribe)


def refused(out, status: str, language: str | None = "en") -> bool:
    """The item is there without text, and the record and a note give the reason."""
    [item] = out["items"]
    assert item["title"] == "Fable 5.2 or Opus? When to use each model in Claude Code"
    assert item["text"] is None and out["error"] is None
    assert item["raw"]["transcript"] == {"status": status, "source": None, "language": language, "track": None,
                                         "words": 0, "truncated": False}
    assert out["meta"]["notes"] == [f"no transcript: {status}"]
    return True


def test_a_video_with_no_caption_track_gets_an_audio_run_then_vidscribe(rsrc, environ):
    fake = recognition()

    code, out = rsrc(["read", UPLOADED], fake)

    four = {key: environ[key] for key in FOUR}
    [_, (audio_argv, audio_timeout, audio_options), (asr_argv, asr_timeout, asr_options)] = fake.calls
    audio = audio_argv[-2]
    assert audio_argv == FIXED + ["--no-playlist", "-f", "bestaudio[language=en]", "--max-filesize", str(MAX_AUDIO),
                                  "--fragment-retries", "0", "-o", audio, WATCH + UPLOADED]
    assert (audio_timeout, audio_options) == (30, {"env": four, "file_limit": MAX_AUDIO})
    directory = Path(audio).parent
    assert Path(audio).name == "audio.m4a" and directory.parent == Path(environ["TMPDIR"])
    assert asr_argv == ["vidscribe", audio, "-a", "-f", "txt", "-m", MODEL, "-o", str(directory / "transcript")]
    assert asr_timeout == 60
    assert asr_options == {"env": {**four, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}}  # no download
    assert not directory.exists()
    assert code == 0


def test_a_recognised_transcript_is_the_text_and_says_how_it_was_made(rsrc):
    code, out = rsrc(["read", UPLOADED], recognition())

    [item] = out["items"]
    assert code == 0 and item["text"] == SPOKEN
    assert item["raw"]["transcript"] == {
        "status": "ok", "source": "local-qwen-asr", "language": "en", "track": None, "words": 11,
        "truncated": False, "model": MODEL, "media_duration": 208, "inference_truncated": False}
    assert out["meta"]["attempts"][0]["requests"] == 2  # yt-dlp runs; vidscribe makes no request
    assert out["meta"]["notes"] == [] and out["error"] is None


def test_a_recognition_that_was_cut_off_is_partial(rsrc):
    cut_off = ok(SPOKEN + "\n", "warning: transcript cut off in 0:30-1:00 (likely a repetition loop)\n" + VIDSCRIBE_STDERR)

    code, out = rsrc(["read", UPLOADED], recognition(cut_off))

    [item] = out["items"]
    transcript = item["raw"]["transcript"]
    assert code == 0 and item["text"] == SPOKEN
    assert (transcript["status"], transcript["truncated"], transcript["inference_truncated"]) == ("partial", True, True)


def test_a_recognised_transcript_is_cut_to_words_too(rsrc):
    code, out = rsrc(["read", UPLOADED, "--words", "3"], recognition())

    [item] = out["items"]
    transcript = item["raw"]["transcript"]
    assert item["text"] == "All right, so"
    assert (transcript["status"], transcript["words"], transcript["truncated"]) == ("partial", 3, True)
    assert transcript["inference_truncated"] is False


@pytest.mark.parametrize("fields", [
    {"language": "en-US"},  # a region of a supported language
    {"duration": 600},
    {"duration": 599.5},
    {"live_status": "was_live"},
    {"live_status": None},
])
def test_recognition_runs_within_the_limits(rsrc, fields):
    fake = recognition(info=no_captions(**fields))

    code, out = rsrc(["read", UPLOADED], fake)

    assert len(fake.calls) == 3 and out["items"][0]["raw"]["transcript"]["status"] == "ok"


@pytest.mark.parametrize("fields, status", [
    ({"language": "eo"}, "unsupported_asr_language"),  # not one of the model's 30
    ({"duration": None}, "media_duration_limit"),
    ({"duration": 601}, "media_duration_limit"),
    ({"duration": 0}, "media_duration_limit"),
    ({"duration": "208"}, "media_duration_limit"),
    ({"duration": True}, "media_duration_limit"),
    ({"duration": float("inf")}, "media_duration_limit"),
    ({"duration": 10 ** 400}, "media_duration_limit"),  # an integer no float holds
    ({"live_status": "is_live"}, "media_duration_limit"),
    ({"live_status": "is_upcoming"}, "media_duration_limit"),
    ({"live_status": "post_live"}, "media_duration_limit"),
])
def test_recognition_limits_refuse_before_the_audio_run(rsrc, fields, status):
    fake = FakeRunner(ok(no_captions(**fields)))

    code, out = rsrc(["read", UPLOADED], fake)

    [item] = out["items"]
    assert code == 0 and len(fake.calls) == 1
    assert item["text"] is None and item["raw"]["transcript"]["status"] == status
    assert "model" not in item["raw"]["transcript"]
    assert out["meta"]["notes"] == [f"no transcript: {status}"]
    assert out["meta"]["attempts"][0] == {"backend": "yt-dlp", "outcome": "ok", "requests": 1,
                                          "ms": out["meta"]["attempts"][0]["ms"]}


def test_without_vidscribe_on_path_there_is_no_audio_run(rsrc, environ):
    Path(environ["PATH"], "vidscribe").unlink()
    fake = FakeRunner(ok(no_captions()))

    code, out = rsrc(["read", UPLOADED], fake)

    assert code == 0 and len(fake.calls) == 1 and refused(out, "vidscribe_missing")


@pytest.mark.parametrize("size", [None, 0, MAX_AUDIO + 1])
def test_an_audio_file_that_is_missing_empty_or_too_large_is_not_recognised(rsrc, size):
    fake = FakeRunner(ok(no_captions()), audio_run(size))

    code, out = rsrc(["read", UPLOADED], fake)

    assert code == 0 and len(fake.calls) == 2 and refused(out, "audio_size_limit")
    assert out["meta"]["attempts"][0]["requests"] == 2


def test_an_audio_file_of_exactly_the_limit_is_recognised(rsrc):
    fake = recognition(size=MAX_AUDIO)

    code, out = rsrc(["read", UPLOADED], fake)

    assert len(fake.calls) == 3 and out["items"][0]["text"] == SPOKEN


@pytest.mark.parametrize("vidscribe", [
    proc.Result(1, "", "Traceback (most recent call last): model not found in the local cache\n"),
    proc.Result(1, SPOKEN, ""),
    proc.Timeout("vidscribe timed out after 60 s"),
    proc.Missing("vidscribe not found"),
])
def test_vidscribe_failing_is_a_missing_transcript_not_a_failed_read(rsrc, vidscribe):
    fake = recognition(vidscribe)

    code, out = rsrc(["read", UPLOADED], fake)

    assert code == 0 and len(fake.calls) == 3 and refused(out, "local_asr_error")
    assert out["meta"]["attempts"][0]["outcome"] == "ok"


@pytest.mark.parametrize("stdout", ["", " \n\n"])
def test_vidscribe_printing_nothing_is_an_empty_transcript(rsrc, stdout):
    code, out = rsrc(["read", UPLOADED], recognition(ok(stdout, VIDSCRIBE_STDERR)))

    assert code == 0 and refused(out, "transcript_empty")


@pytest.mark.parametrize("video, fake", [(UPLOADED, read_uploaded), (DUBBED, read_dubbed)])
def test_a_video_with_captions_is_never_recognised(rsrc, video, fake):
    fake = fake()

    rsrc(["read", video], fake)

    assert len(fake.argvs) == 2
    assert all(argv[0] == "yt-dlp" and "-f" not in argv for argv in fake.argvs)


def test_a_video_whose_only_captions_are_in_another_language_is_never_recognised(rsrc):
    french = {"fr": [{"ext": "vtt", "url": "?lang=fr", "name": "French"}]}
    fake = FakeRunner(ok(metadata(subtitles=french, automatic_captions={})))

    code, out = rsrc(["read", UPLOADED], fake)

    # the track choice stops before it names a language
    assert code == 0 and len(fake.calls) == 1 and refused(out, "original_caption_unavailable", language=None)


# read: usage and setup

@pytest.mark.parametrize("video", [
    UPLOADED,
    f" {UPLOADED} ",
    f"https://www.youtube.com/watch?v={UPLOADED}",
    f"https://youtube.com/watch?v={UPLOADED}",
    f"https://m.youtube.com/watch?v={UPLOADED}",
    f"https://www.youtube.com/watch?v={UPLOADED}&t=42s&list=PL0123456789",
    f"https://www.youtube.com/watch?feature=share&v={UPLOADED}#comments",
    f"HTTPS://WWW.YOUTUBE.COM/watch?v={UPLOADED}",
    f"https://www.youtube.com/shorts/{UPLOADED}",
    f"https://www.youtube.com/live/{UPLOADED}?si=abc",
    f"https://m.youtube.com/embed/{UPLOADED}",
    f"https://youtu.be/{UPLOADED}",
    f"https://youtu.be/{UPLOADED}?t=10",
])
def test_read_accepts_an_id_or_a_video_url_and_reads_the_same_video(rsrc, video):
    fake = read_uploaded()

    code, out = rsrc(["read", video], fake)

    assert code == 0
    assert fake.argvs[0][-1] == WATCH + UPLOADED and fake.argvs[1][-1] == WATCH + UPLOADED
    assert out["input"] == {"video": video, "words": 5000}  # as typed
    assert out["items"][0]["id"] == UPLOADED and out["items"][0]["url"] == WATCH + UPLOADED


@pytest.mark.parametrize("video", [
    "",
    "C33hJGt",
    UPLOADED + "x",
    "C33hJGt ySg",
    "C33hJGt=ySg",
    f"ytsearch5:{UPLOADED}",
    f"www.youtube.com/watch?v={UPLOADED}",
    f"http://www.youtube.com/watch?v={UPLOADED}",
    f"https://music.youtube.com/watch?v={UPLOADED}",
    f"https://www.youtube.com.example.org/watch?v={UPLOADED}",
    f"https://example.org/watch?v={UPLOADED}",
    f"https://user@www.youtube.com/watch?v={UPLOADED}",
    f"https://www.youtube.com:8443/watch?v={UPLOADED}",
    f"https://www.youtube.com/watch?v={UPLOADED}&v={DUBBED}",
    f"https://www.youtube.com/watch?v={UPLOADED}x",
    f"https://www.youtube.com/watch?v={UPLOADED}%0A",
    "https://www.youtube.com/watch?v=",
    "https://www.youtube.com/watch",
    f"https://www.youtube.com/watch/{UPLOADED}",
    "https://www.youtube.com/playlist?list=PL0123456789",
    "https://www.youtube.com/@claude",
    "https://www.youtube.com/channel/UCV03SRZXJEz-hchIAogeJOg",
    f"https://www.youtube.com/shorts/{UPLOADED}/more",
    "https://www.youtube.com/shorts/",
    f"https://www.youtube.com/clip/{UPLOADED}",
    "https://youtu.be/",
    f"https://youtu.be/{UPLOADED}/more",
    f"https://youtu.be/watch?v={UPLOADED}",
    f"https://youtu.be/{UPLOADED}\thttps://example.org",
    "https://vimeo.com/76979871",
    "https://[",
    "https://youtu.be/C33hJ\tGt-ySg",
    "https://www.youtube.com/watch?v=C33hJGt\n-ySg",
    "https://www.youtube.com/embed/videoseries?list=PL0123456789abcdef",  # a playlist's player, 11 characters
    "https://www.youtube.com/embed/live_stream?channel=UCV03SRZXJEz-hchIAogeJOg",  # a channel's live player
])
def test_read_of_anything_else_is_usage_with_no_run(rsrc, video):
    fake = FakeRunner()

    code, out = rsrc(["read", "--", video], fake)

    assert (code, out["error"]["code"], fake.calls) == (2, "usage", [])
    assert out["items"] == []


@pytest.mark.parametrize("words", ["0", "-1", "40001"])
def test_read_words_outside_1_to_40000_is_usage_with_no_run(rsrc, words):
    fake = FakeRunner()

    code, out = rsrc(["read", UPLOADED, "--words", words], fake)

    assert (code, out["error"]["code"], fake.calls) == (2, "usage", [])
    assert "--words" in out["error"]["message"]


@pytest.mark.parametrize("words, returned", [("1", 1), ("40000", 558)])
def test_read_words_at_either_end_of_the_range_is_accepted(rsrc, words, returned):
    code, out = rsrc(["read", UPLOADED, "--words", words], read_uploaded())

    assert code == 0 and out["items"][0]["raw"]["transcript"]["words"] == returned


@pytest.mark.parametrize("program", ["yt-dlp", "deno"])
def test_read_without_a_needed_program_is_config_naming_it_with_no_run(rsrc, environ, program):
    Path(environ["PATH"], program).unlink()
    fake = FakeRunner()

    code, out = rsrc(["read", UPLOADED], fake)

    assert (code, out["error"]["code"], fake.calls) == (2, "config", [])
    assert program in out["error"]["message"]
    assert out["input"] == {"video": UPLOADED, "words": 5000} and out["items"] == []
    assert out["meta"]["attempts"][0]["requests"] == 0


def test_read_looks_for_its_programs_only_on_the_path_it_was_given(rsrc):
    fake = FakeRunner()

    code, out = rsrc(["read", UPLOADED], fake, environ={"HOME": "/home/tester"})  # no PATH: not this machine's

    assert (code, out["error"]["code"], fake.calls) == (2, "config", [])


def test_a_wrong_argument_is_reported_before_a_missing_program(rsrc, environ):
    Path(environ["PATH"], "deno").unlink()

    code, out = rsrc(["read", "https://vimeo.com/76979871"], FakeRunner())

    assert (code, out["error"]["code"]) == (2, "usage")


# read: the policy gives no transcript

ROW = {"ext": "vtt", "url": "?lang=xx", "name": "A track"}
SIGNATURE_WARNING = ("WARNING: [youtube] C33hJGt-ySg: Signature extraction failed: Some formats may be missing\n"
                     "WARNING: [youtube] C33hJGt-ySg: Some web client https formats have been skipped\n")
MAX_CAPTION = 4 * 1024 * 1024


@pytest.mark.parametrize("changes, status", [
    ({"language": None, "automatic_captions": {}}, "unknown_original_language"),
    ({"language": "English", "automatic_captions": {}}, "unknown_original_language"),
    ({"automatic_captions": {"en-orig": [ROW], "fr-orig": [ROW]}}, "ambiguous_original_language"),  # none marked
    ({"subtitles": {"fr": [ROW]}, "automatic_captions": {}}, "original_caption_unavailable"),
    ({"subtitles": {}, "automatic_captions": {"en": [{**ROW, "url": "?lang=fr&tlang=en"}]}},
     "original_caption_unavailable"),  # only a machine translation into the video's language
    ({"subtitles": None}, "invalid_caption_metadata"),
    ({"automatic_captions": {"en-orig": "a track"}}, "invalid_caption_metadata"),
])
def test_a_track_choice_that_gives_no_track_returns_the_details_and_makes_no_other_run(rsrc, changes, status):
    fake = FakeRunner(ok(metadata(**changes)))

    code, out = rsrc(["read", UPLOADED], fake)

    assert code == 0 and len(fake.calls) == 1 and refused(out, status, language=None)
    assert out["meta"]["attempts"][0]["outcome"] == "ok" and out["meta"]["attempts"][0]["requests"] == 1


def test_a_warning_on_the_metadata_run_means_no_caption_request(rsrc):
    fake = FakeRunner(ok(META_UPLOADED, stderr=SIGNATURE_WARNING))

    code, out = rsrc(["read", UPLOADED], fake)

    [item] = out["items"]
    assert code == 0 and len(fake.calls) == 1 and out["error"] is None
    assert item["title"] == "Fable 5.2 or Opus? When to use each model in Claude Code" and item["text"] is None
    assert item["raw"]["transcript"] == {"status": "incomplete_metadata", "source": None, "language": None,
                                         "track": None, "words": 0, "truncated": False}
    assert out["meta"]["notes"] == [
        "no transcript: incomplete_metadata",
        "yt-dlp warned on the metadata run: WARNING: [youtube] C33hJGt-ySg: Signature extraction failed: Some "
        "formats may be missing WARNING: [youtube] C33hJGt-ySg: Some web client https formats have been skipped"]


def test_the_note_holds_the_first_200_characters_of_the_warning(rsrc):
    stderr = "[youtube] Extracting URL\nwarning: " + "x" * 400 + "\n"

    code, out = rsrc(["read", UPLOADED], FakeRunner(ok(META_UPLOADED, stderr=stderr)))

    assert out["meta"]["notes"][1] == "yt-dlp warned on the metadata run: warning: " + "x" * 191


def test_a_warning_on_the_metadata_run_means_no_speech_recognition_either(rsrc):
    fake = FakeRunner(ok(no_captions(), stderr=SIGNATURE_WARNING))  # the empty caption list cannot be trusted

    code, out = rsrc(["read", UPLOADED], fake)

    assert code == 0 and len(fake.calls) == 1
    assert out["items"][0]["raw"]["transcript"]["status"] == "incomplete_metadata"


def test_a_caption_run_that_leaves_no_file_is_caption_fetch_empty(rsrc):
    fake = FakeRunner(ok(META_UPLOADED), ok(stderr=CAPTION_WARNING))

    code, out = rsrc(["read", UPLOADED], fake)

    assert code == 0 and len(fake.calls) == 2 and refused(out, "caption_fetch_empty")
    assert out["meta"]["attempts"][0]["requests"] == 2


def test_a_caption_file_over_4_mib_is_caption_fetch_empty(rsrc):
    fake = FakeRunner(ok(META_UPLOADED), caption_run(UPLOADED_VTT + " " * MAX_CAPTION))

    code, out = rsrc(["read", UPLOADED], fake)

    assert code == 0 and len(fake.calls) == 2 and refused(out, "caption_fetch_empty")


def test_a_caption_file_of_exactly_4_mib_is_read(rsrc):
    vtt = UPLOADED_VTT + " " * (MAX_CAPTION - len(UPLOADED_VTT.encode()))

    code, out = rsrc(["read", UPLOADED], FakeRunner(ok(META_UPLOADED), caption_run(vtt)))

    assert out["items"][0]["raw"]["transcript"]["status"] == "ok" and len(out["items"][0]["text"].split()) == 558


def test_a_caption_file_that_cannot_be_read_is_caption_fetch_empty(rsrc):
    def unreadable(argv):  # needs a user whose permissions apply, as everywhere this suite runs
        response = caption_run(UPLOADED_VTT)(argv)
        Path(argv[argv.index("-o") + 1].replace("%(id)s", UPLOADED) + ".en.vtt").chmod(0)
        return response

    code, out = rsrc(["read", UPLOADED], FakeRunner(ok(META_UPLOADED), unreadable))

    assert code == 0 and refused(out, "caption_fetch_empty")


@pytest.mark.parametrize("vtt", ["", "WEBVTT\nKind: captions\nLanguage: en\n", "WEBVTT\n\n00:00.000 --> 00:02.000\n<c> </c>\n"])
def test_a_caption_file_with_no_spoken_text_is_transcript_empty(rsrc, vtt):
    fake = FakeRunner(ok(META_UPLOADED), caption_run(vtt))

    code, out = rsrc(["read", UPLOADED], fake)

    assert code == 0 and len(fake.calls) == 2 and refused(out, "transcript_empty")


# read: a run fails

PRIVATE = ("ERROR: [youtube] C33hJGt-ySg: Private video. Sign in if you've been granted access to this video. Use "
           "--cookies-from-browser or --cookies for the authentication.\n")
REMOVED = "ERROR: [youtube] C33hJGt-ySg: This video has been removed by the uploader\n"
NO_SUBTITLES = "ERROR: Unable to download video subtitles for 'en': HTTP Error 404: Not Found\n"
STREAM = "https://rr3---sn-abc.googlevideo.com/videoplayback?expire=1794440000&ip=203.0.113.9&sig=SIGNATURE"
NO_RECORD = {"status": None, "source": None, "language": "en", "track": None, "words": 0, "truncated": False}


@pytest.mark.parametrize("response, code", [
    (failed(RATE_LIMIT_429), "rate_limited"),
    (failed(RATE_LIMIT_SESSION), "rate_limited"),
    (failed(BOT_CHECK), "blocked"),
    (failed(FORBIDDEN), "blocked"),
    (failed(CAPTCHA), "blocked"),  # not `not_found`, though it starts "Video unavailable"
    (failed(AGE_CHECK), "upstream_error"),
    (failed(UNAVAILABLE), "not_found"),
    (failed(PRIVATE), "not_found"),
    (failed(REMOVED), "not_found"),
    (proc.Timeout("yt-dlp timed out after 30 s"), "timeout"),
    (failed("ERROR: [youtube] C33hJGt-ySg: Unable to extract initial data\n", code=1), "upstream_error"),
    (failed("", code=-9), "upstream_error"),
    (ok("<html>not metadata</html>"), "upstream_error"),
    (ok(""), "upstream_error"),
    (ok("[1, 2]"), "upstream_error"),
    (ok("[" * 100_000), "upstream_error"),
    (proc.Overflow("more than 33554432 bytes on stdout"), "upstream_error"),
])
def test_a_failed_metadata_run_returns_no_item_and_makes_no_other_run(rsrc, response, code):
    fake = FakeRunner(response)

    exit_code, out = rsrc(["read", UPLOADED], fake)

    assert (exit_code, out["error"]["code"], out["items"], len(fake.calls)) == (1, code, [], 1)
    assert out["meta"]["attempts"] == [
        {"backend": "yt-dlp", "outcome": code, "requests": 1, "ms": out["meta"]["attempts"][0]["ms"]}]
    assert out["input"] == {"video": UPLOADED, "words": 5000}


def test_an_age_restricted_video_is_explained_not_called_a_block(rsrc):
    code, out = rsrc(["read", UPLOADED], FakeRunner(failed(AGE_CHECK)))

    assert "age-restricted" in out["error"]["message"] and "login" in out["error"]["message"]


def test_metadata_past_the_32_mib_limit_says_so(rsrc):
    code, out = rsrc(["read", UPLOADED], FakeRunner(proc.Overflow("more than 33554432 bytes on stdout")))

    assert "32 MiB" in out["error"]["message"]


def test_metadata_for_another_video_is_url_mismatch(rsrc):
    fake = FakeRunner(ok(metadata(META_DUBBED)))

    code, out = rsrc(["read", UPLOADED], fake)

    assert (code, out["error"]["code"], out["items"], len(fake.calls)) == (1, "url_mismatch", [], 1)
    assert UPLOADED in out["error"]["message"] and DUBBED in out["error"]["message"]
    assert out["meta"]["rejected"] == [{"url": WATCH + DUBBED, "reason": "url_mismatch"}]
    assert out["meta"]["attempts"][0]["outcome"] == "url_mismatch"


@pytest.mark.parametrize("returned", [None, 12345678901, "", "https://example.org/x", ["C33hJGt-ySg"]])
def test_metadata_without_a_video_id_is_url_mismatch_and_rejects_no_address(rsrc, returned):
    code, out = rsrc(["read", UPLOADED], FakeRunner(ok(metadata(id=returned))))

    assert (code, out["error"]["code"], out["items"]) == (1, "url_mismatch", [])
    assert out["meta"]["rejected"] == []


@pytest.mark.parametrize("response, code, status, exit_code", [
    (failed(RATE_LIMIT_429), "rate_limited", "source_blocked", 1),
    (failed(BOT_CHECK), "blocked", "source_blocked", 1),
    (failed(FORBIDDEN), "blocked", "source_blocked", 1),
    (proc.Timeout("yt-dlp timed out after 30 s"), "timeout", "timeout", 1),
    (failed(NO_SUBTITLES), "upstream_error", "upstream_error", 1),
    (failed(AGE_CHECK), "upstream_error", "upstream_error", 1),
    (failed(UNAVAILABLE), "not_found", "upstream_error", 1),
    (proc.Missing("yt-dlp not found"), "config", "upstream_error", 2),
])
def test_a_failed_caption_run_keeps_the_item_and_sets_the_error(rsrc, response, code, status, exit_code):
    fake = FakeRunner(ok(META_UPLOADED), response)

    exited, out = rsrc(["read", UPLOADED], fake)

    [item] = out["items"]
    assert (exited, out["error"]["code"], len(fake.calls)) == (exit_code, code, 2)
    assert out["ok"] is False
    assert item["title"] == "Fable 5.2 or Opus? When to use each model in Claude Code" and item["text"] is None
    assert item["engagement"] == {"views": 478412, "likes": 6633, "comments": 450}
    assert item["raw"]["transcript"] == {**NO_RECORD, "status": status}
    assert out["meta"]["attempts"] == [
        {"backend": "yt-dlp", "outcome": code, "requests": 2, "ms": out["meta"]["attempts"][0]["ms"]}]


@pytest.mark.parametrize("response, code, status", [
    (failed(RATE_LIMIT_SESSION), "rate_limited", "source_blocked"),
    (failed(BOT_CHECK), "blocked", "source_blocked"),
    (proc.Timeout("yt-dlp timed out after 30 s"), "timeout", "timeout"),
    (failed("ERROR: unable to write data: [Errno 27] File too large\n"), "upstream_error", "upstream_error"),
    (failed(UNAVAILABLE), "not_found", "upstream_error"),
])
def test_a_failed_audio_run_keeps_the_item_and_is_never_followed_by_recognition(rsrc, response, code, status):
    fake = FakeRunner(ok(no_captions()), response)

    exited, out = rsrc(["read", UPLOADED], fake)

    [item] = out["items"]
    assert (exited, out["error"]["code"], len(fake.calls)) == (1, code, 2)
    assert item["text"] is None and item["raw"]["transcript"] == {**NO_RECORD, "status": status}
    assert all(argv[0] == "yt-dlp" for argv in fake.argvs)


# read: no address in a message

def test_an_error_message_never_holds_an_address_from_stderr(rsrc):
    stderr = f"ERROR: unable to download video data from {STREAM} : HTTP Error 500: Internal Server Error\n"

    code, out = rsrc(["read", UPLOADED], FakeRunner(failed(stderr)))

    message = out["error"]["message"]
    assert out["error"]["code"] == "upstream_error" and "HTTP Error 500: Internal Server Error" in message
    for part in ("googlevideo", "203.0.113.9", "SIGNATURE", "videoplayback"):
        assert part not in message


def test_an_address_longer_than_the_kept_tail_is_removed_whole(rsrc):
    stderr = f"ERROR: giving up on {STREAM}&pad={'x' * 400}&ip=203.0.113.9&sig=SIGNATURE\n"

    code, out = rsrc(["read", UPLOADED], FakeRunner(ok(META_UPLOADED), failed(stderr)))

    message = out["error"]["message"]
    assert "giving up on" in message
    for part in ("203.0.113.9", "SIGNATURE", "xxxx"):
        assert part not in message


def test_a_warning_note_never_holds_an_address(rsrc):
    stderr = f"WARNING: [youtube] C33hJGt-ySg: nsig extraction failed for {STREAM} ; some formats may be missing\n"

    code, out = rsrc(["read", UPLOADED], FakeRunner(ok(META_UPLOADED, stderr=stderr)))

    note = out["meta"]["notes"][1]
    assert "nsig extraction failed" in note and "some formats may be missing" in note
    for part in ("googlevideo", "203.0.113.9", "SIGNATURE"):
        assert part not in json.dumps(out)


def test_a_search_error_message_never_holds_an_address_either(rsrc):
    code, out = rsrc(["search", "q"], FakeRunner(failed(f"ERROR: unable to open {STREAM}\n")))

    assert "unable to open" in out["error"]["message"] and "203.0.113.9" not in out["error"]["message"]


# urllib3's wording when a download stalls: the host and the path are printed apart, without a scheme.
SCHEMELESS = ("HTTPSConnectionPool(host='rr3---sn-abc.googlevideo.com', port=443): Read timed out. "
              "(url: /videoplayback?expire=1794440000&ip=203.0.113.9&sig=SIGNATURE)")


def test_a_stream_host_or_caller_address_without_a_scheme_is_removed_too(rsrc):
    stderr = f"ERROR: unable to download video data: {SCHEMELESS}\n"

    code, out = rsrc(["read", UPLOADED], FakeRunner(ok(META_UPLOADED), failed(stderr)))

    message = out["error"]["message"]
    assert "unable to download video data" in message and "Read timed out" in message
    for part in ("googlevideo", "203.0.113.9", "SIGNATURE"):
        assert part not in message


def test_a_warning_note_drops_a_stream_host_without_a_scheme_too(rsrc):
    stderr = f"WARNING: [youtube] C33hJGt-ySg: format check failed: {SCHEMELESS}\n"

    code, out = rsrc(["read", UPLOADED], FakeRunner(ok(META_UPLOADED, stderr=stderr)))

    assert "format check failed" in out["meta"]["notes"][1]
    for part in ("googlevideo", "203.0.113.9", "SIGNATURE"):
        assert part not in json.dumps(out)


# read: 100 s for the whole command

class Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("time.monotonic", clock)
    return clock


def taking(clock: Clock, seconds: float, response):
    """A run that takes `seconds` on the clock, then answers with `response`."""
    def respond(argv):
        clock.now += seconds
        if isinstance(response, Exception):
            raise response
        return response(argv) if callable(response) else response
    return respond


def test_each_run_gets_its_own_limit_while_the_100_s_last(rsrc, clock):
    fake = FakeRunner(taking(clock, 29, ok(no_captions())), taking(clock, 29, audio_run()),
                      ok(SPOKEN, VIDSCRIBE_STDERR))

    code, out = rsrc(["read", UPLOADED], fake)

    assert [timeout for _, timeout, _ in fake.calls] == [30, 30, 42]  # 100 - 29 - 29 is less than vidscribe's 60
    assert code == 0 and out["items"][0]["text"] == SPOKEN


def test_a_run_gets_only_what_is_left_of_the_100_s(rsrc, clock):
    fake = FakeRunner(taking(clock, 85, ok(META_UPLOADED)), caption_run(UPLOADED_VTT))

    code, out = rsrc(["read", UPLOADED], fake)

    assert [timeout for _, timeout, _ in fake.calls] == [30, 15]
    assert code == 0


@pytest.mark.parametrize("seconds", [100, 250])
def test_with_nothing_left_the_caption_run_is_not_started(rsrc, clock, seconds):
    fake = FakeRunner(taking(clock, seconds, ok(META_UPLOADED)))

    code, out = rsrc(["read", UPLOADED], fake)

    [item] = out["items"]
    assert (code, out["error"]["code"], len(fake.calls)) == (1, "timeout", 1)
    assert "100 s" in out["error"]["message"]
    assert item["text"] is None and item["raw"]["transcript"] == {**NO_RECORD, "status": "timeout"}
    assert out["meta"]["attempts"][0]["requests"] == 1


def test_with_nothing_left_the_audio_run_is_not_started(rsrc, clock):
    fake = FakeRunner(taking(clock, 100, ok(no_captions())))

    code, out = rsrc(["read", UPLOADED], fake)

    assert (code, out["error"]["code"], len(fake.calls)) == (1, "timeout", 1)


def test_with_nothing_left_vidscribe_is_not_started(rsrc, clock):
    fake = FakeRunner(taking(clock, 30, ok(no_captions())), taking(clock, 70, audio_run()))

    code, out = rsrc(["read", UPLOADED], fake)

    assert code == 0 and len(fake.calls) == 2 and refused(out, "local_asr_error")


def test_search_has_no_100_s_ceiling(rsrc, clock):
    clock.now += 1000
    fake = FakeRunner(ok(SEARCH))

    code, out = rsrc(["search", "q"], fake)

    assert code == 0 and fake.calls[0][1] == 60


# read: metadata of an odd shape
BAD_ROW = {"ext": "vtt", "url": "https://[bad", "name": "A track"}  # an address `urlsplit` refuses


def test_a_caption_address_that_cannot_be_parsed_is_never_chosen(rsrc):
    fake = FakeRunner(ok(metadata(subtitles={"en": [BAD_ROW]}, automatic_captions={})))

    code, out = rsrc(["read", UPLOADED], fake)

    assert code == 0 and len(fake.calls) == 1 and refused(out, "original_caption_unavailable", language=None)


def test_an_unparseable_address_on_another_track_does_not_lose_the_read(rsrc):
    automatic = {**json.loads(META_UPLOADED)["automatic_captions"], "zz": [BAD_ROW]}
    fake = FakeRunner(ok(metadata(automatic_captions=automatic)), caption_run(UPLOADED_VTT))

    code, out = rsrc(["read", UPLOADED], fake)

    [item] = out["items"]
    assert code == 0 and item["raw"]["transcript"]["status"] == "ok"
    assert "zz" not in item["raw"]["captions"]["automatic"]
    assert out["meta"]["attempts"][0]["requests"] == 2


# read: the temporary directory after a failure

@pytest.mark.parametrize("fake", [
    lambda: FakeRunner(ok(META_UPLOADED), failed(NO_SUBTITLES)),
    lambda: FakeRunner(ok(no_captions()), failed(BOT_CHECK)),
    lambda: recognition(proc.Result(1, "", "Traceback (most recent call last): out of memory\n")),
], ids=["caption run fails", "audio run fails", "vidscribe fails"])
def test_the_temporary_directory_is_gone_after_a_failure_too(rsrc, environ, fake):
    fake = fake()

    rsrc(["read", UPLOADED], fake)

    directory = Path(fake.argvs[1][-2]).parent
    assert directory.parent == Path(environ["TMPDIR"])
    assert not directory.exists()


@pytest.mark.parametrize("second", ["found", "missing", "wrong_video", "rate_limit", "forbidden"])
def test_search_recovers_only_confirmed_channel_names(rsrc, second):
    first, last = json.loads((FIXTURES / "youtube_search_lockup.json").read_text())
    responses = [(200, first["oembed"]), {
        "found": (200, last["oembed"]), "missing": (404, {}), "wrong_video": (200, first["oembed"]),
        "rate_limit": (429, {}), "forbidden": (403, {}),
    }[second]]

    def transport(method, url, headers, body):
        from urllib.parse import parse_qs, urlsplit
        row = first if len(responses) == 2 else last
        assert method == "GET" and urlsplit(url).netloc == "www.youtube.com"
        assert parse_qs(urlsplit(url).query)["url"] == [WATCH + row["search"]["id"]]
        status, data = responses.pop(0)
        return status, json.dumps(data).encode()

    code, out = rsrc(["search", "Claude Code"], FakeRunner(ok(lines(first["search"], last["search"]))),
                     transport=transport)
    assert [i["author"] for i in out["items"]] == ["Eric Tech", "Zinho Automates" if second == "found" else None]
    assert out["meta"]["attempts"][-1]["requests"] == 2
    assert any("unavailable" in n for n in out["meta"]["notes"]) == (second != "found")
    assert code == (1 if second in ("rate_limit", "forbidden") else 0)


def test_unknown_media_codec_keeps_captions_but_never_masks_a_second_warning(rsrc):
    codec_warning = "WARNING: Unknown codec iamf.001.001.Opus\n"
    fake = FakeRunner(ok(META_UPLOADED, stderr=codec_warning), caption_run(UPLOADED_VTT))
    code, out = rsrc(["read", UPLOADED], fake)
    assert code == 0 and out["items"][0]["raw"]["transcript"]["words"] == 558

    fake = FakeRunner(ok(META_UPLOADED, stderr=codec_warning + SIGNATURE_WARNING))
    code, out = rsrc(["read", UPLOADED], fake)
    assert code == 0 and out["items"][0]["raw"]["transcript"]["status"] == "incomplete_metadata"
    assert out["items"][0]["text"] is None and len(fake.calls) == 1
