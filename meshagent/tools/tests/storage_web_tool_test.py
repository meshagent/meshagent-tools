from __future__ import annotations

import asyncio
import brotli
import zlib

import backports.zstd as zstd
from aiohttp import ClientPayloadError
import pytest

from meshagent.api import RoomClient, RoomException
from meshagent.api.messaging import FileContent, JsonContent, TextContent
from meshagent.tools import ToolContext
from meshagent.tools._text_utils import grep_text, truncate_text
from meshagent.tools.blob import Blob, get_bytes_from_url
from meshagent.tools.storage import (
    StorageToolLocalMount,
    StorageToolRoomMount,
    StorageToolkit,
)
from meshagent.tools.web_toolkit import WebFetchTool, WebGrepTool, WebToolkit
import meshagent.tools.web_toolkit as web_toolkit
import meshagent.tools.storage as storage_toolkit


def test_web_infer_filename_uses_python_mimetypes_fallbacks() -> None:
    cases = [
        ("application/pdf", "downloaded-content.pdf"),
        ("image/png", "downloaded-content.png"),
        ("image/jpeg", "downloaded-content.jpg"),
        ("image/gif", "downloaded-content.gif"),
        ("image/webp", "downloaded-content.webp"),
        ("image/bmp", "downloaded-content.bmp"),
        ("image/tiff", "downloaded-content.tiff"),
        ("image/svg+xml", "downloaded-content.svg"),
        ("image/avif", "downloaded-content.avif"),
        ("image/heic", "downloaded-content.heic"),
        ("image/heif", "downloaded-content.heif"),
        ("text/plain", "downloaded-content.txt"),
        ("text/html", "downloaded-content.html"),
        ("application/json", "downloaded-content.json"),
        ("application/xhtml+xml", "downloaded-content.xhtml"),
        ("application/xml", "downloaded-content.xsl"),
        ("application/octet-stream", "downloaded-content.bin"),
        ("application/x-tar", "downloaded-content.tar"),
        ("application/zip", "downloaded-content.zip"),
        ("text/csv", "downloaded-content.csv"),
        ("text/markdown", "downloaded-content.md"),
        ("application/wasm", "downloaded-content.wasm"),
        ("audio/mpeg", "downloaded-content.mp3"),
        ("video/mp4", "downloaded-content.mp4"),
        ("application/x-bzip2", "downloaded-content.bz2"),
        ("application/x-7z-compressed", "downloaded-content.7z"),
        ("application/x-rar-compressed", "downloaded-content.rar"),
        ("application/vnd.ms-excel", "downloaded-content.xls"),
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "downloaded-content.xlsx",
        ),
        ("application/msword", "downloaded-content.doc"),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "downloaded-content.docx",
        ),
        ("application/vnd.ms-powerpoint", "downloaded-content.ppt"),
        (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "downloaded-content.pptx",
        ),
        ("application/rtf", "downloaded-content.rtf"),
        ("text/rtf", "downloaded-content.rtf"),
        ("application/x-sh", "downloaded-content.sh"),
        ("application/x-python-code", "downloaded-content.pyc"),
        ("text/css", "downloaded-content.css"),
        ("text/javascript", "downloaded-content.js"),
        ("application/epub+zip", "downloaded-content.epub"),
        ("application/vnd.apple.installer+xml", "downloaded-content.mpkg"),
        ("", "downloaded-content"),
    ]
    for content_type, expected in cases:
        assert (
            web_toolkit._infer_filename(
                url="https://example.com/download/",
                content_type=content_type,
            )
            == expected
        )


def test_web_url_extension_matches_python_splitext_hidden_files() -> None:
    assert web_toolkit._url_extension("https://example.com/.json") == ""
    assert web_toolkit._url_extension("https://example.com/..json") == ""
    assert web_toolkit._url_extension("https://example.com/.a.json") == ".json"
    assert web_toolkit._url_extension("https://example.com/file.") == "."
    assert web_toolkit._url_extension("https://example.com/file.yaml ") == ".yaml"
    assert web_toolkit._url_extension("https://example.com/.a.json ") == ".json"

    assert not web_toolkit._is_text_like_url(
        url="https://example.com/.json",
        content_type="application/octet-stream",
    )
    assert not web_toolkit._is_pdf_or_image_url(
        url="https://example.com/.pdf",
        content_type="",
    )
    assert web_toolkit._is_text_like_url(
        url="https://example.com/.a.json",
        content_type="application/octet-stream",
    )
    assert web_toolkit._is_text_like_url(
        url="https://example.com/file.yaml ",
        content_type="application/octet-stream",
    )
    assert web_toolkit._is_pdf_or_image_url(
        url="https://example.com/.a.pdf",
        content_type="",
    )


def test_html_to_markdown_media_source_fallbacks() -> None:
    from html_to_markdown import convert

    cases = [
        (
            '<video><source src="v.mp4">Fallback</video>',
            "[v.mp4](v.mp4)\n\nFallback\n",
        ),
        (
            '<video><source src="a.mp4"><source src="b.mp4">Fallback</video>',
            "[a.mp4](a.mp4)\n\nFallback\n",
        ),
        (
            '<audio><source src="a.ogg">Fallback</audio>',
            "[a.ogg](a.ogg)\n\nFallback\n",
        ),
        (
            '<video src="v.mp4"><source src="a.mp4">Fallback</video>',
            "[v.mp4](v.mp4)\n\nFallback\n",
        ),
        (
            '<video src=""><source src="v.mp4">Fallback</video>',
            "[v.mp4](v.mp4)\n\nFallback\n",
        ),
        (
            '<video><source src=""><source src="v.mp4">Fallback</video>',
            "Fallback\n",
        ),
        (
            '<audio><source srcset="a.mp3">Fallback</audio>',
            "Fallback\n",
        ),
        (
            '<picture><source srcset="a.webp"><img src="a.png" alt="A"></picture>',
            "![A](a.png)\n",
        ),
        (
            '<iframe src="">Fallback</iframe><p>A</p>',
            "A\n",
        ),
    ]
    for html, expected in cases:
        assert convert(html) == expected


def test_html_to_markdown_named_entity_decoding() -> None:
    from html_to_markdown import convert

    cases = [
        (
            "<p>&reg; &euro; &mdash; &ndash; &hellip; &rsquo; &lsquo; "
            "&ldquo; &rdquo;</p>",
            "® € — – … ’ ‘ “ ”\n",
        ),
        (
            "<p>&apos; &cent; &pound; &yen; &sect; &para; &notin;</p>",
            "' ¢ £ ¥ § ¶ ∉\n",
        ),
        (
            "<p>&trade; &laquo; &raquo; &bull; &middot; &plusmn; &times; &divide;</p>",
            "™ « » • · ± × ÷\n",
        ),
        (
            "<p>&deg; &micro; &alpha; &beta; &gamma; &Delta; &Omega; &rarr;</p>",
            "° µ α β γ Δ Ω →\n",
        ),
        (
            "<p>&le; &ge; &ne; &infin; &sum; &radic; &nbsp;X</p>",
            "≤ ≥ ≠ ∞ ∑ √ X\n",
        ),
        (
            "<p>&copy &trade &raquo</p>",
            "&copy &trade &raquo\n",
        ),
        (
            "<p>&notanentity; &#xZZ; &#999999999999;</p>",
            "&notanentity; &#xZZ; &#999999999999;\n",
        ),
        (
            "<p>&#65; &#065; &#x41; &#X41; &#x000041; &#X000041; "
            "&#x1f600; &#X1F600;</p>",
            "A A A A A A 😀 😀\n",
        ),
        (
            "<p>&#65 &#x41 &#X41 &#xD800; &#55296;</p>",
            "&#65 &#x41 &#X41 &#xD800; &#55296;\n",
        ),
        (
            '<a href="/x?c=&copy;&euro;&notin;">L</a>',
            "[L](/x?c=©€∉)\n",
        ),
        (
            '<iframe src="/x?c=&copy;&euro;&notin;"></iframe>',
            "[/x?c=&copy;&euro;&notin;](/x?c=&copy;&euro;&notin;)\n",
        ),
        (
            '<video src="x&amp;y">F</video>',
            "[x&amp;y](x&amp;y)\n\nF\n",
        ),
        (
            '<video><source src="x&amp;y">F</video>',
            "[x&amp;y](x&amp;y)\n\nF\n",
        ),
        (
            '<html><head><meta name="description" content="A &copy; B">'
            "<title>T &copy;</title></head><body><p>X</p></body></html>",
            "---\nmeta-description: A &copy; B\ntitle: T &copy;\n---\n\n\nX\n",
        ),
        (
            "<p>X&ensp;Y X&emsp;Y X&thinsp;Y X&zwnj;Y X&zwj;Y X&lrm;Y X&rlm;Y</p>",
            "X Y X Y X Y X\u200cY X\u200dY X\u200eY X\u200fY\n",
        ),
        (
            "<p>&Aacute; &aacute; &Eacute; &eacute; &Iacute; &iacute; "
            "&Oacute; &oacute; &Uacute; &uacute; &Ntilde; &ntilde; "
            "&Ccedil; &ccedil;</p>",
            "Á á É é Í í Ó ó Ú ú Ñ ñ Ç ç\n",
        ),
        (
            "<p>&aring; &auml; &ouml; &uuml; &yuml; &frac12; &frac14; "
            "&frac34; &sup2; &sup3; &minus;</p>",
            "å ä ö ü ÿ ½ ¼ ¾ ² ³ −\n",
        ),
        (
            "<p>&permil; &dagger; &Dagger; &lsaquo; &rsaquo; &prime; "
            "&Prime; &oline; &frasl;</p>",
            "‰ † ‡ ‹ › ′ ″ ‾ ⁄\n",
        ),
        (
            "<p>&spades; &clubs; &hearts; &diams; &loz; &harr; &larr; "
            "&uarr; &darr;</p>",
            "♠ ♣ ♥ ♦ ◊ ↔ ← ↑ ↓\n",
        ),
        (
            "<p>&forall; &part; &exist; &empty; &nabla; &isin; &ni; &prod; &int;</p>",
            "∀ ∂ ∃ ∅ ∇ ∈ ∋ ∏ ∫\n",
        ),
        (
            "<p>&and; &or; &cap; &cup; &sub; &sup; &sube; &supe; "
            "&oplus; &otimes; &perp; &sdot; &lceil; &rceil; "
            "&lfloor; &rfloor; &lang; &rang;</p>",
            "∧ ∨ ∩ ∪ ⊂ ⊃ ⊆ ⊇ ⊕ ⊗ ⊥ ⋅ ⌈ ⌉ ⌊ ⌋ ⟨ ⟩\n",
        ),
        ("<p>&nbsp;&Tab;&NewLine;&ZeroWidthSpace;&NoBreak;</p>", " \u200b\u2060\n"),
        (
            "<p>&OElig; &oelig; &Scaron; &scaron; &Yuml; &circ; &tilde;</p>",
            "Œ œ Š š Ÿ ˆ ˜\n",
        ),
        (
            "<p>&Alpha; &Beta; &Gamma; &delta; &epsilon; &zeta; &eta; "
            "&theta; &lambda; &pi; &sigma; &phi; &psi;</p>",
            "Α Β Γ δ ε ζ η θ λ π σ φ ψ\n",
        ),
        (
            "<p>&sim; &cong; &asymp; &equiv; &prop; &there4; &not; &ang;</p>",
            "∼ ≅ ≈ ≡ ∝ ∴ ¬ ∠\n",
        ),
        (
            "<p>&real; &image; &weierp; &alefsym; &crarr; &rArr; "
            "&lArr; &uArr; &dArr; &hArr;</p>",
            "ℜ ℑ ℘ ℵ ↵ ⇒ ⇐ ⇑ ⇓ ⇔\n",
        ),
        (
            "<p>&Agrave; &agrave; &Acirc; &acirc; &Atilde; &atilde; "
            "&AElig; &aelig; &Egrave; &egrave; &Ecirc; &ecirc; &Euml; &euml;</p>",
            "À à Â â Ã ã Æ æ È è Ê ê Ë ë\n",
        ),
        (
            "<p>&Igrave; &igrave; &Icirc; &icirc; &Iuml; &iuml; "
            "&Ograve; &ograve; &Ocirc; &ocirc; &Otilde; &otilde; &Oslash; &oslash;</p>",
            "Ì ì Î î Ï ï Ò ò Ô ô Õ õ Ø ø\n",
        ),
        (
            "<p>&Ugrave; &ugrave; &Ucirc; &ucirc; &ETH; &eth; "
            "&THORN; &thorn; &szlig; &divide; &times;</p>",
            "Ù ù Û û Ð ð Þ þ ß ÷ ×\n",
        ),
        (
            "<p>&iexcl; &iquest; &brvbar; &uml; &macr; &acute; &cedil; "
            "&ordf; &ordm; &shy;</p>",
            "¡ ¿ ¦ ¨ ¯ ´ ¸ ª º \xad\n",
        ),
        (
            "<p>&curren; &euro; &fnof; &copy; &reg; &trade; &sect; &para;</p>",
            "¤ € ƒ © ® ™ § ¶\n",
        ),
        (
            "<p>&Chi; &chi; &Psi; &omega; &xi; &rho; &tau; &upsilon;</p>",
            "Χ χ Ψ ω ξ ρ τ υ\n",
        ),
        ("<p>&thetasym; &upsih; &piv; &middot; &bull;</p>", "ϑ ϒ ϖ · •\n"),
    ]
    for html, expected in cases:
        assert convert(html) == expected


