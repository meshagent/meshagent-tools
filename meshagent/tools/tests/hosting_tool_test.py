from collections.abc import AsyncIterable

import pytest

from meshagent.api import RemoteParticipant
from meshagent.api.messaging import Content, TextContent, _ControlContent
from meshagent.tools import ContentTool, ToolContext, Toolkit
from meshagent.tools.hosting import stream_tool_call


class _StreamingContentTool(ContentTool):
    def __init__(self, *, chunks: list[Content]) -> None:
        super().__init__(name="stream_text")
        self._chunks = chunks

    async def execute(
        self,
        *,
        context: ToolContext,
        input: AsyncIterable[Content] | Content,
    ) -> AsyncIterable[Content] | Content:
        del context, input

        async def stream() -> AsyncIterable[Content]:
            for chunk in self._chunks:
                yield chunk

        return stream()


@pytest.mark.asyncio
async def test_stream_tool_call_continues_after_send_chunk_error() -> None:
    toolkit = Toolkit(
        name="remote",
        tools=[
            _StreamingContentTool(
                chunks=[TextContent(text="one"), TextContent(text="two")]
            )
        ],
        validation_mode="none",
    )
    responses: list[Content] = []
    chunks: list[Content] = []
    send_attempts = 0

    async def send_response(content: Content) -> None:
        responses.append(content)

    async def send_chunk(content: Content) -> None:
        nonlocal send_attempts
        send_attempts += 1
        if send_attempts == 1:
            raise RuntimeError("first chunk send failed")
        chunks.append(content)

    await stream_tool_call(
        toolkit=toolkit,
        validation_mode="none",
        room=None,
        caller=RemoteParticipant(id="caller-1"),
        on_behalf_of=None,
        name="stream_text",
        input=TextContent(text="input"),
        send_response=send_response,
        send_chunk=send_chunk,
    )

    assert len(responses) == 1
    assert isinstance(responses[0], _ControlContent)
    assert responses[0].method == "open"
    assert isinstance(chunks[0], TextContent)
    assert chunks[0].text == "two"
    assert isinstance(chunks[1], _ControlContent)
    assert chunks[1].method == "close"
    assert send_attempts == 3
