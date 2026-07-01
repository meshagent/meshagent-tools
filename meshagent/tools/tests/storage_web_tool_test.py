from __future__ import annotations

import pytest

from meshagent.api import RoomClient, RoomException
from meshagent.api.messaging import FileContent, JsonContent, TextContent
from meshagent.tools import ToolContext
from meshagent.tools._text_utils import grep_text, truncate_text
from meshagent.tools.blob import get_bytes_from_url
from meshagent.tools.storage import (
    StorageToolLocalMount,
    StorageToolRoomMount,
    StorageToolkit,
)
from meshagent.tools.web_toolkit import WebToolkit
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