def test_html_to_markdown_table_edge_cases() -> None:
    from html_to_markdown import convert

    cases = [
        (
            "<table><tr><td>1</td><td>2</td><td>3</td></tr><tr><td>4</td></tr></table>",
            "\n\n- 1 2 3\n- 4\n",
        ),
        (
            "<table><tr><td><p>A</p><p>B</p></td></tr></table>",
            "\n\n| A<br>B |\n| --- |\n",
        ),
        (
            '<table><tr><td COLSPAN="2">A</td><td>B</td></tr></table>',
            "\n\n| A | B |\n| --- | --- |\n",
        ),
        (
            '<table><tr><th rowspan="2">A</th><th>B</th></tr>'
            "<tr><td>C</td></tr></table>",
            "\n\n| A | B |\n| --- | --- |\n|  | C |\n",
        ),
        (
            '<table><tr><th ROWSPAN="2">A</th><th>B</th></tr>'
            "<tr><td>C</td></tr></table>",
            "\n\n| A | B |\n| --- | --- |\n| C |\n",
        ),
        (
            '<table><tr><th COLSPAN="2">A</th><th>B</th></tr></table>',
            "\n\n| A | B |\n| --- | --- |\n",
        ),
    ]
    for html, expected in cases:
        assert convert(html) == expected


def test_html_to_markdown_pre_edge_cases() -> None:
    from html_to_markdown import convert

    cases = [
        ("<pre> line</pre>", "    line\n"),
        ("<pre>\n\nline\n\n</pre>", "    line\n"),
        ("<pre>line\r\nnext</pre>", "    line\n    next\n"),
        ("<pre><span>A</span></pre>", "    A\n"),
        ("<pre>before <code>code</code> after</pre>", "    before code after\n"),
        ("<pre>A&nbsp;&amp;&copy;</pre>", "    A\xa0&©\n"),
        ("<pre>&#65; &#X41; &#xD800;</pre>", "    A A &#xD800;\n"),
        ("<pre>a < b > c</pre>", "    a < b > c\n"),
    ]
    for html, expected in cases:
        assert convert(html) == expected


def test_html_to_markdown_metadata_and_svg_edge_cases() -> None:
    from html_to_markdown import convert

    cases = [
        (
            '<html><head><meta name="a" content="A"><meta name="b" content="B">'
            "<title>T</title></head><body><p>B</p></body></html>",
            "---\nmeta-a: A\nmeta-b: B\ntitle: T\n---\n\n\nB\n",
        ),
        (
            '<html><head><meta name="a" content=""><meta name="b">'
            '<meta content="C"><title>T</title></head><body><p>B</p></body></html>',
            "---\nmeta-a:\ntitle: T\n---\n\n\nB\n",
        ),
        (
            '<html><head><meta name="a" content="A &copy;">'
            '<meta property="og:title" content="OG &copy;"></head><body><p>B</p></body></html>',
            "---\nmeta-a: A &copy;\nmeta-og:title: OG &copy;\n---\n\n\nB\n",
        ),
        (
            '<html><head><meta NAME="a" content="A">'
            '<meta name="b" CONTENT="B"></head><body><p>B</p></body></html>',
            "B\n",
        ),
        (
            "<svg><text>SVG</text></svg>",
            "![SVG Image](data:image/svg+xml;base64,PHN2Zz48dGV4dD5TVkc8L3RleHQ+PC9zdmc+)\n",
        ),
        (
            "<svg><title>T &copy;</title><text>SVG</text></svg>",
            "![T ©](data:image/svg+xml;base64,PHN2Zz48dGl0bGU+VCAmY29weTs8L3RpdGxlPjx0ZXh0PlNWRzwvdGV4dD48L3N2Zz4=)\n",
        ),
    ]
    for html, expected in cases:
        assert convert(html) == expected


def test_html_to_markdown_mathml_edge_cases() -> None:
    from html_to_markdown import convert

    cases = [
        ("<math></math>", ""),
        (
            "<math><mtext>A&nbsp;&amp;&copy;</mtext></math>",
            "<!-- MathML: <math><mtext>A&nbsp;&amp;&copy;</mtext></math> --> A\xa0&©\n",
        ),
        (
            "<math><mtext>&#65;&nbsp;&#X41;</mtext></math>",
            "<!-- MathML: <math><mtext>&#65;&nbsp;&#X41;</mtext></math> --> A\xa0A\n",
        ),
        (
            '<math><annotation encoding="application/x-tex">x^2</annotation>'
            "<mi>x</mi></math>",
            '<!-- MathML: <math><annotation encoding="application/x-tex">x^2</annotation>'
            "<mi>x</mi></math> --> x^2x\n",
        ),
        (
            '<math display="block"><mi>x</mi></math><p>A</p>',
            '\n\n<!-- MathML: <math display="block"><mi>x</mi></math> --> x\n\nA\n',
        ),
        (
            '<math DISPLAY="block"><mi>x</mi></math><p>A</p>',
            '<!-- MathML: <math DISPLAY="block"><mi>x</mi></math> --> x\n\nA\n',
        ),
    ]
    for html, expected in cases:
        assert convert(html) == expected


def test_html_to_markdown_link_attribute_edge_cases() -> None:
    from html_to_markdown import convert

    cases = [
        ('<a href="x y">L</a>', "[L](<x y>)\n"),
        ("<a href=>L</a>", "[L](<>)\n"),
        ("<a href=x&y>L</a>", "[L](x&y)\n"),
        ("<a href=x title=>L</a>", '[L](x "")\n'),
        ("<a href=x title>L</a>", "[L](x)\n"),
        ("<img src=x title=>", '![](x "")\n'),
        ("<img src=x title>", "![](x)\n"),
        ('<iframe src="x y"></iframe>', "[x y](x y)\n"),
        ('<video src="x y">F</video>', "[x y](x y)\n\nF\n"),
    ]
    for html, expected in cases:
        assert convert(html) == expected


def test_html_to_markdown_blockquote_edge_cases() -> None:
    from html_to_markdown import convert

    cases = [
        (
            "<blockquote><p>Quote</p><p>Two</p></blockquote><p>A</p>",
            "> Quote\n>\n> Two\n\nA\n",
        ),
        (
            "<blockquote>Quote</blockquote><p>A</p>",
            "> Quote\n\nA\n",
        ),
        (
            "<blockquote><blockquote>Deep</blockquote></blockquote>",
            "> > Deep\n",
        ),
    ]
    for html, expected in cases:
        assert convert(html) == expected


def test_html_to_markdown_abbr_attribute_edge_cases() -> None:
    from html_to_markdown import convert

    cases = [
        (
            '<p><abbr title="HyperText Markup Language">HTML</abbr></p>',
            "HTML (HyperText Markup Language)\n",
        ),
        ("<p><abbr title=>A</abbr></p>", "A\n"),
        ("<p><abbr title>A</abbr></p>", "A\n"),
        ('<p><abbr title="">A</abbr></p>', "A\n"),
        ('<p><abbr TITLE="X">A</abbr></p>', "A\n"),
    ]
    for html, expected in cases:
        assert convert(html) == expected


