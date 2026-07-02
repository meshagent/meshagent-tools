import asyncio
from collections.abc import AsyncIterable

import pytest

from meshagent.api import RemoteParticipant, RoomException
from meshagent.api.messaging import (
    Content,
    EmptyContent,
    ErrorContent,
    JsonContent,
    TextContent,
    _ControlContent,
    pack_message,
    unpack_content_parts,
    unpack_message,
)
from meshagent.tools import ContentTool, FunctionTool, ToolContext, Toolkit
import meshagent.tools.hosting as hosting
from meshagent.tools.hosting import (
    RemoteTool,
    RemoteToolkitServer,
    _RemoteToolkitWrapper,
    start_hosted_toolkit,
    stream_tool_call,
)
from meshagent.tools.tool import ToolContentSpec
from meshagent.tools.toolkit import InvalidToolDataException


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


class _EchoFunctionTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="echo",
            input_schema={
                "type": "object",
                "required": [],
                "additionalProperties": True,
                "properties": {},
            },
        )
        self.seen_contexts: list[ToolContext] = []
        self.seen_kwargs: list[dict] = []

    async def execute(self, context: ToolContext, **kwargs) -> Content:
        self.seen_contexts.append(context)
        self.seen_kwargs.append(kwargs)
        return JsonContent(json={"arguments": kwargs})


class _RawContentTool(ContentTool):
    def __init__(self) -> None:
        super().__init__(name="raw")

    async def execute(
        self,
        *,
        context: ToolContext,
        input: AsyncIterable[Content] | Content,
    ) -> Content:
        return JsonContent(
            json={
                "caller": context.caller.id,
                "on_behalf_of": None
                if context.on_behalf_of is None
                else context.on_behalf_of.id,
                "input": input.text if isinstance(input, TextContent) else "wrong",
            }
        )


class _FailingContentTool(ContentTool):
    def __init__(
        self,
        *,
        name: str = "fail",
        stream: bool,
        invalid_data: bool,
        emit_event: bool = False,
        event: dict | None = None,
    ) -> None:
        super().__init__(name=name)
        self._stream = stream
        self._invalid_data = invalid_data
        self._emit_event = emit_event
        self._event = event or {"type": "progress"}

    def _exception(self) -> Exception:
        if self._invalid_data:
            return InvalidToolDataException("bad stream data")
        return RuntimeError("generic boom")

    async def execute(
        self,
        *,
        context: ToolContext,
        input: AsyncIterable[Content] | Content,
    ) -> AsyncIterable[Content] | Content:
        del input
        if self._emit_event:
            context.emit(self._event)
        if not self._stream:
            raise self._exception()

        async def stream() -> AsyncIterable[Content]:
            yield TextContent(text="first")
            raise self._exception()

        return stream()


def _pack_request_stream_chunk(tool_call_id: str, chunk: Content) -> bytes:
    chunk_header, chunk_payload = unpack_message(chunk.pack())
    return pack_message(
        header={"tool_call_id": tool_call_id, "chunk": chunk_header},
        data=chunk_payload,
    )


@pytest.mark.asyncio
async def test_remote_tool_start_stop_matches_python_lifecycle() -> None:
    tool = RemoteTool(
        name="remote_echo",
        input_schema={
            "type": "object",
            "required": [],
            "additionalProperties": False,
            "properties": {},
        },
    )
    with pytest.raises(
        RuntimeError,
        match="Remote tool 'remote_echo' requires start\\(room=\\.\\.\\.\\) before use",
    ):
        _ = tool.room

    room = object()
    await tool.start(room=room)
    assert tool.room is room
    with pytest.raises(RoomException, match="room is already started"):
        await tool.start(room=room)
    await tool.stop()
    with pytest.raises(
        RuntimeError,
        match="Remote tool 'remote_echo' requires start\\(room=\\.\\.\\.\\) before use",
    ):
        _ = tool.room


