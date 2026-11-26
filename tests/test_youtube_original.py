"""The YouTube transcript policy (`rsrc.channels.youtube_original`), called directly: caption text
and track choice."""

import json
import re
from pathlib import Path

import pytest

from rsrc.channels.youtube_original import Unavailable, caption_cues, choose_track, timed_transcript

FIXTURES = Path(__file__).parent / "fixtures"
UPLOADED_VTT = (FIXTURES / "youtube_uploaded.vtt").read_text(encoding="utf-8")  # C33hJGt-ySg, track en
AUTOMATIC_VTT = (FIXTURES / "youtube_automatic.vtt").read_text(encoding="utf-8")  # vlfCjWuv4es, track en-orig
META_UPLOADED = json.loads((FIXTURES / "youtube_meta_uploaded.json").read_text(encoding="utf-8"))
META_DUBBED = json.loads((FIXTURES / "youtube_meta_dubbed.json").read_text(encoding="utf-8"))

_MARKUP = re.compile(r"WEBVTT|Kind:|Language:|-->|\d\d:\d\d:\d\d\.\d\d\d|align:|position:|[<>]")


# caption text

STAMP = re.compile(r"^\[(?:\d+:)?\d\d:\d\d\] ", re.M)


def transcript(caption: str, words: int = 10**6) -> str:
    return timed_transcript(caption_cues(caption), words)[0]


def spoken(text: str) -> str:
    """The words of a timed transcript, without its line times."""
    return " ".join(STAMP.sub("", text).split())


def test_uploaded_captions_become_the_spoken_text_only():
    text = transcript(UPLOADED_VTT)

    assert text.startswith("[00:00] Claude Fable 5.2 is out. It's our most capable generally available model for technical fields.")
    assert spoken(text).endswith("follow the steps in the guide linked below.")
    assert len(spoken(text).split()) == 558
    assert not _MARKUP.search(STAMP.sub("", text))


def test_automatic_captions_keep_each_scrolling_line_once():
    text = transcript(AUTOMATIC_VTT)

    assert spoken(text).startswith("I had some technical difficulties during my talk this morning, so I ran back home and "
                                   "I'm just going to record it from my computer instead. So, hi everyone. I'm Lydia.")
    assert spoken(text).endswith("best practices in the docs as well. Thank you so much.")
    assert len(spoken(text).split()) == 3618
    assert not _MARKUP.search(STAMP.sub("", text))


def test_each_line_starts_with_its_first_caption_time_and_spans_about_20_seconds():
    lines = transcript(AUTOMATIC_VTT).splitlines()
    times = [STAMP.match(line)[0] for line in lines]

    assert len(times) == len(lines) > 1
    assert lines[0].startswith("[00:00] I had some technical difficulties")
    seconds = [int(t[1:3]) * 60 + int(t[4:6]) for t in times]
    assert seconds == sorted(seconds) and all(20 <= b - a <= 40 for a, b in zip(seconds, seconds[1:]))


def test_times_from_an_hour_on_carry_the_hour_and_srt_is_read_too():
    srt = ("1\n00:59:58,500 --> 01:00:01,000\nlast words\n\n"
           "2\n01:00:30,000 --> 01:00:31,000\nof the hour\n")

    assert transcript(srt) == "[59:58] last words\n[1:00:30] of the hour"


def test_words_cut_the_transcript_and_the_total_counts_every_word():
    text, total = timed_transcript(caption_cues(UPLOADED_VTT), 6)

    assert (text, total) == ("[00:00] Claude Fable 5.2 is out. It's", 558)


def test_a_line_said_again_later_is_kept_and_cue_numbers_are_dropped():
    vtt = ("WEBVTT\n\n"
           "1\n00:00:01.000 --> 00:00:02.000\nYes.\n\n"
           "2\n00:00:02.000 --> 00:00:03.000\nNo.\n\n"
           "3\n00:00:03.000 --> 00:00:04.000\nYes.\n")

    assert caption_cues(vtt) == [(1.0, "Yes."), (2.0, "No."), (3.0, "Yes.")]


def test_a_line_repeated_by_the_next_cue_is_kept_once_with_its_first_time():
    vtt = ("WEBVTT\n\n"
           "00:00:01.000 --> 00:00:02.000\nfirst line\n\n"
           "00:00:02.000 --> 00:00:03.000\nfirst line\nsecond line\n\n"
           "00:00:03.000 --> 00:00:04.000\nsecond line\n \n")

    assert caption_cues(vtt) == [(1.0, "first line"), (2.0, "second line")]


