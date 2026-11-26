"""Rendered HTML as Markdown, keeping what plain page text drops: link and image addresses, list
markers, quotes, code blocks, tables, headings and emphasis.

Used for HTML a site renders from its own Markdown or rich text (Reddit bodies, Ashby job
descriptions). Pure and dependency-free: the standard library's HTMLParser builds a small tree.
Text is not escaped, so it reads as the page showed it.
"""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

BLOCKS = frozenset({"p", "div", "section", "article", "header", "footer", "aside", "main", "figure", "figcaption",
                    "blockquote", "pre", "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "hr",
                    "h1", "h2", "h3", "h4", "h5", "h6", "details", "summary", "dl", "dt", "dd", "caption"})
VOID = frozenset({"br", "img", "hr", "input", "meta", "link", "source", "wbr", "col", "area", "base", "embed",
                  "track", "param"})
SKIP = frozenset({"script", "style", "template", "svg", "noscript", "button"})
INLINE_MARKS = {"strong": "**", "b": "**", "em": "*", "i": "*", "del": "~~", "s": "~~", "strike": "~~"}
# Elements whose content is media, not text: they render as their address.
MEDIA = frozenset({"iframe", "video", "audio", "embed", "object"})
IMAGE_FILE = re.compile(r"\.(?:png|jpe?g|gif|webp|avif|bmp|svg)(?:[?#]|$)|^https://(?:i|preview)\.redd\.it/",
                        re.IGNORECASE)
MAX_TABLE_CELLS = 10_000
_SPACE = re.compile(r"\s+")


def destination(url: str) -> str:
    """`url` as a Markdown link destination: in angle brackets when a space or parenthesis would
    end it early."""
    return f"<{url}>" if re.search(r"[\s()<>]", url) else url


def label(text: str) -> str:
    """`text` safe inside a link or image label."""
    return text.replace("[", "\\[").replace("]", "\\]")


def link(text: str, url: str) -> str:
    return "[" + label(text) + "](" + destination(url) + ")"


def fence(code: str) -> str:
    """`code` in a fence longer than any backtick run inside it."""
    ticks = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", code)), default=0))
    return f"{ticks}\n{code}\n{ticks}"


class Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag, attrs=None):
        self.tag, self.attrs, self.children = tag, dict(attrs or {}), []


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        if tag == "li":  # an `li` whose end tag was left out ends at its next sibling
            for depth in range(len(self.stack) - 1, 0, -1):
                if self.stack[depth].tag in ("ul", "ol"):
                    break
                if self.stack[depth].tag == "li":
                    del self.stack[depth:]
                    break
        node = Node(tag, [(k, v or "") for k, v in attrs])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, [(k, v or "") for k, v in attrs]))

    def handle_endtag(self, tag):
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                del self.stack[depth:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def to_markdown(html: str | None, base: str = "") -> str:
    """`html` as Markdown, blocks separated by a blank line; "" when it holds no text or media, or
    nests too deeply to walk. Relative link and image addresses are resolved against `base`."""
    if not html:
        return ""
    tree = _Tree()
    tree.feed(html)
    tree.close()
    try:
        return "\n\n".join(_Renderer(base).blocks(tree.root.children)).strip("\n")
    except RecursionError:
        return ""


def _hidden(node) -> bool:
    return isinstance(node, Node) and (node.tag in SKIP or "hidden" in node.attrs)


class _Renderer:
    def __init__(self, base):
        self.base = base

    def url(self, value):
        """An address resolved against the base; "" for a script or data address."""
        value = (value or "").strip()
        if value.lower().startswith(("javascript:", "data:", "vbscript:")):
            return ""
        if not value or not self.base:
            return value
        try:
            return urljoin(self.base, value)
        except ValueError:
            return value

    # --- blocks ---------------------------------------------------------------------

    def blocks(self, children) -> list[str]:
        out, run = [], []

        def flush():
            text = "".join(run).strip()
            text = re.sub(r" *\n *", "\n", text)
            if text:
                out.append(text)
            run.clear()

        for child in children:
            if _hidden(child):
                continue
            if isinstance(child, Node) and self.is_block(child):
                flush()
                out.extend(block for block in self.block(child) if block.strip())
            else:
                run.append(self.inline(child))
        flush()
        return out

    def is_block(self, node: Node) -> bool:
        return node.tag in BLOCKS or node.tag in MEDIA or any(
            isinstance(c, Node) and not _hidden(c) and self.is_block(c) for c in node.children)

    def block(self, node: Node) -> list[str]:
        tag = node.tag
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = " ".join(self.blocks(node.children))
            return [("#" * int(tag[1]) + " " + text) if text else ""]
        if tag == "hr":
            return ["---"]
        if tag in MEDIA:
            return [self.inline(node)]
        if tag == "pre":
            code = self.raw_text(node).strip("\n")
            return [fence(code)] if code.strip() else []
        if tag == "blockquote":
            inner = "\n\n".join(self.blocks(node.children))
            return ["\n".join(("> " + line).rstrip() for line in inner.split("\n"))] if inner else []
        if tag in ("ul", "ol"):
            return [self.list_block(node)]
        if tag == "table":
            return self.table(node)
        if tag == "a":  # a link around blocks: the blocks, then where it leads
            href = self.href(node)
            return self.blocks(node.children) + ([f"[link]({destination(href)})"] if href else [])
        return self.blocks(node.children)

    def list_block(self, node: Node) -> str:
        items, number = [], self.start(node)
        for child in node.children:
            if not isinstance(child, Node) or _hidden(child):
                continue
            if child.tag != "li":
                items.extend(self.blocks([child]))
                continue
            marker = f"{number}. " if node.tag == "ol" else "- "
            number += 1
            body = "\n\n".join(self.blocks(child.children))
            if not body:
                continue
            lines = body.split("\n")
            pad = " " * len(marker)
            items.append(marker + lines[0] + "".join("\n" + (pad + line if line else "") for line in lines[1:]))
        return "\n".join(items)

    @staticmethod
    def start(node: Node) -> int:
        try:
            return int(node.attrs.get("start", "1"))
        except ValueError:
            return 1

    def table(self, node: Node) -> list[str]:
        captions = [" ".join(self.blocks(c.children)) for c in node.children
                    if isinstance(c, Node) and c.tag == "caption"]
        rows = []
        for row in self.find(node, "tr"):
            cells = [c for c in row.children if isinstance(c, Node) and c.tag in ("th", "td")]
            if cells:
                rows.append([" ".join(" ".join(self.blocks(c.children)).split()).replace("|", "\\|")
                             for c in cells])
        if not rows:
            return [c for c in captions if c]
        width = max(len(r) for r in rows)
        if width * len(rows) > MAX_TABLE_CELLS:  # too wide to pad: one line per row instead
            return [c for c in captions if c] + ["\n".join(" | ".join(r) for r in rows)]
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return [c for c in captions if c] + ["\n".join(lines)]

    def find(self, node: Node, tag: str):
        for child in node.children:
            if isinstance(child, Node) and not _hidden(child):
                if child.tag == tag:
                    yield child
                elif child.tag != "table":
                    yield from self.find(child, tag)

    # --- inline ---------------------------------------------------------------------

    def href(self, node: Node) -> str:
        return self.url(node.attrs.get("href"))

    def inline(self, node) -> str:
        if isinstance(node, str):
            return _SPACE.sub(" ", node)
        tag = node.tag
        if _hidden(node):
            return ""
        if tag == "br":
            return "\n"
        if tag == "img":
            return self.image(node, node.attrs.get("src") or self.first_srcset(node))
        if tag in MEDIA:
            sources = [node.attrs.get("src") or node.attrs.get("data")]
            sources += [c.attrs.get("src") for c in node.children if isinstance(c, Node) and c.tag == "source"]
            src = self.url(next((s for s in sources if s), ""))
            label = "embed" if tag in ("iframe", "embed", "object") else tag
            return f"[{label}]({destination(src)})" if src else ""
        if tag == "a":
            return self.anchor(node)
        if tag == "code":
            code = self.raw_text(node)
            run = max((len(r) for r in re.findall(r"`+", code)), default=0)
            ticks = "`" * (run + 1)
            pad = " " if code.startswith("`") or code.endswith("`") else ""
            return f"{ticks}{pad}{code}{pad}{ticks}" if code.strip() else code
        if tag == "sup":
            text = self.inline_children(node).strip()
            return f"^({text})" if text else ""
        if tag == "span" and "md-spoiler-text" in node.attrs.get("class", "").split():
            text = self.inline_children(node).strip()
            return f">!{text}!<" if text else ""
        text = self.inline_children(node)
        mark = INLINE_MARKS.get(tag)
        if mark and text.strip():
            return self.wrap(text, lambda core: f"{mark}{core}{mark}")
        return text

    def anchor(self, node: Node) -> str:
        href = self.href(node)
        meaningful = [c for c in node.children if not (isinstance(c, str) and not c.strip())]
        if len(meaningful) == 1 and isinstance(meaningful[0], Node) and meaningful[0].tag == "img":
            image = meaningful[0]
            src = self.url(image.attrs.get("src") or self.first_srcset(image))
            if href and href != src and not IMAGE_FILE.search(href):  # a thumbnail linking to a page
                return f"[{self.image(image, src) or 'image'}]({destination(href)})"
            return self.image(image, href or src)
        text = self.inline_children(node)
        if not href or not text.strip():
            return href or text
        return self.wrap(text, lambda core: core if core == href else link(core, href))

    @staticmethod
    def wrap(text: str, make) -> str:
        """`make` applied to `text` without its outer spaces, which stay outside the markup."""
        lead, core, trail = text[:len(text) - len(text.lstrip())], text.strip(), text[len(text.rstrip()):]
        return f"{lead}{make(core)}{trail}"

    def inline_children(self, node: Node) -> str:
        return "".join(self.inline(c) for c in node.children)

    def image(self, node: Node, src) -> str:
        src = self.url(src)
        if not src:
            return ""
        alt = label(" ".join((node.attrs.get("alt") or "image").split()))
        return f"![{alt}]({destination(src)})"

    @staticmethod
    def first_srcset(node: Node) -> str:
        candidates = [c.strip().split(" ")[0] for c in node.attrs.get("srcset", "").split(",") if c.strip()]
        return candidates[-1] if candidates else ""  # the last candidate is the largest

    def raw_text(self, node) -> str:
        if isinstance(node, str):
            return node
        if node.tag == "br":
            return "\n"
        return "".join(self.raw_text(c) for c in node.children)