@pytest.mark.asyncio
async def test_remote_toolkit_wrapper_register_and_unregister_match_python_requests() -> (
    None
):
    function_tool = _EchoFunctionTool()
    content_tool = _RawContentTool()
    content_tool.title = "Raw"
    content_tool.description = "raw content"
    content_tool.input_spec = ToolContentSpec(types=["text"], stream=False)
    content_tool.output_spec = ToolContentSpec(types=["empty"], stream=False)
    wrapper = _RemoteToolkitWrapper(
        toolkit=Toolkit(
            name="remote",
            title="Remote",
            description="remote tools",
            tools=[function_tool, content_tool],
            annotations={"owner": "tools"},
        ),
        public=False,
    )

    requests: list[tuple[str, dict]] = []

    class FakeRoom:
        is_closed = False

        async def send_request(self, method: str, data: dict) -> dict:
            requests.append((method, data))
            if method == "room.register_toolkit":
                return {"id": "registration-1"}
            return {}

    wrapper._room = FakeRoom()
    await wrapper._register(public=wrapper.public)

    assert wrapper._registration_id == "registration-1"
    assert requests == [
        (
            "room.register_toolkit",
            {
                "name": "remote",
                "description": "remote tools",
                "title": "Remote",
                "tools": {
                    "echo": {
                        "title": "echo",
                        "description": "",
                        "input_spec": {
                            "types": ["json"],
                            "stream": False,
                            "schema": {
                                "type": "object",
                                "required": [],
                                "additionalProperties": True,
                                "properties": {},
                            },
                        },
                        "output_spec": None,
                        "defs": None,
                        "strict": True,
                    },
                    "raw": {
                        "title": "Raw",
                        "description": "raw content",
                        "input_spec": {"types": ["text"], "stream": False},
                        "output_spec": {"types": ["empty"], "stream": False},
                        "defs": None,
                        "strict": None,
                    },
                },
                "public": False,
                "annotations": {"owner": "tools"},
            },
        )
    ]

    await wrapper._unregister()
    assert wrapper._registration_id is None
    assert requests[-1] == ("room.unregister_toolkit", {"id": "registration-1"})


@pytest.mark.asyncio
async def test_remote_toolkit_wrapper_register_rejects_duplicate_tool_names_like_python() -> (
    None
):
    wrapper = _RemoteToolkitWrapper(
        toolkit=Toolkit(
            name="remote",
            tools=[
                _EchoFunctionTool(),
                _RawContentTool(),
            ],
        )
    )
    wrapper.tools[1].name = "echo"

    class FakeRoom:
        is_closed = False

        async def send_request(self, method: str, data: dict) -> dict:
            raise AssertionError(
                "duplicate validation should happen before send_request"
            )

    wrapper._room = FakeRoom()
    with pytest.raises(RoomException, match="duplicate tool name echo"):
        await wrapper._register(public=True)


@pytest.mark.asyncio
async def test_start_hosted_toolkit_starts_and_returns_wrapper_like_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeWrapper:
        def __init__(self, *, toolkit: Toolkit) -> None:
            calls.append(("init", toolkit))
            self.toolkit = toolkit

        async def start(self, *, room: object) -> None:
            calls.append(("start", room))

    monkeypatch.setattr(hosting, "_RemoteToolkitWrapper", FakeWrapper)
    room = object()
    toolkit = Toolkit(name="remote", tools=[])

    wrapper = await start_hosted_toolkit(room=room, toolkit=toolkit)

    assert isinstance(wrapper, FakeWrapper)
    assert wrapper.toolkit is toolkit
    assert calls == [("init", toolkit), ("start", room)]


def test_remote_toolkit_server_construction_and_factory_match_python() -> None:
    with pytest.raises(ValueError, match="cls or create_toolkit is required"):
        RemoteToolkitServer()

    seen_arguments: list[dict | None] = []

    def create_toolkit(*, arguments: dict | None) -> Toolkit:
        seen_arguments.append(arguments)
        suffix = "default" if arguments is None else arguments.get("suffix", "default")
        return Toolkit(
            name=f"toolkit-{suffix}",
            tools=[],
            annotations={"source": "server"},
        )

    server = RemoteToolkitServer(create_toolkit=create_toolkit)
    wrapper = server._create_toolkit(arguments={"suffix": "one"})
    assert wrapper.name == "toolkit-one"
    assert wrapper.annotations == {"source": "server"}
    assert seen_arguments == [{"suffix": "one"}]

    default_wrapper = server._create_toolkit(arguments=None)
    assert default_wrapper.name == "toolkit-default"
    assert seen_arguments == [{"suffix": "one"}, None]

    class ExampleToolkit(Toolkit):
        def __init__(self, *, suffix: str = "default") -> None:
            super().__init__(name=f"cls-{suffix}", tools=[])

    cls_server = RemoteToolkitServer(cls=ExampleToolkit)
    assert cls_server._create_toolkit(arguments={"suffix": "two"}).name == "cls-two"


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


