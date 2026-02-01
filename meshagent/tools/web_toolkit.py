from __future__ import annotations

import json
import mimetypes
import os
from urllib.parse import urlparse

from meshagent.api.http import new_client_session
from meshagent.api.messaging import FileResponse, JsonResponse, Response, TextResponse
from meshagent.tools.config import ToolkitConfig
from meshagent.tools.tool import Tool, ToolContext
from meshagent.tools.toolkit import Toolkit, ToolkitBuilder
from meshagent.api.room_server_client import RoomClient


class WebToolkit(Toolkit):
    def __init__(self):
        super().__init__(name="web_fetch", tools=[WebFetchTool()])


class WebFetchTool(Tool):
    def __init__(self):
        super().__init__(
            name="web_fetch",
            title="web fetch",
            description="fetches a url and returns text, json, or file content",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "the url of the web page (always start it with a proper scheme like https://)",
                    }
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        )

    async def execute(self, context: ToolContext, **kwargs: object) -> Response:
        url = str(kwargs.get("url", ""))
        if not url:
            raise ValueError("url is required")
        async with new_client_session() as session:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise Exception(f"web fetch failed with status {resp.status}")

                content_type = (resp.content_type or "").lower()
                data = await resp.read()

                if _is_json_content_type(content_type):
                    text = _decode_text(data=data, charset=resp.charset)
                    try:
                        parsed = json.loads(text)
                    except json.JSONDecodeError:
                        return TextResponse(text=text)

                    if isinstance(parsed, dict):
                        return JsonResponse(json=parsed)
                    return JsonResponse(json={"data": parsed})

                if _is_text_content_type(content_type):
                    text = _decode_text(data=data, charset=resp.charset)
                    if content_type == "text/html":
                        from html_to_markdown import convert

                        text = convert(text)
                    return TextResponse(text=text)

                if _is_file_content_type(content_type):
                    filename = _infer_filename(url=url, content_type=content_type)
                    return FileResponse(
                        name=filename,
                        mime_type=content_type or "application/octet-stream",
                        data=data,
                    )

                filename = _infer_filename(url=url, content_type=content_type)
                return FileResponse(
                    name=filename,
                    mime_type=content_type or "application/octet-stream",
                    data=data,
                )


def _decode_text(*, data: bytes, charset: str | None) -> str:
    encoding = charset or "utf-8"
    return data.decode(encoding, errors="replace")


def _is_json_content_type(content_type: str) -> bool:
    if content_type in {"application/json", "text/json"}:
        return True
    return content_type.endswith("+json")


def _is_text_content_type(content_type: str) -> bool:
    if content_type.startswith("text/"):
        return True
    return content_type in {
        "application/xml",
        "application/xhtml+xml",
        "application/javascript",
        "application/x-javascript",
    }


def _is_file_content_type(content_type: str) -> bool:
    if content_type.startswith("image/"):
        return True
    return content_type == "application/pdf"


def _infer_filename(*, url: str, content_type: str) -> str:
    parsed = urlparse(url)
    basename = os.path.basename(parsed.path)
    if basename:
        return basename
    extension = mimetypes.guess_extension(content_type or "") or ""
    return f"downloaded-content{extension}"


class WebFetchConfig(ToolkitConfig):
    name: str = "web_fetch"


class WebFetchToolkitBuilder(ToolkitBuilder):
    def __init__(self):
        super().__init__(name="web_fetch", type=WebFetchConfig)

    async def make(
        self, *, room: RoomClient, model: str, config: WebFetchConfig
    ) -> Toolkit:
        return WebToolkit()
