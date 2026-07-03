import asyncio
from dataclasses import dataclass
from collections.abc import AsyncIterable
import logging
from typing import Callable

import pytest

from meshagent.api import ErrorCode
from meshagent.api.messaging import (
    Content,
    ControlCloseStatus,
    ErrorContent,
    JsonContent,
    TextContent,
    _ControlContent,
    pack_message,
    unpack_message,
    unpack_content,
    unpack_content_parts,
)
from meshagent.api.room_server_client import RoomException, ToolContentSpec
from meshagent.tools import ContentTool, FunctionTool, Toolkit, tool
from meshagent.tools.hosting import _RemoteToolkitWrapper


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
        self._handlers: dict[str, Callable] = {}

    async def send(self, *, type: str, data: bytes, message_id: int) -> None:
        self.sent.append(_SentMessage(typ=type, data=data, message_id=message_id))
        if type == "room.tool_call_response" and not self.response_sent.done():
            self.response_sent.set_result(True)

    def register_handler(self, typ: str, handler: Callable) -> None:
        self._handlers[typ] = handler

    def get_handler(self, typ: str) -> Callable | None:
        return self._handlers.get(typ)

    def unregister_handler(self, typ: str, handler: Callable) -> None:
        current_handler = self._handlers.get(typ)
        assert current_handler == handler
        self._handlers.pop(typ, None)


class _FakeMessaging:
    def __init__(self):
        self.remote_participants: list[object] = []


class _FakeRoom:
    def __init__(self):
        self.protocol = _FakeProtocol()
        self.messaging = _FakeMessaging()
        self.requests: list[tuple[str, dict]] = []
        self.is_closed = False
        self.is_connected = True
        self._events: dict[str, list[Callable]] = {}
        self._registration_count = 0

    def on(self, event_name: str, handler: Callable) -> None:
        self._events.setdefault(event_name, []).append(handler)

    def off(self, event_name: str, handler: Callable) -> None:
        handlers = self._events.get(event_name)
        if handlers is None:
            return
        if handler in handlers:
            handlers.remove(handler)
        if len(handlers) == 0:
            self._events.pop(event_name, None)

    def emit(self, event_name: str, **kwargs) -> None:
        for handler in list(self._events.get(event_name, [])):
            handler(**kwargs)

    async def send_request(self, typ: str, request: dict):
        self.requests.append((typ, request))
        if typ == "room.register_toolkit":
            self._registration_count += 1
            return {"id": f"registration-{self._registration_count}"}
        if typ == "room.unregister_toolkit":
            return {"ok": True}
        raise AssertionError(f"unexpected request: {typ}")


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 1.0, interval: float = 0.01
) -> None:
    async def wait_loop() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(wait_loop(), timeout=timeout)


class _CollectRequestChunksTool(ContentTool):
    def __init__(self):
        super().__init__(
            name="collect_request_chunks",
            input_schema={"oneOf": [{"type": "object"}, {"type": "string"}]},
        )

    async def execute(
        self,
        *,
        context,
        input,
    ):
        del context

        values: list[object] = []
        if isinstance(input, Content):

            async def single() -> AsyncIterable[Content]:
                yield input

            request_stream = single()
        else:
            request_stream = input

        async for chunk in request_stream:
            if isinstance(chunk, JsonContent):
                values.append(chunk.json)
            elif isinstance(chunk, TextContent):
                values.append(chunk.text)

        return JsonContent(json={"values": values})


class _WrongOutputTypeTool(ContentTool):
    def __init__(self):
        super().__init__(
            name="wrong_output_type",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
            output_spec=ToolContentSpec(types=["json"], stream=False),
        )

    async def execute(self, *, context, input):
        del context
        del input
        return TextContent(text="not-json")


class _SchemaValidatedTextEchoTool(ContentTool):
    def __init__(self):
        super().__init__(
            name="schema_validated_text_echo",
            input_schema={"type": "string", "pattern": "^ok$"},
            input_spec=ToolContentSpec(types=["text"], stream=False),
        )

    async def execute(self, *, context, input):
        del context
        if not isinstance(input, TextContent):
            raise Exception("expected text input")
        return JsonContent(json={"echo": input.text})