@pytest.mark.asyncio
async def test_stream_tool_call_response_state_machine_matches_python() -> None:
    responses: list[Content] = []
    chunks: list[Content | dict] = []

    async def send_response(content: Content) -> None:
        responses.append(content)

    async def send_chunk(content: Content | dict) -> None:
        chunks.append(content)

    await stream_tool_call(
        toolkit=Toolkit(
            name="remote",
            tools=[
                _StreamingContentTool(chunks=[TextContent(text="one")]),
            ],
            validation_mode="none",
        ),
        validation_mode="none",
        room=None,
        caller=RemoteParticipant(id="caller-1"),
        on_behalf_of=None,
        name="stream_text",
        input=TextContent(text="input"),
        send_response=send_response,
        send_chunk=None,
    )
    assert len(responses) == 1
    assert isinstance(responses.pop(), _ControlContent)
    assert chunks == []

    await stream_tool_call(
        toolkit=Toolkit(
            name="remote",
            tools=[_FailingContentTool(stream=False, invalid_data=False)],
            validation_mode="none",
        ),
        validation_mode="none",
        room=None,
        caller=RemoteParticipant(id="caller-2"),
        on_behalf_of=None,
        name="fail",
        input=TextContent(text="input"),
        send_response=send_response,
        send_chunk=send_chunk,
    )
    pre_response_error = responses.pop()
    assert isinstance(pre_response_error, ErrorContent)
    assert pre_response_error.text == "generic boom"
    assert pre_response_error.code is None

    await stream_tool_call(
        toolkit=Toolkit(
            name="remote",
            tools=[_FailingContentTool(stream=True, invalid_data=True)],
            validation_mode="none",
        ),
        validation_mode="none",
        room=None,
        caller=RemoteParticipant(id="caller-3"),
        on_behalf_of=None,
        name="fail",
        input=TextContent(text="input"),
        send_response=send_response,
        send_chunk=send_chunk,
    )
    invalid_open = responses.pop()
    assert isinstance(invalid_open, _ControlContent)
    assert invalid_open.method == "open"
    assert isinstance(chunks[-2], TextContent)
    assert chunks[-2].text == "first"
    assert isinstance(chunks[-1], _ControlContent)
    assert chunks[-1].method == "close"
    assert chunks[-1].status_code == 1007
    assert chunks[-1].message == "bad stream data"

    await stream_tool_call(
        toolkit=Toolkit(
            name="remote",
            tools=[_FailingContentTool(stream=True, invalid_data=False)],
            validation_mode="none",
        ),
        validation_mode="none",
        room=None,
        caller=RemoteParticipant(id="caller-4"),
        on_behalf_of=None,
        name="fail",
        input=TextContent(text="input"),
        send_response=send_response,
        send_chunk=send_chunk,
    )
    generic_open = responses.pop()
    assert isinstance(generic_open, _ControlContent)
    assert generic_open.method == "open"
    assert isinstance(chunks[-3], TextContent)
    assert chunks[-3].text == "first"
    assert isinstance(chunks[-2], ErrorContent)
    assert chunks[-2].text == "generic boom"
    assert chunks[-2].code is None
    assert isinstance(chunks[-1], _ControlContent)
    assert chunks[-1].method == "close"
    assert chunks[-1].status_code == 1000

    await stream_tool_call(
        toolkit=Toolkit(
            name="remote",
            tools=[
                _FailingContentTool(
                    name="emit_then_fail",
                    stream=False,
                    invalid_data=False,
                    emit_event=True,
                )
            ],
            validation_mode="none",
        ),
        validation_mode="none",
        room=None,
        caller=RemoteParticipant(id="caller-5"),
        on_behalf_of=None,
        name="emit_then_fail",
        input=TextContent(text="input"),
        item_id="item-1",
        send_response=send_response,
        send_chunk=send_chunk,
    )
    assert {"type": "progress", "item_id": "item-1"} in chunks
    emitted_error = responses.pop()
    assert isinstance(emitted_error, ErrorContent)
    assert emitted_error.text == "generic boom"

    await stream_tool_call(
        toolkit=Toolkit(
            name="remote",
            tools=[
                _FailingContentTool(
                    name="emit_existing_item_then_fail",
                    stream=False,
                    invalid_data=False,
                    emit_event=True,
                    event={"type": "progress", "item_id": "existing-item"},
                )
            ],
            validation_mode="none",
        ),
        validation_mode="none",
        room=None,
        caller=RemoteParticipant(id="caller-6"),
        on_behalf_of=None,
        name="emit_existing_item_then_fail",
        input=TextContent(text="input"),
        item_id="item-2",
        send_response=send_response,
        send_chunk=send_chunk,
    )
    assert {"type": "progress", "item_id": "existing-item"} in chunks
    emitted_existing_error = responses.pop()
    assert isinstance(emitted_existing_error, ErrorContent)
    assert emitted_existing_error.text == "generic boom"