def test_notes_styles_tags_and_cue_settings_are_removed_speakers_kept_and_entities_decoded():
    vtt = ("WEBVTT - a title\nKind: captions\nLanguage: en\n\n"
           "NOTE\na comment\nover two lines\n\n"
           "STYLE\n::cue { color: red }\n\n"
           "intro\n00:01.000 --> 00:02.000 align:start position:0%\n"
           "<v Ann>Tom &amp; Jerry</v>   said&nbsp;<i>hi</i>\n\n"
           "NOTE a comment on one line\n\n"
           "00:00:02.000 --> 00:00:03.000\n2 &lt; 3<00:00:02.500><c> always</c>\n")

    assert transcript(vtt) == "[00:01] Ann: Tom & Jerry said hi 2 < 3 always"


def test_windows_line_endings_are_read_the_same():
    vtt = "WEBVTT\r\n\r\n00:00:01.000 --> 00:00:02.000\r\none\r\n\r\n00:00:02.000 --> 00:00:03.000\r\ntwo\r\n"

    assert transcript(vtt) == "[00:01] one two"


def test_a_file_without_cues_is_empty_text():
    assert timed_transcript(caption_cues("WEBVTT\nKind: captions\nLanguage: en\n"), 10) == ("", 0)
    assert timed_transcript(caption_cues(""), 10) == ("", 0)


def test_recognised_speech_keeps_repeated_lines_comparisons_and_indented_timings():
    srt = ("1\n 00:00:01,000 --> 00:00:02,000\nYes.\n\n"
           "2\n00:00:02,000 --> 00:00:03,000\nYes.\n\n"
           "3\n00:00:03,000 --> 00:00:04,000\nx < y and z > w\n")

    assert caption_cues(srt, scrolling=False) == [(1.0, "Yes."), (2.0, "Yes."), (3.0, "x < y and z > w")]


def test_adjacent_speakers_stay_apart():
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Ann>Hello</v><v.loud Bob>Hi</v>\n"

    assert caption_cues(vtt) == [(1.0, "Ann: Hello Bob: Hi")]


# track choice

def metadata(language="es", automatic=None, uploaded=None, formats=None) -> dict:
    return {"id": "abcdefghijk", "language": language, "duration": 10, "formats": formats or [],
            "automatic_captions": automatic or {}, "subtitles": uploaded or {}}


def caption(lang: str, tlang: str | None = None) -> dict:
    """A caption row as the fixtures keep it: the address cut to its parameters."""
    return {"ext": "vtt", "url": f"?lang={lang}" + (f"&tlang={tlang}" if tlang else "") + "&kind=asr"}


def audio(language, preference) -> dict:
    return {"format_id": f"140-{language}", "language": language, "language_preference": preference}


def dubbed(tracks: dict) -> dict:
    """The shape yt-dlp gives a dubbed video: every dub has its own speech-recognition track, each
    labelled `-orig`; the plain codes are translations of the first. `tracks` maps an audio
    language to yt-dlp's language_preference: 10 original, 5 default, -1 dub."""
    automatic = {"ar-orig": [caption("ar")], "en-orig": [caption("en")], "es-orig": [caption("es")],
                 "ar": [caption("ar")], "en": [caption("ar", "en")], "es": [caption("ar", "es")]}
    video = {"format_id": "137", "language": None, "language_preference": -1}
    return metadata("en-US", automatic, formats=[video] + [audio(*track) for track in tracks.items()])


def refusal(info) -> str:
    with pytest.raises(Unavailable) as raised:
        choose_track(info)
    return raised.value.status


def test_recorded_video_with_uploaded_captions_resolves_to_its_uploaded_track():
    # Lists en-orig and en-US-orig with no audio marked original: one language per region, not dubs.
    assert choose_track(META_UPLOADED) == ("en", "uploaded", "en")


def test_recorded_dubbed_video_resolves_to_the_original_automatic_track():
    assert choose_track(META_DUBBED) == ("en-orig", "automatic", "en")


def test_uploaded_original_track_wins_over_automatic_and_translated_ones():
    info = metadata(automatic={"es-orig": [{"ext": "vtt"}], "en": [{"ext": "vtt"}]},
                    uploaded={"es": [{"ext": "vtt"}], "en": [{"ext": "vtt"}]})

    assert choose_track(info) == ("es", "uploaded", "es")


def test_automatic_original_is_found_without_a_stated_language():
    info = metadata(None, {"ja-orig": [{"ext": "vtt"}], "en": [{"ext": "vtt"}]})

    assert choose_track(info) == ("ja-orig", "automatic", "ja")


def test_empty_caption_maps_are_genuine_absence():
    assert choose_track(metadata()) == (None, None, "es")


def test_only_translated_tracks_are_not_genuine_absence():
    assert refusal(metadata("es", {"en": [{"ext": "vtt"}]})) == "original_caption_unavailable"