def test_html_to_markdown_case_sensitive_attribute_edge_cases() -> None:
    from html_to_markdown import convert

    cases = [
        ('<ol start="3"><li>A</li></ol>', "3. A\n"),
        ('<ol START="3"><li>A</li></ol>', "1. A\n"),
        ('<ol start="0"><li>A</li><li>B</li></ol>', "0. A\n1. B\n"),
        (
            '<select><optgroup label="G"><option>A</option></optgroup></select>',
            "**G**\nA\n",
        ),
        ('<select><optgroup LABEL="G"><option>A</option></optgroup></select>', "A\n"),
    ]
    for html, expected in cases:
        assert convert(html) == expected


def test_html_to_markdown_attribute_name_boundary_edge_cases() -> None:
    from html_to_markdown import convert

    cases = [
        ('<a data-href="x">L</a>', "L\n"),
        ('<img data-src="x.png" alt="A">', "![A]()\n"),
        ('<img data-title="T" src="x.png">', "![](x.png)\n"),
        ('<ol data-start="3"><li>A</li></ol>', "1. A\n"),
        (
            '<select><optgroup data-label="G"><option>A</option></optgroup></select>',
            "A\n",
        ),
        ('<abbr data-title="T">A</abbr>', "A\n"),
        (
            '<html><head><meta data-name="description" content="D">'
            '<meta name="x" data-content="Y"><title>T</title></head><body>B</body></html>',
            "---\ntitle: T\n---\nB\n",
        ),
    ]
    for html, expected in cases:
        assert convert(html) == expected


def test_html_to_markdown_definition_list_edge_cases() -> None:
    from html_to_markdown import convert

    cases = [
        ("<dl><dt>A</dt><dd>B</dd></dl>", "A\n:   B\n"),
        ("<dl><dt>A</dt><dt>B</dt><dd>C</dd></dl>", "A\nB\n:   C\n"),
        ("<dl><dt>A</dt><dd>B</dd><dt>C</dt><dd>D</dd></dl>", "A\n:   B\n\nC\n:   D\n"),
        ("<dl><dd>B</dd><dd>C</dd></dl>", "B\n\nC\n"),
        ("<p>X</p><dl><dt>A</dt><dd>B</dd></dl><p>Y</p>", "X\n\nA\n:   B\n\nY\n"),
    ]
    for html, expected in cases:
        assert convert(html) == expected


def test_html_to_markdown_structural_inline_block_edge_cases() -> None:
    from html_to_markdown import convert

    cases = [
        ("<article>Article</article><section>Section</section>", "ArticleSection\n"),
        ("<main>Main</main><footer>Foot</footer><p>Next</p>", "MainFoot\n\nNext\n"),
        ("<aside>Aside</aside><nav>Nav</nav><p>Next</p>", "AsideNav\n\nNext\n"),
        ("<address>A</address><address>B</address>", "AB\n"),
        ("<div>A</div><div>B</div>", "A\n\nB\n"),
        ("<p>A</p><unknown>B</unknown><p>C</p>", "A\n\nB\n\nC\n"),
        ("<a href='x y' title='T U'>L</a>", '[L](<x y> "T U")\n'),
        ("<img src='x.png' alt='A B' title='T U'>", '![A B](x.png "T U")\n'),
        ("<abbr title='Title'>A</abbr>", "A (Title)\n"),
        ("<ol start='4'><li>A</li></ol>", "4. A\n"),
        ("<optgroup label='G'><option>A</option></optgroup>", "**G**\nA\n"),
        ("<meta name='description' content='Desc'><p>A</p>", "A\n"),
        ("<p data-x='a > b'>A</p>", "A\n"),
        ('<a href="x" title="A &amp; B">L</a>tail', '[L](x "A &amp; B")tail\n'),
        ('<img src="x y" alt="A">tail', "![A](x y)tail\n"),
        ('<img src="" alt="A">tail', "![A]()tail\n"),
        ('<a href="x"><span>Label</span></a>tail', "[Label](x)tail\n"),
        ('<a href="x">L', "[L](x)\n"),
        ("<strong>A", "**A**\n"),
        ("<em>A", "*A*\n"),
        ("<code>A", "`A`\n"),
        ("<del>A", "~~A~~\n"),
        ("<mark>A", "==A==\n"),
        ('<abbr title="T">A', "A (T)\n"),
        ('<p><a href="x">L</p>tail', "[Ltail](x)\n"),
        ("<p><strong>A</p>tail", "**Atail**\n"),
        ("<p><em>A</p>tail", "*Atail*\n"),
        ("<p><code>A</p>tail", "`Atail`\n"),
        ("<p><del>A</p>tail", "~~Atail~~\n"),
        ("<p><mark>A</p>tail", "==Atail==\n"),
        ("<strong><em>A", "***A***\n"),
        ("<em><strong>A", "***A***\n"),
        ('<a href="x"><strong>A', "[**A**](x)\n"),
        ('<strong><a href="x">A', "**[A](x)**\n"),
        ('<abbr title="T"><strong>A', "**A** (T)\n"),
        ("<strong><em>A</strong>B</em>", "***AB***\n"),
        ("<em><strong>A</em>B</strong>", "***AB***\n"),
        ("<strong>A</em>B", "**AB**\n"),
        ("<em>A</strong>B", "*AB*\n"),
        ('<a href="x">A</strong>B', "[AB](x)\n"),
        ("<strong><code>A</code>B", "**`A`B**\n"),
        ("<code><strong>A</strong>B", "`AB`\n"),
        ("<p><strong><em>A</p>tail", "***Atail***\n"),
        ('<p><a href="x"><strong>A</p>tail', "[**Atail**](x)\n"),
        ('<p><abbr title="T"><em>A</p>tail', "*Atail* (T)\n"),
        ("<hr>tail", "---\ntail\n"),
        ("head<hr>", "head\n\n---\n"),
        ("<br>tail", "\ntail\n"),
        ("head<br>tail", "head  \ntail\n"),
        ("<ul><li>A</li></ul>tail", "- A\ntail\n"),
        ("<ol><li>A</li></ol>tail", "1. A\ntail\n"),
        ("<dl><dt>A</dt><dd>B</dd></dl>tail", "A\n:   B\n\ntail\n"),
        ("<blockquote>A</blockquote>tail", "> A\n\ntail\n"),
        ("<h1>A</h1>tail", "# A\n\ntail\n"),
        ("<table><tr><td>A</td></tr></table>tail", "\n\n| A |\n| --- |\ntail\n"),
        ("<pre>A</pre>tail", "    A\n\ntail\n"),
        (
            "<svg><text>A</text></svg>tail",
            "![SVG Image](data:image/svg+xml;base64,PHN2Zz48dGV4dD5BPC90ZXh0Pjwvc3ZnPg==)tail\n",
        ),
        (
            "<math><mi>x</mi></math>tail",
            "<!-- MathML: <math><mi>x</mi></math> --> xtail\n",
        ),
        ("<details><summary>A</summary>B</details>tail", "**A**\n\nB\n\ntail\n"),
        ("<fieldset><legend>A</legend>B</fieldset>tail", "**A**\n\nB\n\ntail\n"),
        ("<select><option>A</option></select>tail", "A\n\ntail\n"),
        ("<button>A</button>tail", "A\n\ntail\n"),
        ("<label>A</label>tail", "A\n\ntail\n"),
        ("<dialog>A</dialog>tail", "A\n\ntail\n"),
        ("<dialog>A</dialog><dialog>B</dialog>", "A\n\nB\n"),
        ("<p><dialog>A</dialog> B</p>", "A\n\nB\n"),
        ("<summary>A</summary><summary>B</summary>", "**A**\n\n**B**\n"),
        ("<p><summary>A</summary> B</p>", "**A**\n\nB\n"),
        ("<details><summary>Sum</summary>Tail</details>", "**Sum**\n\nTail\n"),
        ("<hgroup><h1>A</h1><p>B</p></hgroup>", "# A\n\nB\n"),
        (
            '<figure><img src="x.png" alt="Alt"><figcaption>Cap</figcaption></figure>',
            "![Alt](x.png)Cap\n",
        ),
        ("<figure><figcaption>Cap</figcaption></figure>", "Cap\n"),
        ("<select><option>A</option><option>B</option></select>", "A\nB\n"),
        ("<label>A</label><label>B</label>", "A\n\nB\n"),
        ("<p><label>A</label> B</p>", "A\n\nB\n"),
        ("<button>Click</button><p>Next</p>", "Click\n\nNext\n"),
        ("<output>42</output><p>Next</p>", "42\n\nNext\n"),
        ("<template><p>Hidden</p></template><p>Shown</p>", "Hidden\n\nShown\n"),
        ("<fieldset><legend>L</legend>Tail</fieldset>", "**L**\n\nTail\n"),
        (
            '<form><label>Name</label><input value="J"><button>Go</button></form><p>After</p>',
            "Name\n\nGo\n\nAfter\n",
        ),
        ('<input value="J"><input placeholder="P"><textarea>T</textarea>', "T\n"),
        ("<meter>5</meter><p>A</p>", "5\n\nA\n"),
        ("x <progress>50</progress> y", "x 50\n\ny\n"),
        ('<video src="v.mp4"/>After', "[v.mp4](v.mp4)\n\nAfter\n"),
        ('<audio><source src="a.ogg"/></audio><p>A</p>', "[a.ogg](a.ogg)\n\nA\n"),
        ('<source src="orphan.mp4"><p>A</p>', "A\n"),
        ('<iframe src="x"/>After', "[x](x)\n\nAfter\n"),
        ("<ruby>漢<rp>(</rp><rt>kan</rt><rp>)</rp></ruby>", "漢(kan)\n"),
        ("<h1>A</h1><h2>B</h2><h5>E</h5>", "# A\n\n## B\n\n##### E\n"),
        ("<p>https://example.com a@b.com</p>", "https://example.com a@b.com\n"),
        (
            '<div class="ocr_page"><span class="ocrx_word" title="bbox 0 0 10 10">Hi</span></div>',
            "Hi\n",
        ),
        (
            '<p>Before</p><input value="x"><source src="x"><embed src="y"><p>After</p>',
            "Before\n\nAfter\n",
        ),
        (
            '<video><source src=""><source src="v.mp4">Fallback</video>',
            "Fallback\n",
        ),
        ('<audio><source srcset="a.mp3">Fallback</audio>', "Fallback\n"),
        (
            '<picture><source srcset="a.webp"><img src="a.png" alt="A"></picture>',
            "![A](a.png)\n",
        ),
        ('<iframe src="">Fallback</iframe><p>A</p>', "A\n"),
        (
            '<p><object data="x.swf">Fallback</object><embed src="x.swf">Tail</p>',
            "FallbackTail\n",
        ),
        ("<canvas>Canvas</canvas><noscript>No script</noscript>", "CanvasNo script\n"),
        (
            "<p>before</p><noscript><p>No JS</p></noscript><p>after</p>",
            "before\n\nNo JS\n\nafter\n",
        ),
        ("<script>bad</script><style>bad</style><p>Good</p>", "\n\nGood\n"),
    ]
    for html, expected in cases:
        assert convert(html) == expected