async def _run_stream_tool_call_once(
    *,
    toolkit: Toolkit,
    name: str,
    input: Content | AsyncIterable[Content],
    caller: RemoteParticipant | None = None,
    on_behalf_of: RemoteParticipant | None = None,
) -> Content:
    responses: list[Content] = []

    async def send_response(content: Content) -> None:
        responses.append(content)

    await stream_tool_call(
        toolkit=toolkit,
        validation_mode="full",
        room=None,
        caller=caller or RemoteParticipant(id="caller-1"),
        on_behalf_of=on_behalf_of,
        name=name,
        input=input,
        send_response=send_response,
        send_chunk=None,
    )
    assert len(responses) == 1
    return responses[0]


@pytest.mark.asyncio
async def test_remote_toolkit_wrapper_non_stream_dispatch_matches_python() -> None:
    echo = _EchoFunctionTool()
    toolkit = Toolkit(
        name="remote",
        tools=[echo, _RawContentTool()],
        validation_mode="full",
    )

    json_result = await _run_stream_tool_call_once(
        toolkit=toolkit,
        name="echo",
        input=JsonContent(json={"message": "hi"}),
        caller=RemoteParticipant(id="caller-1"),
        on_behalf_of=RemoteParticipant(id="sender-1"),
    )
    assert isinstance(json_result, JsonContent)
    assert json_result.json == {"arguments": {"message": "hi"}}
    assert echo.seen_contexts[0].caller.id == "caller-1"
    assert echo.seen_contexts[0].on_behalf_of.id == "sender-1"
    assert echo.seen_kwargs[0] == {"message": "hi"}

    empty_result = await _run_stream_tool_call_once(
        toolkit=toolkit,
        name="echo",
        input=EmptyContent(),
        caller=RemoteParticipant(id="caller-2"),
    )
    assert isinstance(empty_result, JsonContent)
    assert empty_result.json == {"arguments": {}}

    invalid_result = await _run_stream_tool_call_once(
        toolkit=toolkit,
        name="echo",
        input=TextContent(text="not json"),
        caller=RemoteParticipant(id="caller-3"),
    )
    assert isinstance(invalid_result, ErrorContent)
    assert invalid_result.text == "tool 'echo' requires JSON object input"

    content_result = await _run_stream_tool_call_once(
        toolkit=toolkit,
        name="raw",
        input=TextContent(text="hello"),
        caller=RemoteParticipant(id="caller-4"),
        on_behalf_of=RemoteParticipant(id="sender-4"),
    )
    assert isinstance(content_result, JsonContent)
    assert content_result.json == {
        "caller": "caller-4",
        "on_behalf_of": "sender-4",
        "input": "hello",
    }

    missing_result = await _run_stream_tool_call_once(
        toolkit=toolkit,
        name="missing",
        input=EmptyContent(),
        caller=RemoteParticipant(id="caller-5"),
    )
    assert isinstance(missing_result, ErrorContent)
    assert (
        missing_result.text
        == 'a tool with the name "missing" was not found in the toolkit'
    )

    async def attachment_stream() -> AsyncIterable[Content]:
        yield TextContent(text="stream")

    streamed_result = await _run_stream_tool_call_once(
        toolkit=toolkit,
        name="echo",
        input=attachment_stream(),
        caller=RemoteParticipant(id="caller-6"),
    )
    assert isinstance(streamed_result, ErrorContent)
    assert streamed_result.text == "tool 'echo' does not accept streamed input"


