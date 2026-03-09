from __future__ import annotations

import pytest

from meshagent.api import RoomException
from meshagent.api.messaging import FileContent, JsonContent, TextContent
from meshagent.tools import ToolContext
from meshagent.tools._text_utils import grep_text, truncate_text
from meshagent.tools.storage import (
    StorageToolLocalMount,
    StorageToolRoomMount,
    StorageToolkit,
)
from meshagent.tools.web_toolkit import WebToolkit
import meshagent.tools.web_toolkit as web_toolkit
import meshagent.tools.storage as storage_toolkit


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
    return ToolContext(room=object(), caller=object())


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


class _FakeRoom:
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
async def test_grep_file_supports_context_and_offset(tmp_path) -> None:
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


@pytest.mark.asyncio
async def test_room_mount_write_file_uses_room_storage_upload() -> None:
    room = _FakeRoom()
    toolkit = StorageToolkit(
        mounts=[
            StorageToolRoomMount(path="/"),
        ],
    )

    result = await toolkit.execute(
        context=ToolContext(room=room, caller=object()),
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
            StorageToolRoomMount(path="/"),
        ],
    )

    result = await toolkit.execute(
        context=ToolContext(room=room, caller=object()),
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
async def test_web_grep_supports_context_and_offset(monkeypatch) -> None:
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
    storage_toolkit = StorageToolkit(read_only=True)
    web_toolkit_instance = WebToolkit()

    storage_tool_names = [tool.name for tool in storage_toolkit.tools]
    web_tool_names = [tool.name for tool in web_toolkit_instance.tools]

    assert "grep_file" in storage_tool_names
    assert "web_grep" in web_tool_names


def test_updated_function_tool_schemas_are_strict() -> None:
    storage_toolkit = StorageToolkit(read_only=True)
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