class _CollectValidatedTextStreamTool(ContentTool):
    def __init__(self):
        super().__init__(
            name="collect_validated_text_stream",
            input_schema={"type": "string", "pattern": "^ok"},
            input_spec=ToolContentSpec(types=["text"], stream=True),
        )

    async def execute(self, *, context, input):
        del context
        if isinstance(input, Content):
            raise Exception("expected stream input")
        values: list[str] = []
        async for item in input:
            if isinstance(item, TextContent):
                values.append(item.text)
        return JsonContent(json={"values": values})


class _InvalidStreamOutputTool(ContentTool):
    def __init__(self):
        super().__init__(
            name="invalid_stream_output",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
            output_spec=ToolContentSpec(types=["json"], stream=True),
        )

    async def execute(self, *, context, input):
        del context
        del input

        async def stream():
            yield JsonContent(json={"ok": 1})
            yield TextContent(text="invalid")

        return stream()


class _StrictToggleTool(FunctionTool):
    def __init__(self, *, name: str, strict: bool):
        super().__init__(
            name=name,
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": [],
            },
            strict=strict,
        )

    async def execute(self, context, **kwargs):
        del context
        del kwargs
        return JsonContent(json={"ok": True})


class _FailingTool(FunctionTool):
    def __init__(self):
        super().__init__(
            name="failing_tool",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": [],
            },
        )

    async def execute(self, context, **kwargs):
        del context
        del kwargs
        raise RoomException("messaging is already enabled")


class _GenericFailingTool(FunctionTool):
    def __init__(self):
        super().__init__(
            name="generic_failing_tool",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": [],
            },
        )

    async def execute(self, context, **kwargs):
        del context
        del kwargs
        raise RuntimeError("generic failure")


def _make_hosted_toolkit(
    *,
    tools: list[FunctionTool | ContentTool],
    validation_mode: str = "full",
) -> _RemoteToolkitWrapper:
    return _RemoteToolkitWrapper(
        toolkit=Toolkit(
            name="test",
            tools=tools,
            public=True,
            validation_mode=validation_mode,
        )
    )


@pytest.mark.asyncio
async def test_remote_toolkit_stream_parts_are_normalized_as_chunks() -> None:
    room = _FakeRoom()
    toolkit = _make_hosted_toolkit(tools=[stream_parts])
    toolkit._room = room  # type: ignore[assignment]

    await toolkit._tool_call(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=42,
        msg_type="room.tool_call.test",
        data=pack_message(
            header={"name": "stream_parts", "arguments": {}, "caller_id": "caller-1"}
        ),
    )

    await asyncio.wait_for(room.protocol.response_sent, timeout=2.0)

    assert len(room.protocol.sent) == 4
    open_response = room.protocol.sent[0]
    assert open_response.typ == "room.tool_call_response"
    open_chunk = unpack_content(open_response.data)
    assert isinstance(open_chunk, _ControlContent)
    assert open_chunk.method == "open"

    body_event = room.protocol.sent[1]
    assert body_event.typ == "room.tool_call_response_chunk"
    body_header, body_payload = unpack_message(body_event.data)
    body_chunk = unpack_content_parts(header=body_header["chunk"], payload=body_payload)
    assert isinstance(body_chunk, JsonContent)
    assert body_chunk.json == {"step": 1}

    second_event = room.protocol.sent[2]
    assert second_event.typ == "room.tool_call_response_chunk"
    second_header, second_payload = unpack_message(second_event.data)
    second_chunk = unpack_content_parts(
        header=second_header["chunk"], payload=second_payload
    )
    assert isinstance(second_chunk, TextContent)
    assert second_chunk.text == "final text"

    close_event = room.protocol.sent[3]
    assert close_event.typ == "room.tool_call_response_chunk"
    close_header, close_payload = unpack_message(close_event.data)
    close_chunk = unpack_content_parts(
        header=close_header["chunk"], payload=close_payload
    )
    assert isinstance(close_chunk, _ControlContent)
    assert close_chunk.method == "close"