class _FakeResponse:
    def __init__(
        self,
        *,
        data: bytes,
        status: int = 200,
        content_type: str = "text/plain",
        charset: str | None = "utf-8",
    ) -> None:
        self._data = data
        self.status = status
        self.content_type = content_type
        self.charset = charset

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        del exc_type
        del exc
        del tb
        return False

    async def read(self) -> bytes:
        return self._data


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.requested_url: str | None = None
        self.requested_headers: dict[str, str] | None = None

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        del exc_type
        del exc
        del tb
        return False

    def get(self, url: str, headers: dict[str, str]) -> _FakeResponse:
        self.requested_url = url
        self.requested_headers = headers
        return self._response


def _tool_context() -> ToolContext:
    return ToolContext(caller=object())


@pytest.mark.asyncio
async def test_data_url_blob_decode_matches_python_base64_leniency() -> None:
    blob = await get_bytes_from_url(url="data:text/plain;base64,aGVsbG8=")
    assert blob.mime_type == "data:text/plain;base64"
    assert blob.data == b"hello"

    for encoded in ["@@@", "A Q I D", "AQID====", "=AQID", "AQ=ID", "AQ-ID", "AQ_ID"]:
        blob = await get_bytes_from_url(url=f"data:text/plain;base64,{encoded}")
        expected = b"" if encoded == "@@@" else b"\x01\x02\x03"
        assert blob.data == expected

    with pytest.raises(ValueError, match="only ASCII characters"):
        await get_bytes_from_url(url="data:text/plain;base64,é")

    with pytest.raises(Exception, match="Incorrect padding"):
        await get_bytes_from_url(url="data:text/plain;base64,AQ")

    with pytest.raises(
        Exception,
        match="number of data characters \\(5\\) cannot be 1 more than a multiple of 4",
    ):
        await get_bytes_from_url(url="data:,hello")


async def _serve_one_http_response(
    *,
    body: bytes,
    content_type: str = "text/plain",
    content_encoding: str | None = None,
) -> tuple[asyncio.AbstractServer, int, list[str]]:
    requests: list[str] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        requests.append(request.decode("latin1"))
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            + f"Content-Type: {content_type}\r\n".encode()
            + (
                f"Content-Encoding: {content_encoding}\r\n".encode()
                if content_encoding is not None
                else b""
            )
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n"
            + b"\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, requests


@pytest.mark.asyncio
async def test_get_bytes_from_url_applies_host_alias_and_ignores_proxy_env(
    monkeypatch,
) -> None:
    server, port, requests = await _serve_one_http_response(body=b"direct")
    monkeypatch.setenv(
        "MESHAGENT_HTTP_HOST_ALIASES",
        " ignored, missing-target=, meshagent-python-alias.test = 127.0.0.1 ",
    )
    monkeypatch.delenv("MESHAGENT_EXTRA_CA_FILE", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)

    try:
        blob = await get_bytes_from_url(
            url=f"http://meshagent-python-alias.test:{port}/file"
        )
    finally:
        server.close()
        await server.wait_closed()

    assert blob.mime_type == "text/plain"
    assert blob.data == b"direct"
    assert requests[0].startswith("GET /file ")
    assert "Host: meshagent-python-alias.test:" in requests[0]


@pytest.mark.asyncio
async def test_get_bytes_from_url_decompresses_deflate_like_aiohttp() -> None:
    server, _port, _requests = await _serve_one_http_response(
        body=zlib.compress(b"ok"),
        content_encoding="deflate",
    )
    port = server.sockets[0].getsockname()[1]

    try:
        blob = await get_bytes_from_url(url=f"http://127.0.0.1:{port}/file")
    finally:
        server.close()
        await server.wait_closed()

    assert blob.mime_type == "text/plain"
    assert blob.data == b"ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_encoding", "body"),
    [
        ("br", brotli.compress(b"ok")),
        ("zstd", zstd.compress(b"ok")),
    ],
)
async def test_get_bytes_from_url_decompresses_optional_encodings_like_aiohttp(
    content_encoding: str,
    body: bytes,
) -> None:
    server, _port, _requests = await _serve_one_http_response(
        body=body,
        content_encoding=content_encoding,
    )
    port = server.sockets[0].getsockname()[1]

    try:
        blob = await get_bytes_from_url(url=f"http://127.0.0.1:{port}/file")
    finally:
        server.close()
        await server.wait_closed()

    assert blob.mime_type == "text/plain"
    assert blob.data == b"ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("content_encoding", ["gzip", "deflate", "br", "zstd"])
async def test_get_bytes_from_url_malformed_encoding_errors_match_aiohttp(
    content_encoding: str,
) -> None:
    server, _port, _requests = await _serve_one_http_response(
        body=b"not valid compressed bytes",
        content_encoding=content_encoding,
    )
    port = server.sockets[0].getsockname()[1]

    try:
        with pytest.raises(
            ClientPayloadError,
            match=f"Can not decode content-encoding: {content_encoding}",
        ):
            await get_bytes_from_url(url=f"http://127.0.0.1:{port}/file")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_get_bytes_from_url_loads_extra_ca_env_before_request(
    tmp_path,
    monkeypatch,
) -> None:
    extra_ca_file = tmp_path / "invalid-ca.pem"
    extra_ca_file.write_text("not a certificate")
    monkeypatch.delenv("MESHAGENT_HTTP_HOST_ALIASES", raising=False)
    monkeypatch.setenv("MESHAGENT_EXTRA_CA_FILE", str(extra_ca_file))

    with pytest.raises(Exception, match="certificate|PEM|no start line"):
        await get_bytes_from_url(url="http://127.0.0.1:9/file")


class _FakeStorageClient:
    def __init__(self) -> None:
        self.upload_calls: list[dict] = []
        self.download_calls: list[str] = []

    async def upload(self, *, path: str, data: bytes, overwrite: bool) -> None:
        self.upload_calls.append(
            {
                "path": path,
                "data": data,
                "overwrite": overwrite,
            }
        )

    async def download(self, *, path: str) -> FileContent:
        self.download_calls.append(path)
        return FileContent(
            name="rules.txt",
            mime_type="text/plain",
            data=b"hello from room storage",
        )

    async def exists(self, *, path: str) -> bool:
        del path
        return False


class _FakeSyncClient:
    async def describe(self, *, path: str) -> dict:
        del path
        return {"ok": True}


class _FakeRoom(RoomClient):
    def __init__(self) -> None:
        self.storage = _FakeStorageClient()
        self.sync = _FakeSyncClient()


@pytest.mark.asyncio
async def test_read_file_supports_offset_and_truncation(tmp_path) -> None:
    content = "0123456789" * 30
    file_path = tmp_path / "sample.txt"
    file_path.write_text(content, encoding="utf-8")

    toolkit = StorageToolkit(
        read_only=True,
        max_length=64,
        mounts=[
            StorageToolLocalMount(path="/", local_path=str(tmp_path)),
        ],
    )
    result = await toolkit.execute(
        context=_tool_context(),
        name="read_file",
        input=JsonContent(
            json={
                "path": "/sample.txt",
                "offset": 17,
            }
        ),
    )

    assert isinstance(result, TextContent)
    assert result.text == truncate_text(text=content, offset=17, max_length=64)


