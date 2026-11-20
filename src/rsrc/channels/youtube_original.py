"""The YouTube transcript policy: which caption track is the video's own language, a caption file
as text, and which videos may be given to speech recognition. Pure functions;
`rsrc.channels.youtube` makes the runs.

yt-dlp's metadata is untrusted input. A language that cannot be told is never read as "no
captions", and a translated track is never chosen.
"""

import html
import re
from urllib.parse import parse_qs, urlsplit

ORIGINAL = 10  # yt-dlp's language_preference for the audio YouTube names as the original
# The languages Qwen3-ASR recognises, as primary subtags.
ASR_LANGUAGES = frozenset("zh en yue ar de fr es pt id it ko ru th vi ja tr hi ms nl sv da fi pl cs fil fa el ro hu mk"
                          .split())
MAX_MEDIA_SECONDS = 600
LIVE = ("is_live", "is_upcoming", "post_live")  # yt-dlp's live_status while a stream has no final recording

_TAG = re.compile(r"<[^>]*>")
_ONE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")  # one language tag, not a '+'-joined selection
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")


class Unavailable(Exception):
    """The policy gives no transcript; `status` is the reason."""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


def marked_original(info: dict) -> set[str]:
    """The audio languages yt-dlp marks as the original (more than one only if the metadata is odd)."""
    formats = info.get("formats")
    return {f["language"] for f in (formats if isinstance(formats, list) else [])
            if isinstance(f, dict) and f.get("language_preference") == ORIGINAL and isinstance(f.get("language"), str)}


def _original(info: dict, originals: set[str]) -> str:
    """The one `-orig` caption language that is the video's own audio.

    yt-dlp labels the speech-recognition track of every dub `-orig`, so a dubbed video has many;
    the audio language it marks as original decides. With none marked, `-orig` tracks that share
    one primary subtag (en, en-US) are one language listed per region, not dubs, and the video's
    stated language picks among them. Audio codes can be regional (en-US) where caption codes are
    bare (en), so a primary-subtag match counts when it is the only one. It cannot tell regional
    variants apart: an en-US original with a separate en-GB dub and one en-orig track is accepted."""
    marked = marked_original(info)
    if marked:
        declared = next(iter(marked)) if len(marked) == 1 else None
    elif len({o.split("-")[0] for o in originals}) == 1:
        declared = info.get("language") or (next(iter(originals)) if len(originals) == 1 else None)
    else:
        declared = None
    if isinstance(declared, str) and _ONE_TAG.fullmatch(declared):
        primary = declared.split("-")[0]
        matches = originals & {declared} or {o for o in originals if o.split("-")[0] == primary}
        if len(matches) == 1:
            return matches.pop()
    raise Unavailable("ambiguous_original_language")


def _translated(row: dict) -> bool:
    """Whether a caption row is a translation: YouTube's machine translation, not what was said.
    An address that cannot be read counts as one, so its track is never chosen."""
    url = row.get("url")
    if not isinstance(url, str):
        return False
    try:
        return "tlang" in parse_qs(urlsplit(url).query)
    except ValueError:
        return True


def choose_track(info: dict) -> tuple[str | None, str | None, str]:
    """(track code, "uploaded" or "automatic", language) for the caption track in the video's own
    language; (None, None, language) when the video has no caption track at all.

    Raises Unavailable when the maps are malformed, the language cannot be told, or the only
    tracks are translations or in another language."""
    maps = (info.get("subtitles"), info.get("automatic_captions"))
    if not all(isinstance(m, dict) for m in maps) or any(
            not isinstance(code, str) or not isinstance(rows, list) for m in maps for code, rows in m.items()):
        raise Unavailable("invalid_caption_metadata")
    uploaded, automatic = maps
    originals = {code.removesuffix("-orig") for code, rows in automatic.items() if rows and code.endswith("-orig")}
    language = _original(info, originals) if originals else info.get("language")
    if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
        raise Unavailable("unknown_original_language")
    for source, tracks, codes in (("uploaded", uploaded, (language,)),
                                  ("automatic", automatic, (language + "-orig", language))):
        for code in codes:
            rows = tracks.get(code, [])
            if rows and all(isinstance(row, dict) for row in rows) and not any(_translated(row) for row in rows):
                return code, source, language
    if any(rows for m in maps for rows in m.values()):
        raise Unavailable("original_caption_unavailable")
    return None, None, language


def listed(info: dict) -> dict:
    """What a video's metadata lists, as codes only (never an address): its uploaded caption
    tracks, its automatic tracks that are not translations, and the audio marked original."""
    def codes(tracks, keep=lambda rows: True) -> list[str]:
        return sorted(code for code, rows in (tracks.items() if isinstance(tracks, dict) else ())
                      if isinstance(rows, list) and rows and keep(rows))

    return {
        "uploaded": codes(info.get("subtitles")),
        "automatic": codes(info.get("automatic_captions"),
                           lambda rows: not any(isinstance(row, dict) and _translated(row) for row in rows)),
        "audio_original": sorted(marked_original(info)),
    }


def check_recognisable(info: dict, language: str) -> None:
    """Raises Unavailable unless speech recognition may be tried on this video: a language the
    model knows, and a finished recording of at most ten minutes."""
    if language.split("-")[0] not in ASR_LANGUAGES:
        raise Unavailable("unsupported_asr_language")
    duration = info.get("duration")
    # The range test also refuses NaN and infinity, and is safe on an integer no float holds.
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not 0 < duration <= MAX_MEDIA_SECONDS or info.get("live_status") in LIVE):
        raise Unavailable("media_duration_limit")


def clean_vtt(text: str) -> str:
    """A WebVTT caption file as its spoken text on one line.

    Only cue payload is kept: the lines after a timing line, up to the next empty line. That drops
    the header, note and style blocks, cue numbers, and the timings with their settings. Tags are
    removed, entities decoded and whitespace collapsed. A line identical to the line before it is
    dropped, because automatic captions repeat each line as it scrolls; a line said again later
    in the video is kept."""
    lines: list[str] = []
    in_cue = False
    for raw in text.splitlines():
        if "-->" in raw:
            in_cue = True
        elif not raw:
            in_cue = False
        elif in_cue:
            line = " ".join(html.unescape(_TAG.sub("", raw)).split())
            if line and (not lines or lines[-1] != line):
                lines.append(line)
    return " ".join(lines)
