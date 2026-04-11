from __future__ import annotations

import json
import mimetypes
import os
from urllib.parse import urlparse
from typing import Optional
from meshagent.api.http import new_client_session
from meshagent.api.messaging import FileContent, Content, TextContent
from meshagent.tools.config import ToolkitConfig
from meshagent.tools.tool import FunctionTool, ToolContext
from meshagent.tools.toolkit import Toolkit, ToolkitBuilder
from ._text_utils import (
    DEFAULT_TOOL_MAX_LENGTH,
    grep_text,
    normalize_context_lines,
    normalize_offset,
    truncate_text,
    validate_max_length,
)


class WebToolkit(Toolkit):
    def __init__(
        self,
        *,
        user_agent: Optional[str] = None,
        max_length: int = DEFAULT_TOOL_MAX_LENGTH,
    ):
        validated_max_length = validate_max_length(
            max_length=max_length, tool_name="web_fetch"
        )
        super().__init__(
            name="web_fetch",
            tools=[
                WebFetchTool(user_agent=user_agent, max_length=validated_max_length),
                WebGrepTool(user_agent=user_agent, max_length=validated_max_length),
            ],
        )


class WebFetchTool(FunctionTool):
    def __init__(
        self,
        *,
        user_agent: Optional[str] = None,
        max_length: int = DEFAULT_TOOL_MAX_LENGTH,
    ):
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
                    },
                    "offset": {
                        "type": ["integer", "null"],
                        "description": "optional character offset into the fetched text output",
                    },
                },
                "required": ["url", "offset"],
                "additionalProperties": False,
            },
        )
        self.user_agent = user_agent
        self.max_length = validate_max_length(
            max_length=max_length, tool_name="web_fetch"
        )

    async def execute(self, context: ToolContext, **kwargs: object) -> Content:
        url = str(kwargs.get("url", ""))
        if not url:
            raise ValueError("url is required")
        offset = normalize_offset(value=kwargs.get("offset"))

        async with new_client_session() as session:
            async with session.get(
                url,
                headers={
                    "User-Agent": self.user_agent or "Meshagent",
                },
            ) as resp:
                if resp.status >= 400:
                    raise Exception(f"web fetch failed with status {resp.status}")

                content_type = (resp.content_type or "").lower()
                data = await resp.read()

                if _is_json_content_type(content_type):
                    text = _decode_text(data=data, charset=resp.charset)
                    try:
                        parsed = json.loads(text)
                    except json.JSONDecodeError:
                        return TextContent(
                            text=truncate_text(
                                text=text,
                                offset=offset,
                                max_length=self.max_length,
                            )
                        )

                    return TextContent(
                        text=truncate_text(
                            text=json.dumps(parsed, ensure_ascii=False, indent=2),
                            offset=offset,
                            max_length=self.max_length,
                        )
                    )

                if _is_text_content_type(content_type) or _is_text_like_url(
                    url=url, content_type=content_type
                ):
                    text = _decode_text(data=data, charset=resp.charset)
                    if content_type == "text/html":
                        from html_to_markdown import convert

                        text = convert(text)
                    return TextContent(
                        text=truncate_text(
                            text=text,
                            offset=offset,
                            max_length=self.max_length,
                        )
                    )

                if _is_file_content_type(content_type) or _is_pdf_or_image_url(
                    url=url, content_type=content_type
                ):
                    filename = _infer_filename(url=url, content_type=content_type)
                    return FileContent(
                        name=filename,
                        mime_type=content_type or "application/octet-stream",
                        data=data,
                    )

                filename = _infer_filename(url=url, content_type=content_type)
                return FileContent(
                    name=filename,
                    mime_type=content_type or "application/octet-stream",
                    data=data,
                )