@pytest.mark.asyncio
async def test_remote_toolkit_forwards_request_stream_to_tool() -> None:
    room = _FakeRoom()
    toolkit = _make_hosted_toolkit(tools=[_CollectRequestChunksTool()])
    toolkit._room = room  # type: ignore[assignment]

    await toolkit._tool_call(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=43,
        msg_type="room.tool_call.test",
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
        msg_type="room.tool_call_request_chunk.test",
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
        msg_type="room.tool_call_request_chunk.test",
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
        msg_type="room.tool_call_request_chunk.test",
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


@pytest.mark.asyncio
async def test_remote_toolkit_allows_non_stream_content_for_content_tool() -> None:
    room = _FakeRoom()
    toolkit = _make_hosted_toolkit(tools=[_CollectRequestChunksTool()])
    toolkit._room = room  # type: ignore[assignment]

    await toolkit._tool_call(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=44,
        msg_type="room.tool_call.test",
        data=pack_message(
            header={
                "name": "collect_request_chunks",
                "arguments": JsonContent(json={"step": 1}).to_json(),
                "caller_id": "caller-1",
                "tool_call_id": "tc-req-2",
            }
        ),
    )

    await asyncio.wait_for(room.protocol.response_sent, timeout=2.0)

    response_msg = room.protocol.sent[-1]
    response = unpack_content(response_msg.data)
    assert isinstance(response, JsonContent)
    assert response.json == {"values": [{"step": 1}]}


def _decode_chunk(message: _SentMessage) -> Content:
    header, payload = unpack_message(message.data)
    return unpack_content_parts(header=header["chunk"], payload=payload)


@pytest.mark.asyncio
async def test_remote_toolkit_validation_rejects_unary_output_type_mismatch() -> None:
    room = _FakeRoom()
    toolkit = _make_hosted_toolkit(tools=[_WrongOutputTypeTool()])
    toolkit._room = room  # type: ignore[assignment]

    await toolkit._tool_call(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=45,
        msg_type="room.tool_call.test",
        data=pack_message(
            header={
                "name": "wrong_output_type",
                "arguments": JsonContent(json={}).to_json(),
                "caller_id": "caller-1",
                "tool_call_id": "tc-req-3",
            }
        ),
    )

    await asyncio.wait_for(room.protocol.response_sent, timeout=2.0)

    response = unpack_content(room.protocol.sent[-1].data)
    assert isinstance(response, ErrorContent)
    assert "output content type 'text'" in response.text


@pytest.mark.asyncio
async def test_remote_toolkit_validation_mode_none_skips_output_type_validation() -> (
    None
):
    room = _FakeRoom()
    toolkit = _make_hosted_toolkit(
        tools=[_WrongOutputTypeTool()],
        validation_mode="none",
    )
    toolkit._room = room  # type: ignore[assignment]

    await toolkit._tool_call(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=46,
        msg_type="room.tool_call.test",
        data=pack_message(
            header={
                "name": "wrong_output_type",
                "arguments": JsonContent(json={}).to_json(),
                "caller_id": "caller-1",
                "tool_call_id": "tc-req-4",
            }
        ),
    )

    await asyncio.wait_for(room.protocol.response_sent, timeout=2.0)

    response = unpack_content(room.protocol.sent[-1].data)
    assert isinstance(response, TextContent)
    assert response.text == "not-json"


@pytest.mark.asyncio
async def test_remote_toolkit_validation_rejects_unary_input_schema_mismatch() -> None:
    room = _FakeRoom()
    toolkit = _make_hosted_toolkit(tools=[_SchemaValidatedTextEchoTool()])
    toolkit._room = room  # type: ignore[assignment]

    await toolkit._tool_call(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=47,
        msg_type="room.tool_call.test",
        data=pack_message(
            header={
                "name": "schema_validated_text_echo",
                "arguments": TextContent(text="not-ok").to_json(),
                "caller_id": "caller-1",
                "tool_call_id": "tc-req-5",
            }
        ),
    )

    await asyncio.wait_for(room.protocol.response_sent, timeout=2.0)

    response = unpack_content(room.protocol.sent[-1].data)
    assert isinstance(response, ErrorContent)
    assert "input does not match input_schema" in response.text
    assert response.code == ErrorCode.INVALID_REQUEST


@pytest.mark.asyncio
async def test_remote_toolkit_validation_rejects_stream_input_schema_mismatch() -> None:
    room = _FakeRoom()
    toolkit = _make_hosted_toolkit(tools=[_CollectValidatedTextStreamTool()])
    toolkit._room = room  # type: ignore[assignment]

    await toolkit._tool_call(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=48,
        msg_type="room.tool_call.test",
        data=pack_message(
            header={
                "name": "collect_validated_text_stream",
                "arguments": _ControlContent(method="open").to_json(),
                "caller_id": "caller-1",
                "tool_call_id": "tc-req-6",
            }
        ),
    )

    await toolkit._tool_call_request_chunk(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=48,
        msg_type="room.tool_call_request_chunk.test",
        data=pack_message(
            header={
                "tool_call_id": "tc-req-6",
                "chunk": TextContent(text="ok-first").to_json(),
            }
        ),
    )
    await toolkit._tool_call_request_chunk(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=48,
        msg_type="room.tool_call_request_chunk.test",
        data=pack_message(
            header={
                "tool_call_id": "tc-req-6",
                "chunk": TextContent(text="invalid").to_json(),
            }
        ),
    )
    await toolkit._tool_call_request_chunk(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=48,
        msg_type="room.tool_call_request_chunk.test",
        data=pack_message(
            header={
                "tool_call_id": "tc-req-6",
                "chunk": _ControlContent(method="close").to_json(),
            }
        ),
    )

    await asyncio.wait_for(room.protocol.response_sent, timeout=2.0)

    response = unpack_content(room.protocol.sent[-1].data)
    assert isinstance(response, ErrorContent)
    assert "input does not match input_schema" in response.text
    assert response.code == ErrorCode.INVALID_REQUEST


@pytest.mark.asyncio
async def test_remote_toolkit_validation_stream_output_sends_invalid_data_close_only() -> (
    None
):
    room = _FakeRoom()
    toolkit = _make_hosted_toolkit(tools=[_InvalidStreamOutputTool()])
    toolkit._room = room  # type: ignore[assignment]

    await toolkit._tool_call(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=49,
        msg_type="room.tool_call.test",
        data=pack_message(
            header={
                "name": "invalid_stream_output",
                "arguments": JsonContent(json={}).to_json(),
                "caller_id": "caller-1",
                "tool_call_id": "tc-req-7",
            }
        ),
    )

    await asyncio.wait_for(room.protocol.response_sent, timeout=2.0)
    await asyncio.sleep(0.05)

    assert len(room.protocol.sent) == 3
    open_response = unpack_content(room.protocol.sent[0].data)
    assert isinstance(open_response, _ControlContent)
    assert open_response.method == "open"

    first_chunk = _decode_chunk(room.protocol.sent[1])
    assert isinstance(first_chunk, JsonContent)
    assert first_chunk.json == {"ok": 1}

    close_chunk = _decode_chunk(room.protocol.sent[2])
    assert isinstance(close_chunk, _ControlContent)
    assert close_chunk.method == "close"
    assert close_chunk.status_code == ControlCloseStatus.INVALID_DATA
    assert close_chunk.message is not None
    assert "output content type 'text'" in close_chunk.message


@pytest.mark.asyncio
async def test_remote_toolkit_registration_preserves_strict_tool_metadata() -> None:
    room = _FakeRoom()
    toolkit = _make_hosted_toolkit(
        tools=[
            _StrictToggleTool(name="strict_tool", strict=True),
            _StrictToggleTool(name="loose_tool", strict=False),
        ],
    )
    toolkit._room = room  # type: ignore[assignment]

    await toolkit._register(public=True)

    assert len(room.requests) == 1
    typ, request = room.requests[0]
    assert typ == "room.register_toolkit"
    assert request["tools"]["strict_tool"]["strict"] is True
    assert request["tools"]["loose_tool"]["strict"] is False


@pytest.mark.asyncio
async def test_remote_toolkit_registration_includes_annotations() -> None:
    room = _FakeRoom()
    toolkit = _RemoteToolkitWrapper(
        toolkit=Toolkit(
            name="test",
            tools=[_StrictToggleTool(name="strict_tool", strict=True)],
            public=True,
            annotations={"meshagent.tool_search": "true"},
        )
    )
    toolkit._room = room  # type: ignore[assignment]

    await toolkit._register(public=True)

    assert len(room.requests) == 1
    typ, request = room.requests[0]
    assert typ == "room.register_toolkit"
    assert request["annotations"] == {"meshagent.tool_search": "true"}


@pytest.mark.asyncio
async def test_remote_toolkit_logs_tool_failures_as_warnings_with_exception_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    room = _FakeRoom()
    toolkit = _make_hosted_toolkit(tools=[_FailingTool()])
    toolkit._room = room  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="hosting"):
        await toolkit._tool_call(
            protocol=room.protocol,  # type: ignore[arg-type]
            message_id=50,
            msg_type="room.tool_call.test",
            data=pack_message(
                header={
                    "name": "failing_tool",
                    "arguments": JsonContent(json={}).to_json(),
                    "caller_id": "caller-1",
                    "tool_call_id": "tc-req-8",
                }
            ),
        )

        await asyncio.wait_for(room.protocol.response_sent, timeout=2.0)

    response = unpack_content(room.protocol.sent[-1].data)
    assert isinstance(response, ErrorContent)
    assert response.text == "messaging is already enabled"
    assert response.code == ErrorCode.INVALID_REQUEST

    warning_records = [record for record in caplog.records if record.name == "hosting"]
    assert len(warning_records) == 1
    assert warning_records[0].levelno == logging.WARNING
    assert warning_records[0].message == "messaging is already enabled"


