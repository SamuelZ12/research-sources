from html.parser import HTMLParser
from pathlib import Path

from rsrc import checks

FIXTURES = Path(__file__).parent / "fixtures"
ITEM_KEYS = ("id", "url", "title", "author", "published_at", "text", "source", "engagement", "raw")


def make_item(**fields):
    item = dict.fromkeys(ITEM_KEYS)
    item.update(fields)
    return item


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = []
        self.body = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        self._in_title = tag == "title"

    def handle_endtag(self, tag):
        self._in_title = False

    def handle_data(self, data):
        if data.strip():
            (self.title if self._in_title else self.body).append(data.strip())


def page_item(name, item_id="abc"):
    parser = _PageText()
    parser.feed((FIXTURES / name).read_text(encoding="utf-8"))
    return make_item(
        id=item_id,
        url=f"https://example.com/{item_id}",
        title=" ".join(parser.title),
        text="\n".join(parser.body),
    )


def test_reddit_block_page_is_rejected():
    item = page_item("block_reddit.html")
    assert item["title"] == "Blocked"
    assert checks.screen_read(item, "abc") == (
        None,
        {"url": "https://example.com/abc", "reason": "blocked_page"},
    )


def test_login_wall_is_rejected():
    item = page_item("login_wall.html")
    kept, rejected = checks.screen_read(item, "abc")
    assert kept is None
    assert rejected == {"url": "https://example.com/abc", "reason": "blocked_page"}


def test_markers_match_case_insensitively_in_title():
    assert checks.is_block_page("Just a moment...", "")
    assert checks.is_block_page("Attention Required! | Cloudflare", None)
    assert checks.is_block_page(None, "Please verify you are HUMAN to proceed")


def test_markers_are_stored_lowercase():
    assert all(marker == marker.lower() for marker in checks.BLOCK_MARKERS)


def test_marker_after_2000_chars_does_not_trigger():
    text = "x" * 2000 + " and then the server said access denied."
    item = make_item(id="1", url="https://example.com/1", title="Debugging a 403", text=text)
    assert not checks.is_block_page(item["title"], text)
    assert checks.screen_read(item, "1") == (item, None)


def test_marker_ending_inside_first_2000_chars_triggers():
    text = "y" * (2000 - len("access denied")) + "access denied" + " more"
    assert checks.is_block_page("A post", text)


def test_marker_straddling_2000_char_boundary_does_not_trigger():
    text = "y" * (2000 - len("access")) + "access denied"
    assert not checks.is_block_page("A post", text)


def test_ordinary_post_passes():
    item = make_item(
        id="1876",
        url="https://x.com/someone/status/1876",
        title="We shipped a new release of our agent framework today.",
        text="Changelog: faster startup, better memory, fewer bugs.",
    )
    assert checks.screen_read(item, "1876") == (item, None)


def test_none_title_and_text_pass():
    assert not checks.is_block_page(None, None)
    item = make_item(id="9", url="https://example.com/9")
    assert checks.screen_read(item, "9") == (item, None)


def test_id_mismatch_is_rejected():
    item = make_item(id="124", url="https://x.com/a/status/124", title="hello", text="world")
    assert checks.screen_read(item, "123") == (
        None,
        {"url": "https://x.com/a/status/124", "reason": "url_mismatch"},
    )


def test_case_insensitive_match_passes():
    item = make_item(id="letta-ai/letta", url="https://github.com/letta-ai/letta", title="letta")
    assert checks.screen_read(item, "Letta-AI/Letta", case_insensitive=True) == (item, None)


def test_case_sensitive_by_default():
    item = make_item(id="letta-ai/letta", url="https://github.com/letta-ai/letta")
    kept, rejected = checks.screen_read(item, "Letta-AI/Letta")
    assert kept is None
    assert rejected["reason"] == "url_mismatch"


def test_blocked_reported_before_mismatch():
    item = page_item("block_reddit.html", item_id="124")
    assert checks.screen_read(item, "123") == (
        None,
        {"url": "https://example.com/124", "reason": "blocked_page"},
    )
