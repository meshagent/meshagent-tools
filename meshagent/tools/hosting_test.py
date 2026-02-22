import asyncio
from dataclasses import dataclass

import pytest

from meshagent.api.messaging import (
    JsonContent,
    TextContent,
    _ControlContent,
    pack_message,
    unpack_message,
    unpack_content,
    unpack_content_parts,
)
from meshagent.tools import StreamTool, tool
from meshagent.tools.hosting import RemoteToolkit


@tool()
async def stream_parts():
    yield {"step": 1}
    yield "final text"


@dataclass
class _SentMessage:
    typ: str
    data: bytes
    message_id: int


class _FakeProtocol:
    def __init__(self):
        self.sent: list[_SentMessage] = []
        self.response_sent = asyncio.Future()

    async def send(self, *, type: str, data: bytes, message_id: int) -> None:
        self.sent.append(_SentMessage(typ=type, data=data, message_id=message_id))
        if type == "agent.tool_call_response" and not self.response_sent.done():
            self.response_sent.set_result(True)


class _FakeMessaging:
    def __init__(self):
        self.remote_participants: list[object] = []


class _FakeRoom:
    def __init__(self):
        self.protocol = _FakeProtocol()
        self.messaging = _FakeMessaging()


class _CollectRequestChunksTool(StreamTool):
    def __init__(self):
        super().__init__(
            name="collect_request_chunks",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        )

    async def execute(
        self,
        *,
        context,
        request_stream,
    ):
        del context

        values: list[object] = []
        async for chunk in request_stream:
            if isinstance(chunk, JsonContent):
                values.append(chunk.json)
            elif isinstance(chunk, TextContent):
                values.append(chunk.text)

        return JsonContent(json={"values": values})


@pytest.mark.asyncio
async def test_remote_toolkit_stream_parts_are_normalized_as_chunks() -> None:
    room = _FakeRoom()
    toolkit = RemoteToolkit(name="test", tools=[stream_parts], public=True)
    toolkit._room = room  # type: ignore[assignment]

    await toolkit._tool_call(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=42,
        msg_type="agent.tool_call.test",
        data=pack_message(
            header={"name": "stream_parts", "arguments": {}, "caller_id": "caller-1"}
        ),
    )

    await asyncio.wait_for(room.protocol.response_sent, timeout=2.0)

    assert len(room.protocol.sent) == 4
    open_response = room.protocol.sent[0]
    assert open_response.typ == "agent.tool_call_response"
    open_chunk = unpack_content(open_response.data)
    assert isinstance(open_chunk, _ControlContent)
    assert open_chunk.method == "open"

    body_event = room.protocol.sent[1]
    assert body_event.typ == "agent.tool_call_response_chunk"
    body_header, body_payload = unpack_message(body_event.data)
    body_chunk = unpack_content_parts(header=body_header["chunk"], payload=body_payload)
    assert isinstance(body_chunk, JsonContent)
    assert body_chunk.json == {"step": 1}

    second_event = room.protocol.sent[2]
    assert second_event.typ == "agent.tool_call_response_chunk"
    second_header, second_payload = unpack_message(second_event.data)
    second_chunk = unpack_content_parts(
        header=second_header["chunk"], payload=second_payload
    )
    assert isinstance(second_chunk, TextContent)
    assert second_chunk.text == "final text"

    close_event = room.protocol.sent[3]
    assert close_event.typ == "agent.tool_call_response_chunk"
    close_header, close_payload = unpack_message(close_event.data)
    close_chunk = unpack_content_parts(
        header=close_header["chunk"], payload=close_payload
    )
    assert isinstance(close_chunk, _ControlContent)
    assert close_chunk.method == "close"


@pytest.mark.asyncio
async def test_remote_toolkit_forwards_request_stream_to_tool() -> None:
    room = _FakeRoom()
    toolkit = RemoteToolkit(
        name="test", tools=[_CollectRequestChunksTool()], public=True
    )
    toolkit._room = room  # type: ignore[assignment]

    await toolkit._tool_call(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=43,
        msg_type="agent.tool_call.test",
        data=pack_message(
            header={
                "name": "collect_request_chunks",
                "arguments": _ControlContent(method="open").to_json(),
                "caller_id": "caller-1",
                "tool_call_id": "tc-req-1",
            }
        ),
    )

    await toolkit._tool_call_request_chunk(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=43,
        msg_type="agent.tool_call_request_chunk.test",
        data=pack_message(
            header={
                "tool_call_id": "tc-req-1",
                "chunk": JsonContent(json={"step": 1}).to_json(),
            }
        ),
    )
    await toolkit._tool_call_request_chunk(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=43,
        msg_type="agent.tool_call_request_chunk.test",
        data=pack_message(
            header={
                "tool_call_id": "tc-req-1",
                "chunk": TextContent(text="done").to_json(),
            }
        ),
    )
    await toolkit._tool_call_request_chunk(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=43,
        msg_type="agent.tool_call_request_chunk.test",
        data=pack_message(
            header={
                "tool_call_id": "tc-req-1",
                "chunk": _ControlContent(method="close").to_json(),
            }
        ),
    )

    await asyncio.wait_for(room.protocol.response_sent, timeout=2.0)

    response_msg = room.protocol.sent[-1]
    response = unpack_content(response_msg.data)
    assert isinstance(response, JsonContent)
    assert response.json == {"values": [{"step": 1}, "done"]}