class WebGrepTool(FunctionTool):
    def __init__(
        self,
        *,
        user_agent: Optional[str] = None,
        max_length: int = DEFAULT_TOOL_MAX_LENGTH,
    ):
        super().__init__(
            name="web_grep",
            title="web grep",
            description="fetches a url and searches its text output using a regular expression pattern. PDFs and images are not supported; use web_fetch instead.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "the url of the web page (always start it with a proper scheme like https://)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "regular expression pattern used to match fetched lines",
                    },
                    "offset": {
                        "type": ["integer", "null"],
                        "description": "optional character offset into the fetched text output",
                    },
                    "before": {
                        "type": ["integer", "null"],
                        "description": "optional number of context lines to include before each match",
                    },
                    "after": {
                        "type": ["integer", "null"],
                        "description": "optional number of context lines to include after each match",
                    },
                },
                "required": ["url", "pattern", "offset", "before", "after"],
                "additionalProperties": False,
            },
        )
        self.user_agent = user_agent
        self.max_length = validate_max_length(
            max_length=max_length, tool_name="web_grep"
        )

    async def execute(self, context: ToolContext, **kwargs: object) -> Content:
        url = str(kwargs.get("url", ""))
        pattern = str(kwargs.get("pattern", ""))
        if not url:
            raise ValueError("url is required")
        offset = normalize_offset(value=kwargs.get("offset"))
        before = normalize_context_lines(
            value=kwargs.get("before"), parameter_name="before"
        )
        after = normalize_context_lines(
            value=kwargs.get("after"), parameter_name="after"
        )

        async with new_client_session() as session:
            async with session.get(
                url,
                headers={
                    "User-Agent": self.user_agent or "Meshagent",
                },
            ) as resp:
                if resp.status >= 400:
                    raise Exception(f"web fetch failed with status {resp.status}")

                content_type = (resp.content_type or "").lower()
                data = await resp.read()
                if _is_file_content_type(content_type) or _is_pdf_or_image_url(
                    url=url, content_type=content_type
                ):
                    return TextContent(
                        text="web_grep does not support PDFs or images. Use web_fetch instead."
                    )
                text = _decode_text(data=data, charset=resp.charset)
                if content_type == "text/html":
                    from html_to_markdown import convert

                    text = convert(text)
                elif _is_json_content_type(content_type):
                    try:
                        text = json.dumps(
                            json.loads(text), ensure_ascii=False, indent=2
                        )
                    except json.JSONDecodeError:
                        pass

                if offset >= len(text):
                    return TextContent(text="No matches found.")

                line_offset = text.count("\n", 0, offset)
                matches = grep_text(
                    text=text[offset:],
                    pattern=pattern,
                    start_line=line_offset + 1,
                    before=before,
                    after=after,
                )
                return TextContent(
                    text=truncate_text(
                        text=matches,
                        offset=0,
                        max_length=self.max_length,
                    )
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
        "application/yaml",
        "application/x-yaml",
        "text/yaml",
        "text/x-yaml",
        "application/yml",
        "application/x-yml",
        "text/yml",
        "text/x-yml",
    }


def _is_file_content_type(content_type: str) -> bool:
    if content_type.startswith("image/"):
        return True
    return content_type == "application/pdf"


def _url_extension(url: str) -> str:
    parsed = urlparse(url)
    return os.path.splitext(parsed.path)[1].strip().lower()


def _is_text_like_url(*, url: str, content_type: str) -> bool:
    # If the server provides a known text-like type, defer to that immediately.
    if _is_text_content_type(content_type):
        return True

    # Some raw file hosts return application/octet-stream for YAML files.
    if content_type not in {
        "",
        "application/octet-stream",
        "binary/octet-stream",
    }:
        return False

    return _url_extension(url) in {
        ".yaml",
        ".yml",
        ".json",
        ".jsonl",
        ".ndjson",
        ".geojson",
    }


def _is_pdf_or_image_url(*, url: str, content_type: str) -> bool:
    if _is_file_content_type(content_type):
        return True

    extension = _url_extension(url)
    if extension == ".pdf":
        return True
    return extension in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".svg",
        ".avif",
        ".heic",
        ".heif",
    }


def _infer_filename(*, url: str, content_type: str) -> str:
    parsed = urlparse(url)
    basename = os.path.basename(parsed.path)
    if basename:
        return basename
    extension = mimetypes.guess_extension(content_type or "") or ""
    return f"downloaded-content{extension}"


class WebFetchConfig(ToolkitConfig):
    name: str = "web_fetch"
    user_agent: str = "Meshagent"
    max_length: int = DEFAULT_TOOL_MAX_LENGTH


class WebFetchToolkitBuilder(ToolkitBuilder):
    def __init__(self):
        super().__init__(name="web_fetch", type=WebFetchConfig)

    async def make(self, *, model: str, config: WebFetchConfig) -> Toolkit:
        del model
        return WebToolkit(user_agent=config.user_agent, max_length=config.max_length)