@pytest.mark.asyncio
async def test_remote_toolkit_wrapper_decode_error_logs_without_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper = _RemoteToolkitWrapper(toolkit=Toolkit(name="remote", tools=[]))
    sends: list[tuple[str, bytes, int | None]] = []
    failures: list[Exception] = []

    class FakeProtocol:
        async def send(
            self,
            *,
            type: str,
            data: bytes,
            message_id: int | None = None,
        ) -> None:
            sends.append((type, data, message_id))

    class FakeRoom:
        protocol = FakeProtocol()

    def capture_failure(ex: Exception) -> None:
        failures.append(ex)

    wrapper._room = FakeRoom()
    monkeypatch.setattr(hosting, "_log_tool_call_failure", capture_failure)

    await wrapper._tool_call(
        FakeProtocol(),
        56,
        "room.tool_call.remote",
        pack_message(
            header={
                "name": "echo",
                "caller_id": "caller-1",
                "tool_call_id": "tool-call-1",
                "arguments": "not-object",
            },
            data=None,
        ),
    )

    for _ in range(20):
        if failures:
            break
        await asyncio.sleep(0)

    assert [str(failure) for failure in failures] == [
        "'arguments' must be a content header object"
    ]
    assert sends == []


@pytest.mark.asyncio
async def test_remote_toolkit_wrapper_request_chunks_buffer_and_replay_like_python() -> (
    None
):
    wrapper = _RemoteToolkitWrapper(toolkit=Toolkit(name="remote", tools=[]))

    await wrapper._tool_call_request_chunk(
        None,
        1,
        "room.tool_call_request_chunk.remote",
        pack_message(header={"chunk": {}}, data=None),
    )
    await wrapper._tool_call_request_chunk(
        None,
        2,
        "room.tool_call_request_chunk.remote",
        pack_message(header={"tool_call_id": "", "chunk": {}}, data=None),
    )
    await wrapper._tool_call_request_chunk(
        None,
        3,
        "room.tool_call_request_chunk.remote",
        pack_message(header={"tool_call_id": "call-1"}, data=None),
    )
    await wrapper._tool_call_request_chunk(
        None,
        4,
        "room.tool_call_request_chunk.remote",
        _pack_request_stream_chunk("call-1", _ControlContent(method="open")),
    )
    await wrapper._tool_call_request_chunk(
        None,
        5,
        "room.tool_call_request_chunk.remote",
        _pack_request_stream_chunk("call-1", TextContent(text="first")),
    )
    await wrapper._tool_call_request_chunk(
        None,
        6,
        "room.tool_call_request_chunk.remote",
        _pack_request_stream_chunk(
            "call-1",
            _ControlContent(method="close", status_code=1000),
        ),
    )

    queue: asyncio.Queue[Content | None] = asyncio.Queue()
    for chunk in wrapper._pending_request_chunks.pop("call-1", []):
        wrapper._enqueue_request_stream_chunk(queue=queue, chunk=chunk)

    first = await queue.get()
    assert isinstance(first, TextContent)
    assert first.text == "first"
    assert await queue.get() is None

    live_queue: asyncio.Queue[Content | None] = asyncio.Queue()
    wrapper._request_streams["call-2"] = live_queue
    await wrapper._tool_call_request_chunk(
        None,
        7,
        "room.tool_call_request_chunk.remote",
        _pack_request_stream_chunk("call-2", _ControlContent(method="open")),
    )
    await wrapper._tool_call_request_chunk(
        None,
        8,
        "room.tool_call_request_chunk.remote",
        _pack_request_stream_chunk("call-2", TextContent(text="live")),
    )
    await wrapper._tool_call_request_chunk(
        None,
        9,
        "room.tool_call_request_chunk.remote",
        _pack_request_stream_chunk("call-2", _ControlContent(method="unknown")),
    )
    await wrapper._tool_call_request_chunk(
        None,
        10,
        "room.tool_call_request_chunk.remote",
        _pack_request_stream_chunk("call-2", _ControlContent(method="close")),
    )

    live = await live_queue.get()
    assert isinstance(live, TextContent)
    assert live.text == "live"
    assert await live_queue.get() is None