@pytest.mark.asyncio
async def test_remote_toolkit_generic_tool_failures_have_no_error_code() -> None:
    room = _FakeRoom()
    toolkit = _make_hosted_toolkit(tools=[_GenericFailingTool()])
    toolkit._room = room  # type: ignore[assignment]

    await toolkit._tool_call(
        protocol=room.protocol,  # type: ignore[arg-type]
        message_id=51,
        msg_type="room.tool_call.test",
        data=pack_message(
            header={
                "name": "generic_failing_tool",
                "arguments": JsonContent(json={}).to_json(),
                "caller_id": "caller-1",
                "tool_call_id": "tc-req-9",
            }
        ),
    )

    await asyncio.wait_for(room.protocol.response_sent, timeout=2.0)

    response = unpack_content(room.protocol.sent[-1].data)
    assert isinstance(response, ErrorContent)
    assert response.text == "generic failure"
    assert response.code is None


@pytest.mark.asyncio
async def test_remote_toolkit_stop_skips_unregister_when_room_is_closed() -> None:
    room = _FakeRoom()
    toolkit = _make_hosted_toolkit(tools=[])

    toolkit._room = room  # type: ignore[assignment]
    toolkit._registration_id = "registration-1"
    room.protocol.register_handler("room.tool_call.test", toolkit._tool_call)
    room.protocol.register_handler(
        "room.tool_call_request_chunk.test",
        toolkit._tool_call_request_chunk,
    )
    room.is_closed = True

    await toolkit.stop()

    assert room.requests == []
    assert toolkit._registration_id is None
    assert room.protocol.get_handler("room.tool_call.test") is None
    assert room.protocol.get_handler("room.tool_call_request_chunk.test") is None


@pytest.mark.asyncio
async def test_remote_toolkit_reregisters_after_room_reconnect() -> None:
    room = _FakeRoom()
    toolkit = _make_hosted_toolkit(tools=[])

    await toolkit.start(room=room)  # type: ignore[arg-type]

    assert [typ for typ, _ in room.requests] == ["room.register_toolkit"]
    assert toolkit._registration_id == "registration-1"

    room.is_connected = False
    room.emit("disconnected", reason="transient transport error")
    assert toolkit._registration_id is None

    room.is_connected = True
    room.emit("reconnected")
    await _wait_until(lambda: len(room.requests) == 2)

    assert [typ for typ, _ in room.requests] == [
        "room.register_toolkit",
        "room.register_toolkit",
    ]
    assert toolkit._registration_id == "registration-2"

    await toolkit.stop()

    assert room._events == {}