@pytest.mark.parametrize("url", ["https://www.youtube.com/api/timedtext?v=abcdefghijk&tlang=es", "?lang=en&tlang=es"])
def test_a_track_whose_address_carries_tlang_is_never_chosen(url):
    assert refusal(metadata("es", {"es": [{"url": url}]})) == "original_caption_unavailable"


def test_a_track_with_one_translated_row_among_others_is_not_chosen():
    info = metadata("en", {"en": [caption("en"), caption("en-US", "en")]})

    assert refusal(info) == "original_caption_unavailable"


@pytest.mark.parametrize("info", [
    {"subtitles": []},
    {"subtitles": {}, "automatic_captions": None},
    metadata(uploaded={"es": "rows"}),
    metadata(automatic={1: []}),
], ids=["no automatic map", "automatic map is null", "rows are not a list", "track code is not text"])
def test_malformed_caption_maps_are_invalid_caption_metadata(info):
    assert refusal(info) == "invalid_caption_metadata"


@pytest.mark.parametrize("language", [None, "", "english", "EN", 7, ["en"]])
def test_no_usable_language_is_unknown_original_language(language):
    assert refusal(metadata(language, {"en": [caption("en")]})) == "unknown_original_language"


def test_dubbed_video_resolves_the_audio_language_marked_original():
    assert choose_track(dubbed({"ar": -1, "en-US": 10, "es-US": -1})) == ("en-orig", "automatic", "en")


def test_single_original_track_matches_a_regional_language_code():
    info = metadata("pt-BR", {"pt-orig": [caption("pt")], "en": [caption("pt", "en")]})

    assert choose_track(info) == ("pt-orig", "automatic", "pt")


def test_exact_track_code_wins_over_a_sibling_with_the_same_primary_subtag():
    info = metadata("pt-PT", {"pt-PT-orig": [caption("pt-PT")], "pt-orig": [caption("pt")]},
                    formats=[audio("pt-PT", 10)])

    assert choose_track(info) == ("pt-PT-orig", "automatic", "pt-PT")


def test_marked_original_outranks_the_stated_language():
    info = metadata("es", {"en-orig": [caption("en")]}, formats=[audio("en-US", 10)])

    assert choose_track(info) == ("en-orig", "automatic", "en")


@pytest.mark.parametrize("language, track", [("en", "en-orig"), ("en-US", "en-US-orig")])
def test_unmarked_original_tracks_in_one_language_are_picked_by_the_stated_language(language, track):
    info = metadata(language, {"en-orig": [caption("en")], "en-US-orig": [caption("en-US")]})

    assert choose_track(info) == (track, "automatic", language)


@pytest.mark.parametrize("language", [None, "en-GB", "fr"])
def test_unmarked_original_tracks_in_one_language_need_a_language_that_names_one(language):
    info = metadata(language, {"en-orig": [caption("en")], "en-US-orig": [caption("en-US")]})

    assert refusal(info) == "ambiguous_original_language"


@pytest.mark.parametrize("info", [
    metadata(None, {"es-orig": [{}], "ja-orig": [{}]}),
    metadata("en", {"en-orig": [caption("en")], "es-orig": [caption("es")]}),
    metadata("en", {"es-orig": [{}]}),
    dubbed({"ar": -1, "en-US": 5, "es-US": -1}),
    dubbed({"ar": -1, "en-US": 10, "es-US": 10}),
    dubbed({"ar": -1, "fr-FR": 10, "es-US": -1}),
    metadata("pt-BR", {"pt-PT-orig": [caption("pt-PT")], "pt-orig": [caption("pt")]}, formats=[audio("pt-BR", 10)]),
    metadata("en", {"en-orig": [caption("en")]}, formats=[audio("es-US", 10)]),
    metadata("en-US+es-US", {"en-orig": [caption("en")]}),  # yt-dlp joins a merged selection's languages with '+'
], ids=["two languages, none stated", "two languages, unmarked, one stated", "stated language has no track",
        "dub with no audio marked original", "two audio languages marked original",
        "marked original has no caption track", "marked original matches two tracks",
        "marked original disagrees with the only track", "language is not one tag"])
def test_unresolvable_original_language_is_ambiguous(info):
    assert refusal(info) == "ambiguous_original_language"


@pytest.mark.parametrize("formats", [None, "en-US", {"a": audio("en-US", 10)}, [None, 1, "x"],
                                     [{"language": ["en"], "language_preference": 10}], [audio("en-US", True)]])
def test_malformed_formats_mark_no_language_original(formats):
    info = dubbed({})
    info["formats"] = formats

    assert refusal(info) == "ambiguous_original_language"


def test_speech_with_angle_brackets_but_no_tag_is_kept():
    assert caption_cues("1\n00:00:01,000 --> 00:00:02,000\nx<y and z>w\n", scrolling=False) == [(1.0, "x<y and z>w")]