def _decode_hosted_tool_call_input_like_python(
    *,
    message_id: int,
    data: bytes,
) -> tuple[str, str, str | None, str, Content, bool]:
    message, attachment = unpack_message(data)
    name = message["name"]
    raw_arguments = message["arguments"]
    caller_id = message["caller_id"]
    on_behalf_of_id = message.get("on_behalf_of_id", None)
    tool_call_id = message.get("tool_call_id", None)
    if not isinstance(tool_call_id, str) or tool_call_id == "":
        tool_call_id = str(message_id)
    request_stream = False
    try:
        input_content = unpack_content_parts(header=raw_arguments, payload=attachment)
    except Exception:
        if not isinstance(raw_arguments, dict):
            raise hosting.InvalidToolDataException(
                "'arguments' must be a content header object"
            )
        if attachment not in (None, b""):
            raise hosting.InvalidToolDataException(
                "legacy binary attachment tool input is no longer supported; send a file content input instead"
            )
        input_content = JsonContent(json=raw_arguments)

    if isinstance(input_content, _ControlContent):
        if input_content.method != "open":
            raise hosting.InvalidToolDataException(
                "request stream must start with an open control chunk"
            )
        request_stream = True

    return (
        name,
        caller_id,
        on_behalf_of_id,
        tool_call_id,
        input_content,
        request_stream,
    )


def test_remote_toolkit_wrapper_tool_call_input_decode_branches_match_python() -> None:
    decoded = _decode_hosted_tool_call_input_like_python(
        message_id=42,
        data=pack_message(
            header={
                "name": "echo",
                "caller_id": "caller-1",
                "on_behalf_of_id": "sender-1",
                "tool_call_id": "tool-call-1",
                "arguments": {"type": "json", "json": {"message": "hi"}},
            },
            data=None,
        ),
    )
    assert decoded[:4] == ("echo", "caller-1", "sender-1", "tool-call-1")
    assert isinstance(decoded[4], JsonContent)
    assert decoded[4].json == {"message": "hi"}
    assert decoded[5] is False

    legacy_json = _decode_hosted_tool_call_input_like_python(
        message_id=43,
        data=pack_message(
            header={
                "name": "legacy",
                "caller_id": "caller-1",
                "tool_call_id": "",
                "arguments": {"message": "hi"},
            },
            data=None,
        ),
    )
    assert legacy_json[3] == "43"
    assert isinstance(legacy_json[4], JsonContent)
    assert legacy_json[4].json == {"message": "hi"}

    stream_open = _decode_hosted_tool_call_input_like_python(
        message_id=44,
        data=pack_message(
            header={
                "name": "stream",
                "caller_id": "caller-1",
                "arguments": {"type": "control", "method": "open"},
            },
            data=None,
        ),
    )
    assert stream_open[3] == "44"
    assert isinstance(stream_open[4], _ControlContent)
    assert stream_open[4].method == "open"
    assert stream_open[5] is True

    with pytest.raises(
        hosting.InvalidToolDataException,
        match="request stream must start with an open control chunk",
    ):
        _decode_hosted_tool_call_input_like_python(
            message_id=45,
            data=pack_message(
                header={
                    "name": "stream",
                    "caller_id": "caller-1",
                    "arguments": {"type": "control", "method": "close"},
                },
                data=None,
            ),
        )

    with pytest.raises(
        hosting.InvalidToolDataException,
        match="'arguments' must be a content header object",
    ):
        _decode_hosted_tool_call_input_like_python(
            message_id=46,
            data=pack_message(
                header={
                    "name": "legacy",
                    "caller_id": "caller-1",
                    "arguments": "not-object",
                },
                data=None,
            ),
        )

    with pytest.raises(
        hosting.InvalidToolDataException,
        match="legacy binary attachment tool input is no longer supported; send a file content input instead",
    ):
        _decode_hosted_tool_call_input_like_python(
            message_id=47,
            data=pack_message(
                header={
                    "name": "legacy",
                    "caller_id": "caller-1",
                    "arguments": {"message": "hi"},
                },
                data=b"legacy bytes",
            ),
        )