def test_resolve_storage_path_uses_first_duplicate_mount_path(tmp_path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    prepared = storage_toolkit._prepare_mounts(
        [
            StorageToolLocalMount(path="/project", local_path=str(first_root)),
            StorageToolLocalMount(path="/project", local_path=str(second_root)),
        ]
    )

    resolved = storage_toolkit._resolve_storage_path(prepared, "/project/rules.md")

    assert resolved.mount is prepared[0].mount
    assert resolved.local_path == str(first_root / "rules.md")


@pytest.mark.asyncio
async def test_read_file_returns_binary_file_content_unchanged(tmp_path) -> None:
    data = b"%PDF-1.7\n\x00\x01\x02binary"
    file_path = tmp_path / "sample.pdf"
    file_path.write_bytes(data)

    toolkit = StorageToolkit(
        read_only=True,
        max_length=1,
        mounts=[
            StorageToolLocalMount(path="/", local_path=str(tmp_path)),
        ],
    )
    result = await toolkit.execute(
        context=_tool_context(),
        name="read_file",
        input=JsonContent(
            json={
                "path": "/sample.pdf",
                "offset": 9999,
            }
        ),
    )

    assert isinstance(result, FileContent)
    assert result.mime_type == "application/pdf"
    assert result.data == data


@pytest.mark.asyncio
async def test_read_file_treats_yaml_as_text_when_mime_is_unknown(
    tmp_path, monkeypatch
) -> None:
    content = "name: webmaster\nversion: v1\n"
    file_path = tmp_path / "webmaster.yaml"
    file_path.write_text(content, encoding="utf-8")

    monkeypatch.setattr(
        storage_toolkit.mimetypes,
        "guess_type",
        lambda _path: (None, None),
    )

    toolkit = StorageToolkit(
        read_only=True,
        max_length=500,
        mounts=[
            StorageToolLocalMount(path="/", local_path=str(tmp_path)),
        ],
    )
    result = await toolkit.execute(
        context=_tool_context(),
        name="read_file",
        input=JsonContent(
            json={
                "path": "/webmaster.yaml",
                "offset": 0,
            }
        ),
    )

    assert isinstance(result, TextContent)
    assert result.text == content


@pytest.mark.asyncio
async def test_read_file_treats_json_as_text_when_mime_is_unknown(
    tmp_path, monkeypatch
) -> None:
    content = '{"name":"webmaster","version":"v1"}\n'
    file_path = tmp_path / "webmaster.json"
    file_path.write_text(content, encoding="utf-8")

    monkeypatch.setattr(
        storage_toolkit.mimetypes,
        "guess_type",
        lambda _path: (None, None),
    )

    toolkit = StorageToolkit(
        read_only=True,
        max_length=500,
        mounts=[
            StorageToolLocalMount(path="/", local_path=str(tmp_path)),
        ],
    )
    result = await toolkit.execute(
        context=_tool_context(),
        name="read_file",
        input=JsonContent(
            json={
                "path": "/webmaster.json",
                "offset": 0,
            }
        ),
    )

    assert isinstance(result, TextContent)
    assert result.text == content


@pytest.mark.asyncio
async def test_read_file_trims_storage_extension_when_mime_is_unknown(
    tmp_path, monkeypatch
) -> None:
    content = "name: webmaster\n"
    file_path = tmp_path / "webmaster.yaml"
    file_path.write_text(content, encoding="utf-8")

    monkeypatch.setattr(
        storage_toolkit.mimetypes,
        "guess_type",
        lambda _path: (None, None),
    )

    toolkit = StorageToolkit(
        read_only=True,
        max_length=500,
        mounts=[
            StorageToolLocalMount(path="/", local_path=str(tmp_path)),
        ],
    )
    result = await toolkit.execute(
        context=_tool_context(),
        name="read_file",
        input=JsonContent(
            json={
                "path": "/webmaster.yaml ",
                "offset": 0,
            }
        ),
    )

    assert isinstance(result, TextContent)
    assert result.text == content


@pytest.mark.asyncio
async def test_grep_file_uses_offset(tmp_path) -> None:
    content = "\n".join(
        [
            "zero",
            "one",
            "two target",
            "three",
            "four target",
            "five",
        ]
    )
    file_path = tmp_path / "sample.txt"
    file_path.write_text(content, encoding="utf-8")

    toolkit = StorageToolkit(
        read_only=True,
        max_length=500,
        mounts=[
            StorageToolLocalMount(path="/", local_path=str(tmp_path)),
        ],
    )
    offset = content.index("four target")
    result = await toolkit.execute(
        context=_tool_context(),
        name="grep_file",
        input=JsonContent(
            json={
                "path": "/sample.txt",
                "pattern": "target",
                "offset": offset,
                "before": 1,
                "after": 1,
            }
        ),
    )

    assert isinstance(result, TextContent)
    assert result.text == grep_text(
        text=content[offset:],
        pattern="target",
        start_line=content.count("\n", 0, offset) + 1,
        before=1,
        after=1,
    )


def test_grep_text_uses_python_splitlines_boundaries() -> None:
    text = "zero\rtarget one\x0bmid\x1ctarget two\x85tail\u2028last"

    assert grep_text(text=text, pattern="target", before=1, after=1) == (
        "1- zero\n2: target one\n3- mid\n4: target two\n5- tail"
    )


def test_grep_text_supports_python_lookaround_patterns() -> None:
    text = "one target\ntwo target\ntargetx\nTARGET\naxxxb\n"

    assert grep_text(text=text, pattern="(?=target)target") == (
        "1: one target\n2: two target\n3: targetx"
    )
    assert grep_text(text=text, pattern="(?<=two )target") == "2: two target"
    assert grep_text(text=text, pattern="target(?!x)") == (
        "1: one target\n2: two target"
    )
    assert grep_text(text=text, pattern="(?i)target") == (
        "1: one target\n2: two target\n3: targetx\n4: TARGET"
    )
    assert grep_text(text=text, pattern="(?m)^target") == "3: targetx"
    assert grep_text(text=text, pattern="(?s)a.*b") == "5: axxxb"
    assert grep_text(text=text, pattern="(?P<name>target)") == (
        "1: one target\n2: two target\n3: targetx"
    )


def test_grep_text_supports_python_backreference_patterns() -> None:
    text = "foo foo\nfoo bar\n123-123\nword WORD\n"

    assert grep_text(text=text, pattern=r"(\w+) \1") == "1: foo foo"
    assert grep_text(text=text, pattern=r"(\d+)-(\1)") == "3: 123-123"
    assert grep_text(text=text, pattern=r"(?i)(word) \1") == "4: word WORD"
    assert grep_text(text=text, pattern=r"(?P<x>foo) (?P=x)") == "1: foo foo"


def test_grep_text_invalid_regex_errors_match_python() -> None:
    cases = [
        (
            "[",
            "invalid regular expression pattern: unterminated character set at position 0",
        ),
        ("\\p{L}", "invalid regular expression pattern: bad escape \\p at position 0"),
        (
            "(?P<x>a)(?P<x>b)",
            "invalid regular expression pattern: redefinition of group name 'x' as group 2; was group 1 at position 12",
        ),
        (
            "(?<bad>target",
            "invalid regular expression pattern: unknown extension ?<b at position 1",
        ),
    ]
    for pattern, expected in cases:
        with pytest.raises(RoomException) as exc_info:
            grep_text(text="target\n", pattern=pattern)
        assert str(exc_info.value) == expected


@pytest.mark.asyncio
async def test_room_mount_write_file_uses_room_storage_upload() -> None:
    room = _FakeRoom()
    toolkit = StorageToolkit(
        mounts=[
            StorageToolRoomMount(path="/", room=room),
        ],
    )

    result = await toolkit.execute(
        context=ToolContext(caller=object()),
        name="write_file",
        input=JsonContent(
            json={
                "path": "/rules.txt",
                "text": "hello from toolkit",
                "overwrite": True,
            }
        ),
    )

    assert isinstance(result, TextContent)
    assert room.storage.upload_calls == [
        {
            "path": "rules.txt",
            "data": b"hello from toolkit",
            "overwrite": True,
        }
    ]


@pytest.mark.asyncio
async def test_write_file_rejects_selected_read_only_mount() -> None:
    toolkit = StorageToolkit(
        read_only=False,
        mounts=[
            StorageToolLocalMount(path="/readonly", local_path="/tmp", read_only=True),
            StorageToolLocalMount(path="/writable", local_path="/tmp"),
        ],
    )

    with pytest.raises(
        RoomException, match="storage mount is read-only: /readonly/file.txt"
    ):
        await toolkit.execute(
            context=ToolContext(caller=object()),
            name="write_file",
            input=JsonContent(
                json={
                    "path": "/readonly/file.txt",
                    "text": "hello",
                    "overwrite": False,
                }
            ),
        )


@pytest.mark.asyncio
async def test_list_files_uses_selected_read_only_mount(tmp_path) -> None:
    readonly_root = tmp_path / "readonly"
    writable_root = tmp_path / "writable"
    readonly_root.mkdir()
    writable_root.mkdir()
    (readonly_root / "readable.txt").write_text("hello", encoding="utf-8")
    (writable_root / "other.txt").write_text("ignored", encoding="utf-8")

    toolkit = StorageToolkit(
        read_only=False,
        mounts=[
            StorageToolLocalMount(
                path="/readonly", local_path=str(readonly_root), read_only=True
            ),
            StorageToolLocalMount(path="/writable", local_path=str(writable_root)),
        ],
    )

    result = await toolkit.execute(
        context=ToolContext(caller=object()),
        name="list_files_in_room",
        input=JsonContent(json={"path": "/readonly"}),
    )

    assert isinstance(result, JsonContent)
    assert [entry["name"] for entry in result.json["files"]] == ["readable.txt"]


@pytest.mark.asyncio
async def test_read_file_uses_selected_read_only_mount(tmp_path) -> None:
    readonly_root = tmp_path / "readonly"
    writable_root = tmp_path / "writable"
    readonly_root.mkdir()
    writable_root.mkdir()
    (readonly_root / "readable.txt").write_text("hello from readonly", encoding="utf-8")
    (writable_root / "readable.txt").write_text("wrong mount", encoding="utf-8")

    toolkit = StorageToolkit(
        read_only=False,
        mounts=[
            StorageToolLocalMount(
                path="/readonly", local_path=str(readonly_root), read_only=True
            ),
            StorageToolLocalMount(path="/writable", local_path=str(writable_root)),
        ],
    )

    result = await toolkit.execute(
        context=ToolContext(caller=object()),
        name="read_file",
        input=JsonContent(json={"path": "/readonly/readable.txt", "offset": 0}),
    )

    assert isinstance(result, TextContent)
    assert result.text == "hello from readonly"


@pytest.mark.asyncio
async def test_grep_file_uses_selected_read_only_mount(tmp_path) -> None:
    readonly_root = tmp_path / "readonly"
    writable_root = tmp_path / "writable"
    readonly_root.mkdir()
    writable_root.mkdir()
    (readonly_root / "notes.txt").write_text(
        "alpha\nneedle from readonly\nomega\n", encoding="utf-8"
    )
    (writable_root / "notes.txt").write_text("needle from writable\n", encoding="utf-8")

    toolkit = StorageToolkit(
        read_only=False,
        mounts=[
            StorageToolLocalMount(
                path="/readonly", local_path=str(readonly_root), read_only=True
            ),
            StorageToolLocalMount(path="/writable", local_path=str(writable_root)),
        ],
    )

    result = await toolkit.execute(
        context=ToolContext(caller=object()),
        name="grep_file",
        input=JsonContent(
            json={
                "path": "/readonly/notes.txt",
                "pattern": "needle",
                "offset": 0,
                "before": 0,
                "after": 0,
            }
        ),
    )

    assert isinstance(result, TextContent)
    assert result.text == "2: needle from readonly"


@pytest.mark.asyncio
async def test_get_download_url_uses_selected_read_only_mount(tmp_path) -> None:
    readonly_root = tmp_path / "readonly"
    writable_root = tmp_path / "writable"
    readonly_root.mkdir()
    writable_root.mkdir()
    (readonly_root / "download.txt").write_text("readonly file", encoding="utf-8")
    (writable_root / "download.txt").write_text("wrong mount", encoding="utf-8")

    toolkit = StorageToolkit(
        read_only=False,
        mounts=[
            StorageToolLocalMount(
                path="/readonly", local_path=str(readonly_root), read_only=True
            ),
            StorageToolLocalMount(path="/writable", local_path=str(writable_root)),
        ],
    )

    result = await toolkit.get_download_url(path="/readonly/download.txt")

    assert result.name == "download.txt"
    assert result.url.startswith("file://")
    assert result.url.endswith("/readonly/download.txt")


@pytest.mark.asyncio
async def test_standalone_get_file_download_url_uses_selected_read_only_mount(
    tmp_path,
) -> None:
    readonly_root = tmp_path / "readonly"
    writable_root = tmp_path / "writable"
    readonly_root.mkdir()
    writable_root.mkdir()
    (readonly_root / "download.txt").write_text("readonly file", encoding="utf-8")
    (writable_root / "download.txt").write_text("wrong mount", encoding="utf-8")
    tool = storage_toolkit.GetFileDownloadUrl(
        mounts=storage_toolkit._prepare_mounts(
            [
                StorageToolLocalMount(
                    path="/readonly", local_path=str(readonly_root), read_only=True
                ),
                StorageToolLocalMount(path="/writable", local_path=str(writable_root)),
            ]
        )
    )

    result = await tool.execute(
        context=ToolContext(caller=object()),
        path="/readonly/download.txt",
    )

    assert result.name == "download.txt"
    assert result.url.startswith("file://")
    assert result.url.endswith("/readonly/download.txt")


@pytest.mark.asyncio
async def test_room_mount_read_file_uses_room_storage_download() -> None:
    room = _FakeRoom()
    toolkit = StorageToolkit(
        mounts=[
            StorageToolRoomMount(path="/", room=room),
        ],
    )

    result = await toolkit.execute(
        context=ToolContext(caller=object()),
        name="read_file",
        input=JsonContent(
            json={
                "path": "/rules.txt",
                "offset": 0,
            }
        ),
    )

    assert isinstance(result, TextContent)
    assert result.text == "hello from room storage"
    assert room.storage.download_calls == ["rules.txt"]


def test_room_mount_stores_bound_room() -> None:
    room = _FakeRoom()
    room_mount = StorageToolRoomMount(path="/room", room=room)

    assert room_mount.room is room


@pytest.mark.asyncio
async def test_grep_file_treats_yaml_as_text_when_mime_is_unknown(
    tmp_path, monkeypatch
) -> None:
    content = "kind: Service\nmetadata:\n  name: webmaster\n"
    file_path = tmp_path / "webmaster.yaml"
    file_path.write_text(content, encoding="utf-8")

    monkeypatch.setattr(
        storage_toolkit.mimetypes,
        "guess_type",
        lambda _path: (None, None),
    )

    toolkit = StorageToolkit(
        read_only=True,
        max_length=500,
        mounts=[
            StorageToolLocalMount(path="/", local_path=str(tmp_path)),
        ],
    )
    result = await toolkit.execute(
        context=_tool_context(),
        name="grep_file",
        input=JsonContent(
            json={
                "path": "/webmaster.yaml",
                "pattern": "metadata",
                "offset": 0,
                "before": None,
                "after": None,
            }
        ),
    )

    assert isinstance(result, TextContent)
    assert "metadata:" in result.text


@pytest.mark.asyncio
async def test_grep_file_returns_guidance_for_pdf_and_images(tmp_path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n\x00\x01")
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")

    toolkit = StorageToolkit(
        read_only=True,
        mounts=[
            StorageToolLocalMount(path="/", local_path=str(tmp_path)),
        ],
    )
    pdf_result = await toolkit.execute(
        context=_tool_context(),
        name="grep_file",
        input=JsonContent(
            json={
                "path": "/sample.pdf",
                "pattern": "x",
                "offset": None,
                "before": None,
                "after": None,
            }
        ),
    )
    image_result = await toolkit.execute(
        context=_tool_context(),
        name="grep_file",
        input=JsonContent(
            json={
                "path": "/sample.png",
                "pattern": "x",
                "offset": None,
                "before": None,
                "after": None,
            }
        ),
    )

    assert isinstance(pdf_result, TextContent)
    assert pdf_result.text == (
        "grep_file does not support PDFs or images. Use read_file instead."
    )
    assert isinstance(image_result, TextContent)
    assert image_result.text == (
        "grep_file does not support PDFs or images. Use read_file instead."
    )


@pytest.mark.asyncio
async def test_grep_file_rejects_negative_context(tmp_path) -> None:
    content = "alpha\nbeta\ngamma"
    file_path = tmp_path / "sample.txt"
    file_path.write_text(content, encoding="utf-8")

    toolkit = StorageToolkit(
        read_only=True,
        mounts=[
            StorageToolLocalMount(path="/", local_path=str(tmp_path)),
        ],
    )

    with pytest.raises(RoomException, match="before must be a non-negative integer"):
        await toolkit.execute(
            context=_tool_context(),
            name="grep_file",
            input=JsonContent(
                json={
                    "path": "/sample.txt",
                    "pattern": "alpha",
                    "before": -1,
                }
            ),
        )


@pytest.mark.asyncio
async def test_web_fetch_supports_offset_and_truncation(monkeypatch) -> None:
    body = "header\n" + ("line\n" * 120)
    fake_response = _FakeResponse(
        data=body.encode("utf-8"),
        content_type="text/plain",
        charset="utf-8",
    )
    fake_session = _FakeSession(fake_response)
    monkeypatch.setattr(web_toolkit, "new_client_session", lambda: fake_session)

    toolkit = WebToolkit(max_length=72)
    result = await toolkit.execute(
        context=_tool_context(),
        name="web_fetch",
        input=JsonContent(
            json={
                "url": "https://example.com/docs.txt",
                "offset": 7,
            }
        ),
    )

    assert isinstance(result, TextContent)
    assert result.text == truncate_text(text=body, offset=7, max_length=72)


@pytest.mark.asyncio
async def test_web_fetch_and_grep_status_errors_before_decoding(monkeypatch) -> None:
    fake_response = _FakeResponse(
        data=b"not decoded",
        status=404,
        content_type="text/plain",
        charset="not-a-real-codec",
    )
    monkeypatch.setattr(
        web_toolkit, "new_client_session", lambda: _FakeSession(fake_response)
    )

    toolkit = WebToolkit(max_length=500)

    with pytest.raises(Exception, match="web fetch failed with status 404"):
        await toolkit.execute(
            context=_tool_context(),
            name="web_fetch",
            input=JsonContent(
                json={
                    "url": "https://example.com/missing.txt",
                    "offset": 0,
                }
            ),
        )

    with pytest.raises(Exception, match="web fetch failed with status 404"):
        await toolkit.execute(
            context=_tool_context(),
            name="web_grep",
            input=JsonContent(
                json={
                    "url": "https://example.com/missing.txt",
                    "pattern": "not",
                    "offset": 0,
                    "before": None,
                    "after": None,
                }
            ),
        )


@pytest.mark.asyncio
async def test_web_fetch_and_grep_decode_response_charset(monkeypatch) -> None:
    body = "café\nnaïve target\n"
    fake_response = _FakeResponse(
        data=body.encode("latin-1"),
        content_type="text/plain",
        charset="latin-1",
    )
    monkeypatch.setattr(
        web_toolkit, "new_client_session", lambda: _FakeSession(fake_response)
    )

    toolkit = WebToolkit(max_length=500)
    fetch_result = await toolkit.execute(
        context=_tool_context(),
        name="web_fetch",
        input=JsonContent(
            json={
                "url": "https://example.com/latin1.txt",
                "offset": 0,
            }
        ),
    )
    grep_result = await toolkit.execute(
        context=_tool_context(),
        name="web_grep",
        input=JsonContent(
            json={
                "url": "https://example.com/latin1.txt",
                "pattern": "naïve",
                "offset": 0,
                "before": 1,
                "after": None,
            }
        ),
    )

    assert isinstance(fetch_result, TextContent)
    assert fetch_result.text == body
    assert isinstance(grep_result, TextContent)
    assert grep_result.text == grep_text(
        text=body,
        pattern="naïve",
        start_line=1,
        before=1,
        after=0,
    )

    for charset in ("cp1252", "iso8859_1"):
        alias_response = _FakeResponse(
            data="café\n".encode(charset),
            content_type="text/plain",
            charset=charset,
        )
        monkeypatch.setattr(
            web_toolkit, "new_client_session", lambda: _FakeSession(alias_response)
        )
        alias_result = await toolkit.execute(
            context=_tool_context(),
            name="web_fetch",
            input=JsonContent(
                json={
                    "url": f"https://example.com/{charset}.txt",
                    "offset": 0,
                }
            ),
        )
        assert isinstance(alias_result, TextContent)
        assert alias_result.text == "café\n"

    for charset in ("ascii", "us-ascii"):
        ascii_response = _FakeResponse(
            data=b"caf\xe9\n",
            content_type="text/plain",
            charset=charset,
        )
        monkeypatch.setattr(
            web_toolkit, "new_client_session", lambda: _FakeSession(ascii_response)
        )
        ascii_result = await toolkit.execute(
            context=_tool_context(),
            name="web_fetch",
            input=JsonContent(
                json={
                    "url": f"https://example.com/{charset}.txt",
                    "offset": 0,
                }
            ),
        )
        assert isinstance(ascii_result, TextContent)
        assert ascii_result.text == "caf�\n"

    for charset, data in (
        ("utf-16", "café\n".encode("utf-16")),
        ("utf-16le", "café\n".encode("utf-16le")),
        ("utf-16be", "café\n".encode("utf-16be")),
        ("utf_16", "café\n".encode("utf-16")),
        ("utf_16_le", "café\n".encode("utf-16le")),
        ("utf_16_be", "café\n".encode("utf-16be")),
    ):
        utf16_response = _FakeResponse(
            data=data,
            content_type="text/plain",
            charset=charset,
        )
        monkeypatch.setattr(
            web_toolkit, "new_client_session", lambda: _FakeSession(utf16_response)
        )
        utf16_result = await toolkit.execute(
            context=_tool_context(),
            name="web_fetch",
            input=JsonContent(
                json={
                    "url": f"https://example.com/{charset}.txt",
                    "offset": 0,
                }
            ),
        )
        assert isinstance(utf16_result, TextContent)
        assert utf16_result.text == "café\n"

    odd_utf16_response = _FakeResponse(
        data=b"\xff",
        content_type="text/plain",
        charset="utf-16",
    )
    monkeypatch.setattr(
        web_toolkit, "new_client_session", lambda: _FakeSession(odd_utf16_response)
    )
    odd_utf16_result = await toolkit.execute(
        context=_tool_context(),
        name="web_fetch",
        input=JsonContent(
            json={
                "url": "https://example.com/odd-utf16.txt",
                "offset": 0,
            }
        ),
    )
    assert isinstance(odd_utf16_result, TextContent)
    assert odd_utf16_result.text == "�"

    unknown_charset_response = _FakeResponse(
        data=b"not decoded",
        content_type="text/plain",
        charset="not-a-real-codec",
    )
    monkeypatch.setattr(
        web_toolkit,
        "new_client_session",
        lambda: _FakeSession(unknown_charset_response),
    )
    with pytest.raises(LookupError, match="unknown encoding: not-a-real-codec"):
        await toolkit.execute(
            context=_tool_context(),
            name="web_fetch",
            input=JsonContent(
                json={
                    "url": "https://example.com/unknown-charset.txt",
                    "offset": 0,
                }
            ),
        )
    with pytest.raises(LookupError, match="unknown encoding: not-a-real-codec"):
        await toolkit.execute(
            context=_tool_context(),
            name="web_grep",
            input=JsonContent(
                json={
                    "url": "https://example.com/unknown-charset.txt",
                    "pattern": "decoded",
                    "offset": 0,
                    "before": None,
                    "after": None,
                }
            ),
        )


@pytest.mark.asyncio
async def test_web_tools_direct_execute_stringifies_url_and_pattern(
    monkeypatch,
) -> None:
    fake_response = _FakeResponse(
        data=b"123 target\n",
        content_type="text/plain",
        charset="utf-8",
    )
    fake_session = _FakeSession(fake_response)
    monkeypatch.setattr(web_toolkit, "new_client_session", lambda: fake_session)

    toolkit = WebToolkit(max_length=500)
    web_fetch = toolkit.get_tool("web_fetch")
    web_grep = toolkit.get_tool("web_grep")

    fetch_result = await web_fetch.execute(
        _tool_context(),
        url=123,
        offset=0,
    )
    grep_result = await web_grep.execute(
        _tool_context(),
        url=123,
        pattern=123,
        offset=0,
        before=None,
        after=None,
    )

    assert fake_session.requested_url == "123"
    assert isinstance(fetch_result, TextContent)
    assert fetch_result.text == "123 target\n"
    assert isinstance(grep_result, TextContent)
    assert grep_result.text == "1: 123 target"


@pytest.mark.asyncio
async def test_web_tools_empty_user_agent_falls_back_to_meshagent(monkeypatch) -> None:
    fake_response = _FakeResponse(
        data=b"ok\n",
        content_type="text/plain",
        charset="utf-8",
    )
    fetch_session = _FakeSession(fake_response)
    monkeypatch.setattr(web_toolkit, "new_client_session", lambda: fetch_session)

    toolkit = WebToolkit(user_agent="", max_length=500)
    await toolkit.execute(
        context=_tool_context(),
        name="web_fetch",
        input=JsonContent(
            json={
                "url": "https://example.com/fetch.txt",
                "offset": 0,
            }
        ),
    )
    assert fetch_session.requested_headers == {"User-Agent": "Meshagent"}

    grep_session = _FakeSession(fake_response)
    monkeypatch.setattr(web_toolkit, "new_client_session", lambda: grep_session)
    await toolkit.execute(
        context=_tool_context(),
        name="web_grep",
        input=JsonContent(
            json={
                "url": "https://example.com/grep.txt",
                "pattern": "ok",
                "offset": 0,
                "before": None,
                "after": None,
            }
        ),
    )
    assert grep_session.requested_headers == {"User-Agent": "Meshagent"}

    custom_session = _FakeSession(fake_response)
    monkeypatch.setattr(web_toolkit, "new_client_session", lambda: custom_session)
    custom_toolkit = WebToolkit(user_agent="custom-agent", max_length=500)
    await custom_toolkit.execute(
        context=_tool_context(),
        name="web_fetch",
        input=JsonContent(
            json={
                "url": "https://example.com/custom.txt",
                "offset": 0,
            }
        ),
    )
    assert custom_session.requested_headers == {"User-Agent": "custom-agent"}


@pytest.mark.asyncio
async def test_web_fetch_returns_pdf_file_content(monkeypatch) -> None:
    data = b"%PDF-1.7\n\x00\x01\x02binary"
    fake_response = _FakeResponse(
        data=data,
        content_type="application/pdf",
        charset="utf-8",
    )
    fake_session = _FakeSession(fake_response)
    monkeypatch.setattr(web_toolkit, "new_client_session", lambda: fake_session)

    toolkit = WebToolkit(max_length=2)
    result = await toolkit.execute(
        context=_tool_context(),
        name="web_fetch",
        input=JsonContent(
            json={
                "url": "https://example.com/file.pdf",
                "offset": 9999,
            }
        ),
    )

    assert isinstance(result, FileContent)
    assert result.mime_type == "application/pdf"
    assert result.data == data


@pytest.mark.asyncio
async def test_web_fetch_returns_image_file_content(monkeypatch) -> None:
    data = b"\x89PNG\r\n\x1a\n\x00\x00"
    fake_response = _FakeResponse(
        data=data,
        content_type="image/png",
        charset="utf-8",
    )
    fake_session = _FakeSession(fake_response)
    monkeypatch.setattr(web_toolkit, "new_client_session", lambda: fake_session)

    toolkit = WebToolkit(max_length=2)
    result = await toolkit.execute(
        context=_tool_context(),
        name="web_fetch",
        input=JsonContent(
            json={
                "url": "https://example.com/image.png",
                "offset": 9999,
            }
        ),
    )

    assert isinstance(result, FileContent)
    assert result.mime_type == "image/png"
    assert result.data == data


@pytest.mark.asyncio
async def test_web_fetch_treats_yaml_as_text_when_content_type_is_octet_stream(
    monkeypatch,
) -> None:
    body = "kind: Service\nmetadata:\n  name: webmaster\n"
    fake_response = _FakeResponse(
        data=body.encode("utf-8"),
        content_type="application/octet-stream",
        charset="utf-8",
    )
    fake_session = _FakeSession(fake_response)
    monkeypatch.setattr(web_toolkit, "new_client_session", lambda: fake_session)

    toolkit = WebToolkit(max_length=500)
    result = await toolkit.execute(
        context=_tool_context(),
        name="web_fetch",
        input=JsonContent(
            json={
                "url": "https://example.com/webmaster.yaml",
                "offset": 0,
            }
        ),
    )

    assert isinstance(result, TextContent)
    assert result.text == body


@pytest.mark.asyncio
async def test_web_fetch_and_grep_pretty_json_preserve_non_ascii(monkeypatch) -> None:
    body = '["café","😀","a\\nb"]'
    expected = '[\n  "café",\n  "😀",\n  "a\\nb"\n]'
    fake_response = _FakeResponse(
        data=body.encode("utf-8"),
        content_type="application/json",
        charset="utf-8",
    )
    monkeypatch.setattr(
        web_toolkit, "new_client_session", lambda: _FakeSession(fake_response)
    )

    toolkit = WebToolkit(max_length=500)
    fetch_result = await toolkit.execute(
        context=_tool_context(),
        name="web_fetch",
        input=JsonContent(
            json={
                "url": "https://example.com/data.json",
                "offset": 0,
            }
        ),
    )
    grep_result = await toolkit.execute(
        context=_tool_context(),
        name="web_grep",
        input=JsonContent(
            json={
                "url": "https://example.com/data.json",
                "pattern": "café",
                "offset": 0,
                "before": 1,
                "after": 1,
            }
        ),
    )

    assert isinstance(fetch_result, TextContent)
    assert fetch_result.text == expected
    assert isinstance(grep_result, TextContent)
    assert grep_result.text == '1- [\n2:   "café",\n3-   "😀",'


@pytest.mark.asyncio
async def test_web_fetch_treats_json_as_text_when_content_type_is_octet_stream(
    monkeypatch,
) -> None:
    body = '{"kind":"Service","metadata":{"name":"webmaster"}}\n'
    fake_response = _FakeResponse(
        data=body.encode("utf-8"),
        content_type="application/octet-stream",
        charset="utf-8",
    )
    fake_session = _FakeSession(fake_response)
    monkeypatch.setattr(web_toolkit, "new_client_session", lambda: fake_session)

    toolkit = WebToolkit(max_length=500)
    result = await toolkit.execute(
        context=_tool_context(),
        name="web_fetch",
        input=JsonContent(
            json={
                "url": "https://example.com/webmaster.json",
                "offset": 0,
            }
        ),
    )

    assert isinstance(result, TextContent)
    assert result.text == body


@pytest.mark.asyncio
async def test_web_grep_uses_offset(monkeypatch) -> None:
    body = "\n".join(
        [
            "zero",
            "one",
            "two target",
            "three",
            "four target",
            "five",
        ]
    )
    fake_response = _FakeResponse(
        data=body.encode("utf-8"),
        content_type="text/plain",
        charset="utf-8",
    )
    monkeypatch.setattr(
        web_toolkit, "new_client_session", lambda: _FakeSession(fake_response)
    )

    toolkit = WebToolkit(max_length=500)
    offset = body.index("four target")
    result = await toolkit.execute(
        context=_tool_context(),
        name="web_grep",
        input=JsonContent(
            json={
                "url": "https://example.com/docs.txt",
                "pattern": "target",
                "offset": offset,
                "before": 1,
                "after": 1,
            }
        ),
    )

    assert isinstance(result, TextContent)
    assert result.text == grep_text(
        text=body[offset:],
        pattern="target",
        start_line=body.count("\n", 0, offset) + 1,
        before=1,
        after=1,
    )


@pytest.mark.asyncio
async def test_web_grep_returns_guidance_for_pdf_and_images(monkeypatch) -> None:
    pdf_response = _FakeResponse(
        data=b"%PDF-1.7\n\x00\x01",
        content_type="application/pdf",
        charset="utf-8",
    )
    image_response = _FakeResponse(
        data=b"\x89PNG\r\n\x1a\n\x00\x00",
        content_type="image/png",
        charset="utf-8",
    )

    monkeypatch.setattr(
        web_toolkit, "new_client_session", lambda: _FakeSession(pdf_response)
    )
    toolkit = WebToolkit(max_length=500)
    pdf_result = await toolkit.execute(
        context=_tool_context(),
        name="web_grep",
        input=JsonContent(
            json={
                "url": "https://example.com/file.pdf",
                "pattern": "target",
                "offset": None,
                "before": None,
                "after": None,
            }
        ),
    )
    monkeypatch.setattr(
        web_toolkit, "new_client_session", lambda: _FakeSession(image_response)
    )
    image_result = await toolkit.execute(
        context=_tool_context(),
        name="web_grep",
        input=JsonContent(
            json={
                "url": "https://example.com/image.png",
                "pattern": "target",
                "offset": None,
                "before": None,
                "after": None,
            }
        ),
    )

    assert isinstance(pdf_result, TextContent)
    assert pdf_result.text == (
        "web_grep does not support PDFs or images. Use web_fetch instead."
    )
    assert isinstance(image_result, TextContent)
    assert image_result.text == (
        "web_grep does not support PDFs or images. Use web_fetch instead."
    )


@pytest.mark.asyncio
async def test_web_grep_treats_yaml_as_text_when_content_type_is_octet_stream(
    monkeypatch,
) -> None:
    body = "kind: Service\nmetadata:\n  name: webmaster\n"
    fake_response = _FakeResponse(
        data=body.encode("utf-8"),
        content_type="application/octet-stream",
        charset="utf-8",
    )

    monkeypatch.setattr(
        web_toolkit, "new_client_session", lambda: _FakeSession(fake_response)
    )
    toolkit = WebToolkit(max_length=500)
    result = await toolkit.execute(
        context=_tool_context(),
        name="web_grep",
        input=JsonContent(
            json={
                "url": "https://example.com/webmaster.yaml",
                "pattern": "metadata",
                "offset": 0,
                "before": None,
                "after": None,
            }
        ),
    )

    assert isinstance(result, TextContent)
    assert "metadata:" in result.text


def test_toolkits_expose_grep_tools() -> None:
    storage_toolkit = StorageToolkit(
        read_only=True,
        mounts=[StorageToolLocalMount(path="/", local_path="/tmp")],
    )
    web_toolkit_instance = WebToolkit()

    storage_tool_names = [tool.name for tool in storage_toolkit.tools]
    web_tool_names = [tool.name for tool in web_toolkit_instance.tools]

    assert "grep_file" in storage_tool_names
    assert "web_grep" in web_tool_names


def test_web_toolkit_max_length_validation_matches_python() -> None:
    for constructor in (WebToolkit, WebFetchTool, WebGrepTool):
        with pytest.raises(ValueError, match="max_length must be greater than 0"):
            constructor(max_length=0)
        with pytest.raises(ValueError, match="max_length must be greater than 0"):
            constructor(max_length=-1)
        with pytest.raises(ValueError, match="max_length must be an integer"):
            constructor(max_length=True)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="max_length must be an integer"):
            constructor(max_length=1.5)  # type: ignore[arg-type]


def test_storage_toolkit_omits_write_tools_when_all_mounts_are_read_only() -> None:
    storage_toolkit = StorageToolkit(
        read_only=False,
        mounts=[StorageToolLocalMount(path="/", local_path="/tmp", read_only=True)],
    )

    assert [tool.name for tool in storage_toolkit.tools] == [
        "list_files_in_room",
        "read_file",
        "grep_file",
    ]


@pytest.mark.asyncio
async def test_storage_toolkit_read_only_direct_writes_fail_before_path_resolution() -> (
    None
):
    storage_toolkit = StorageToolkit(
        read_only=True,
        mounts=[StorageToolLocalMount(path="/", local_path="/tmp")],
    )

    with pytest.raises(RoomException, match="storage toolkit is read-only: ../bad.txt"):
        await storage_toolkit.write_text(
            path="../bad.txt", text="hello", overwrite=True
        )

    with pytest.raises(RoomException, match="storage toolkit is read-only: ../bad.bin"):
        await storage_toolkit.write_bytes(
            path="../bad.bin", data=b"hello", overwrite=True
        )

    with pytest.raises(RoomException, match="storage toolkit is read-only: ../bad.txt"):
        await storage_toolkit.delete(path="../bad.txt")


@pytest.mark.asyncio
async def test_storage_toolkit_direct_writes_reject_selected_read_only_mount() -> None:
    toolkit = StorageToolkit(
        read_only=False,
        mounts=[
            StorageToolLocalMount(path="/readonly", local_path="/tmp", read_only=True),
            StorageToolLocalMount(path="/writable", local_path="/tmp"),
        ],
    )

    with pytest.raises(
        RoomException, match="storage mount is read-only: /readonly/file.txt"
    ):
        await toolkit.write_text(
            path="/readonly/file.txt", text="hello", overwrite=True
        )

    with pytest.raises(
        RoomException, match="storage mount is read-only: /readonly/file.bin"
    ):
        await toolkit.write_bytes(
            path="/readonly/file.bin", data=b"hello", overwrite=True
        )

    with pytest.raises(
        RoomException, match="storage mount is read-only: /readonly/file.txt"
    ):
        await toolkit.delete(path="/readonly/file.txt")


@pytest.mark.asyncio
async def test_storage_toolkit_direct_writes_use_selected_writable_mount(
    tmp_path,
) -> None:
    readonly_root = tmp_path / "readonly"
    writable_root = tmp_path / "writable"
    readonly_root.mkdir()
    writable_root.mkdir()
    (writable_root / "delete-me.txt").write_text("old", encoding="utf-8")

    toolkit = StorageToolkit(
        read_only=False,
        mounts=[
            StorageToolLocalMount(
                path="/readonly", local_path=str(readonly_root), read_only=True
            ),
            StorageToolLocalMount(path="/writable", local_path=str(writable_root)),
        ],
    )

    await toolkit.write_text(path="/writable/text.txt", text="hello", overwrite=False)
    await toolkit.write_bytes(path="/writable/blob.bin", data=b"bytes", overwrite=False)
    await toolkit.delete(path="/writable/delete-me.txt")

    assert (writable_root / "text.txt").read_text(encoding="utf-8") == "hello"
    assert (writable_root / "blob.bin").read_bytes() == b"bytes"
    assert not (writable_root / "delete-me.txt").exists()
    assert list(readonly_root.iterdir()) == []


@pytest.mark.asyncio
async def test_save_file_from_url_resolves_path_before_fetching(monkeypatch) -> None:
    fetched_urls: list[str] = []

    async def fake_get_bytes_from_url(*, url: str):
        fetched_urls.append(url)
        raise AssertionError("fetch should not be called for an invalid storage path")

    monkeypatch.setattr(storage_toolkit, "get_bytes_from_url", fake_get_bytes_from_url)

    toolkit = StorageToolkit(
        read_only=False,
        mounts=[StorageToolLocalMount(path="/", local_path="/tmp")],
    )

    with pytest.raises(RoomException, match="dot segments not allowed: ../bad.bin"):
        await toolkit.execute(
            context=ToolContext(caller=object()),
            name="save_file_from_url",
            input=JsonContent(
                json={
                    "url": "https://example.com/file.bin",
                    "path": "../bad.bin",
                    "overwrite": False,
                }
            ),
        )

    assert fetched_urls == []


@pytest.mark.asyncio
async def test_save_file_from_url_fetches_before_read_only_mount_error(
    monkeypatch,
) -> None:
    fetched_urls: list[str] = []

    async def fake_get_bytes_from_url(*, url: str):
        fetched_urls.append(url)
        return Blob(
            mime_type="application/octet-stream",
            data=b"downloaded",
        )

    monkeypatch.setattr(storage_toolkit, "get_bytes_from_url", fake_get_bytes_from_url)

    toolkit = StorageToolkit(
        read_only=False,
        mounts=[
            StorageToolLocalMount(path="/readonly", local_path="/tmp", read_only=True),
            StorageToolLocalMount(path="/writable", local_path="/tmp"),
        ],
    )

    with pytest.raises(
        RoomException, match="storage mount is read-only: /readonly/file.bin"
    ):
        await toolkit.execute(
            context=ToolContext(caller=object()),
            name="save_file_from_url",
            input=JsonContent(
                json={
                    "url": "https://example.com/file.bin",
                    "path": "/readonly/file.bin",
                    "overwrite": False,
                }
            ),
        )

    assert fetched_urls == ["https://example.com/file.bin"]


def test_updated_function_tool_schemas_are_strict() -> None:
    storage_toolkit = StorageToolkit(
        read_only=True,
        mounts=[StorageToolLocalMount(path="/", local_path="/tmp")],
    )
    web_toolkit_instance = WebToolkit()

    read_file_tool = storage_toolkit.get_tool("read_file")
    grep_file_tool = storage_toolkit.get_tool("grep_file")
    web_fetch_tool = web_toolkit_instance.get_tool("web_fetch")
    web_grep_tool = web_toolkit_instance.get_tool("web_grep")

    assert set(read_file_tool.input_schema["required"]) == {"path", "offset"}
    assert set(grep_file_tool.input_schema["required"]) == {
        "path",
        "pattern",
        "offset",
        "before",
        "after",
    }
    assert set(web_fetch_tool.input_schema["required"]) == {"url", "offset"}
    assert set(web_grep_tool.input_schema["required"]) == {
        "url",
        "pattern",
        "offset",
        "before",
        "after",
    }
