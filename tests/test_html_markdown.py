"""HTML a site renders (Reddit bodies, Ashby descriptions) as Markdown that keeps addresses and structure."""

from rsrc.html_markdown import to_markdown


def test_links_keep_their_address_and_relative_ones_are_resolved():
    html = ('<p>See <a href="https://example.com/a">the docs</a>, <a href="/r/Python">r/Python</a> and '
            '<a href="https://example.com/b">https://example.com/b</a>.</p>')

    assert to_markdown(html, "https://www.reddit.com/") == (
        "See [the docs](https://example.com/a), [r/Python](https://www.reddit.com/r/Python) and "
        "https://example.com/b.")


def test_an_image_keeps_its_address_and_a_linked_image_its_full_size_link():
    html = ('<p>Before</p><figure><a href="https://img.example/full.jpg">\n  <img src="https://img.example/small.jpg" '
            'alt="Comment Image"></a></figure><p><img src="https://img.example/inline.png"></p>')

    assert to_markdown(html) == ("Before\n\n![Comment Image](https://img.example/full.jpg)\n\n"
                                 "![image](https://img.example/inline.png)")


def test_lists_quotes_headings_and_rules_keep_their_markers():
    html = ("<h2>Plan</h2><ol start=\"3\"><li><p>three</p></li><li>four<ul><li>inner</li></ul></li></ol>"
            "<blockquote><p>quoted</p><p>twice</p></blockquote><hr><p>end</p>")

    assert to_markdown(html) == ("## Plan\n\n3. three\n4. four\n\n   - inner\n\n> quoted\n>\n> twice\n\n---\n\nend")


def test_code_keeps_its_spacing_and_inline_code_its_backticks():
    html = "<p>Run <code>uv  sync</code>:</p><pre><code>def f():\n    return 1\n</code></pre>"

    assert to_markdown(html) == "Run `uv  sync`:\n\n```\ndef f():\n    return 1\n```"


def test_a_table_becomes_rows_and_skips_empty_rows():
    html = ("<table><thead><tr><th>Name</th><th>Note</th></tr><tr></tr></thead>"
            "<tbody><tr><td>a</td><td>x | y</td></tr><tr><td>b</td></tr></tbody></table>")

    assert to_markdown(html) == "| Name | Note |\n|---|---|\n| a | x \\| y |\n| b |  |"


def test_emphasis_spoilers_superscript_and_line_breaks_are_marked():
    html = ('<p><strong>bold</strong> <em>it</em> <del>gone</del> <span class="md-spoiler-text">secret</span> '
            'x<sup>2</sup><br>next line</p>')

    assert to_markdown(html) == "**bold** *it* ~~gone~~ >!secret!< x^(2)\nnext line"


def test_embedded_media_keeps_its_address_and_scripts_are_dropped():
    html = ('<p>Watch:</p><iframe src="https://media.example/embed/1"></iframe>'
            '<video><source src="https://media.example/v.mp4"></video><script>track()</script>')

    assert to_markdown(html) == ("Watch:\n\n[embed](https://media.example/embed/1)\n\n"
                                 "[video](https://media.example/v.mp4)")


def test_layout_whitespace_collapses_and_empty_html_is_empty():
    html = '\n  <div class="md">\n    <p>\n      one\n      two\n    </p>\n  </div>\n'

    assert to_markdown(html) == "one two"
    assert to_markdown("") == to_markdown(None) == to_markdown("<div> </div>") == ""


def test_hidden_parts_scripts_and_script_links_add_nothing():
    html = ('<p>Current rate</p><p hidden>Old rate</p>'
            '<p><a href="JavaScript:alert(1)">click</a> <a href=" data:text/html,x">data</a></p>')

    assert to_markdown(html) == "Current rate\n\nclick data"


def test_a_thumbnail_linking_to_a_page_keeps_both_addresses():
    html = '<a href="https://example.com/post"><img src="https://img.example/t.jpg" alt="thumb"></a>'

    assert to_markdown(html) == "[![thumb](https://img.example/t.jpg)](https://example.com/post)"


def test_a_link_around_a_heading_keeps_where_it_leads():
    assert to_markdown('<a href="/jobs"><h2>Jobs</h2></a>', "https://jobs.example/") == (
        "## Jobs\n\n[link](https://jobs.example/jobs)")


def test_unclosed_list_items_empty_items_and_table_captions():
    html = ("<ul><li>One<li>Two<li><br></ul>"
            "<table><caption>Salaries in thousands of USD</caption><tr><th>Role</th></tr><tr><td>A</td></tr></table>")

    assert to_markdown(html) == "- One\n- Two\n\nSalaries in thousands of USD\n\n| Role |\n|---|\n| A |"


def test_addresses_with_parentheses_and_code_with_backticks_stay_whole():
    html = ('<p>See<a href="https://example.org/a_(b)"> the page </a>now and <code>a `b` c</code></p>'
            '<pre>```\nfenced\n```</pre>')

    assert to_markdown(html) == ("See [the page](<https://example.org/a_(b)>) now and ``a `b` c``\n\n"
                                 "````\n```\nfenced\n```\n````")


def test_a_malformed_address_keeps_the_link_text():
    assert to_markdown('<p><a href="http://[bad">text</a></p>', "https://www.reddit.com/") == (
        "[text](http://[bad)")


def test_an_image_with_only_a_srcset_keeps_its_largest_file():
    html = '<img srcset="https://img.example/s.jpg 320w, https://img.example/l.jpg 1080w" alt="Map [v2]">'

    assert to_markdown(html) == "![Map \\[v2\\]](https://img.example/l.jpg)"
