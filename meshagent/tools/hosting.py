import logging

from meshagent.api.messaging import (
    pack_request_parts,
    ErrorContent,
    Content,
    JsonContent,
    ControlCloseStatus,
    _ControlContent,
    unpack_content_parts,
    unpack_message,
    pack_message,
)
from meshagent.api import (
    websocket_protocol,
    RemoteParticipant,
    WebhookServer,
    WebSocketClientProtocol,
    CallEvent,
    RoomMessage,
)
from meshagent.api.participant import Participant
from meshagent.api.protocol import Protocol
from meshagent.api.room_server_client import RoomClient, RoomException
from meshagent.api.chan import ChanClosed
from meshagent.tools.tool import ContentTool, FunctionTool, RoomToolContext, ToolContext
from meshagent.tools.toolkit import InvalidToolDataException, Toolkit, ValidationMode

from aiohttp import web

from typing import Optional, Callable, Any
from collections.abc import AsyncIterable, Awaitable
import asyncio
from warnings import deprecated
import signal

logger = logging.getLogger("hosting")


def _error_content_for_exception(ex: Exception) -> ErrorContent:
    code = ex.code if isinstance(ex, RoomException) else None
    return ErrorContent(text=f"{ex}", code=code)


def _log_tool_call_failure(ex: Exception) -> None:
    logger.warning(str(ex), exc_info=ex)


async def stream_tool_call(
    *,
    toolkit: Toolkit,
    validation_mode: ValidationMode | None = None,
    room: RoomClient | None,
    caller: Participant,
    on_behalf_of: Participant | None,
    name: str,
    input: Content | AsyncIterable[Content],
    item_id: str | None = None,
    send_response: Callable[[Content], Awaitable[None]],
    send_chunk: Optional[Callable[[Any], Awaitable[None]]] = None,
) -> None:
    chunk_queue: asyncio.Queue[Optional[Any]] | None = None
    forward_chunks_task: asyncio.Task[None] | None = None
    response_sent = False
    stream_close_emitted = False

    if send_chunk is not None:
        chunk_queue = asyncio.Queue()

        async def forward_chunks() -> None:
            while True:
                chunk = await chunk_queue.get()
                if chunk is None:
                    return
                try:
                    await send_chunk(chunk)
                except Exception as ex:
                    logger.error(
                        "unable to forward tool call response chunk",
                        exc_info=ex,
                    )

        forward_chunks_task = asyncio.create_task(forward_chunks())

    async def send_tool_call_response(response: Content) -> None:
        nonlocal response_sent
        if response_sent:
            return
        await send_response(response)
        response_sent = True

    try:
        event_handler = None
        if chunk_queue is not None:

            def handle_event(event: dict) -> None:
                if item_id is not None and "item_id" not in event:
                    event = {**event, "item_id": item_id}
                chunk_queue.put_nowait(event)

            event_handler = handle_event

        if room is None:
            context: ToolContext = ToolContext(
                caller=caller,
                on_behalf_of=on_behalf_of,
                event_handler=event_handler,
            )
        else:
            context = RoomToolContext(
                room=room,
                caller=caller,
                on_behalf_of=on_behalf_of,
                event_handler=event_handler,
            )

        execution_result = await toolkit.invoke(
            context=context,
            name=name,
            input=input,
            validation_mode=validation_mode,
        )

        if isinstance(execution_result, AsyncIterable):
            await send_tool_call_response(_ControlContent(method="open"))
            if chunk_queue is None:
                return

            async for item in execution_result:
                chunk_queue.put_nowait(item)

            chunk_queue.put_nowait(_ControlContent(method="close"))
            stream_close_emitted = True
            return

        await send_tool_call_response(execution_result)
    except Exception as ex:
        _log_tool_call_failure(ex)
        if response_sent:
            if chunk_queue is not None:
                if isinstance(ex, InvalidToolDataException):
                    if not stream_close_emitted:
                        chunk_queue.put_nowait(
                            _ControlContent(
                                method="close",
                                status_code=ControlCloseStatus.INVALID_DATA,
                                message=str(ex),
                            )
                        )
                        stream_close_emitted = True
                else:
                    chunk_queue.put_nowait(_error_content_for_exception(ex))
                    if not stream_close_emitted:
                        chunk_queue.put_nowait(_ControlContent(method="close"))
                        stream_close_emitted = True
        else:
            await send_tool_call_response(_error_content_for_exception(ex))
    finally:
        if chunk_queue is not None and forward_chunks_task is not None:
            chunk_queue.put_nowait(None)
            await forward_chunks_task


class RemoteTool(FunctionTool):
    def __init__(
        self,
        *,
        name,
        input_schema,
        strict: bool = True,
        title=None,
        description=None,
        rules=None,
        defs=None,
    ):
        self._room: RoomClient | None = None
        super().__init__(
            name=name,
            input_schema=input_schema,
            strict=strict,
            title=title,
            description=description,
            rules=rules,
            defs=defs,
        )

    @property
    def room(self) -> RoomClient:
        if self._room is None:
            raise RuntimeError(
                f"Remote tool '{self.name}' requires start(room=...) before use"
            )
        return self._room

    async def start(self, *, room: RoomClient):
        if self._room is not None:
            raise RoomException("room is already started")
        self._room = room

    async def stop(self):
        self._room = None


class _RemoteToolkitWrapper(Toolkit):
    """Remote toolkit host protocol contract.

    Wire protocol:
    - `room.invoke_tool` starts a call and includes `tool_call_id`.
    - `arguments` always carries the first input content header.
      - Unary input: any content header (`json`, `text`, `file`, `link`, `empty`).
      - Stream input: `_ControlContent(method="open")`.
    - Streamed request items are sent as `room.tool_call_request_chunk` with
      `{"tool_call_id", "chunk"}`; each chunk may include payload bytes.
    - Request stream `open`/`close` control chunks are transport framing and are
      not forwarded to tool implementations.

    Dispatch semantics:
    - Streamed request input (`open` first chunk) requires a `ContentTool`.
    - Unary request input:
      - `FunctionTool` receives JSON object kwargs.
      - `ContentTool` receives a single `Content` input.
    - Request chunks received before a stream is attached are buffered per
      `tool_call_id` and replayed in order once attached.

    Response semantics:
    - Unary output: one `room.tool_call_response`.
    - Streaming output:
      1. send `room.tool_call_response` with `_ControlContent(method="open")`
      2. send each item via `room.tool_call_response_chunk`
      3. send `_ControlContent(method="close")` via `room.tool_call_response_chunk`
    - Tool return values are normalized with `ensure_content`.

    Validation (`validation_mode`):
    - `none`: no spec/schema validation.
    - `content_types`: validate declared `input_spec` / `output_spec`
      content kinds and stream-vs-unary mode.
    - `full` (default): `content_types` plus JSON Schema validation:
      - unary input/output validates one item
      - streamed input/output validates each item in the stream
      - if `$defs` are declared on the tool, they are merged into schema validation.
    - Validation failures are surfaced as tool-call errors:
      - before response-stream open: unary `ErrorContent` response
      - after response-stream open: `_ControlContent(method="close", status_code=ControlCloseStatus.INVALID_DATA, message=...)`
        without an intermediate `ErrorContent` chunk.

    Error/disconnect semantics:
    - Tool execution errors use the same unary/stream error path as validation.
    - Early input disconnect unblocks active request-stream readers and removes
      pending stream state for cleanup.
    - Post-open `ErrorContent` chunks are non-terminal by default.
    - Validation/data errors terminate the stream with non-1000 close status code.
    """

    def __init__(
        self,
        *,
        toolkit: Toolkit,
        public: Optional[bool] = None,
    ):
        super().__init__(
            name=toolkit.name,
            description=toolkit.description,
            title=toolkit.title,
            tools=toolkit.tools,
            validation_mode=toolkit.validation_mode,
            public=toolkit.public if public is None else public,
            annotations=toolkit.annotations,
        )
        self._registration_id = None

        self._room = None
        self._request_streams = dict[str, asyncio.Queue[Optional[Content]]]()
        self._pending_request_chunks = dict[str, list[Content]]()
        self._register_task: asyncio.Task[None] | None = None
        self._room_disconnected_handler: Callable[..., None] | None = None
        self._room_reconnected_handler: Callable[[], None] | None = None

    @property
    def room(self):
        return self._room

    async def start(self, *, room: RoomClient):
        if self._room is not None:
            raise RoomException("room is already started")

        starts = []

        for tool in self.tools:
            if isinstance(tool, RemoteTool):
                starts.append(tool.start(room=room))

        results = await asyncio.gather(*starts, return_exceptions=False)

        for r in results:
            if isinstance(r, BaseException):
                logger.error(
                    f"Unable to start remote tool in toolkit {self.name}", exc_info=r
                )

        self._room = room
        self._room_disconnected_handler = self._on_room_disconnected
        self._room_reconnected_handler = self._on_room_reconnected
        room.on("disconnected", self._room_disconnected_handler)
        room.on("reconnected", self._room_reconnected_handler)

        room.protocol.register_handler(f"room.tool_call.{self.name}", self._tool_call)
        room.protocol.register_handler(
            f"room.tool_call_request_chunk.{self.name}", self._tool_call_request_chunk
        )

        try:
            await self._register(public=self.public)
        except Exception:
            room.protocol.unregister_handler(
                f"room.tool_call.{self.name}", self._tool_call
            )
            room.protocol.unregister_handler(
                f"room.tool_call_request_chunk.{self.name}",
                self._tool_call_request_chunk,
            )
            self._remove_room_event_handlers(room)
            self._room = None
            stops = []
            for tool in self.tools:
                if isinstance(tool, RemoteTool):
                    stops.append(tool.stop())
            await asyncio.gather(*stops, return_exceptions=True)
            raise

    async def stop(self):
        room = self._room
        if room is None:
            return

        register_task = self._register_task
        self._register_task = None
        if register_task is not None:
            register_task.cancel()
            await asyncio.gather(register_task, return_exceptions=True)

        stops = []
        for tool in self.tools:
            if isinstance(tool, RemoteTool):
                stops.append(tool.stop())

        results = await asyncio.gather(*stops, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                logger.error(
                    f"Unable to stop remote tool in toolkit {self.name}", exc_info=r
                )

        self._remove_room_event_handlers(room)
        await self._unregister()
        room.protocol.unregister_handler(f"room.tool_call.{self.name}", self._tool_call)
        room.protocol.unregister_handler(
            f"room.tool_call_request_chunk.{self.name}",
            self._tool_call_request_chunk,
        )
        self._room = None
        self._registration_id = None

    def _remove_room_event_handlers(self, room: RoomClient) -> None:
        if self._room_disconnected_handler is not None:
            room.off("disconnected", self._room_disconnected_handler)
            self._room_disconnected_handler = None
        if self._room_reconnected_handler is not None:
            room.off("reconnected", self._room_reconnected_handler)
            self._room_reconnected_handler = None

    def _consume_register_task_result(self, task: asyncio.Task[None]) -> None:
        if self._register_task is task:
            self._register_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _on_room_disconnected(self, *, reason: str | None) -> None:
        del reason
        self._registration_id = None

    def _on_room_reconnected(self) -> None:
        room = self._room
        if room is None:
            return
        if self._register_task is not None and not self._register_task.done():
            return

        async def register_current_connection() -> None:
            current_room = self._room
            if current_room is None:
                return

            try:
                await self._register(public=self.public)
            except asyncio.CancelledError:
                raise
            except RoomException as ex:
                if current_room.is_closed or not current_room.is_connected:
                    logger.debug(
                        "skipping reconnect toolkit registration for %s",
                        self.name,
                        exc_info=ex,
                    )
                    return
                logger.warning(
                    "unable to re-register toolkit %s after reconnect",
                    self.name,
                    exc_info=ex,
                )
            except Exception as ex:
                logger.warning(
                    "unable to re-register toolkit %s after reconnect",
                    self.name,
                    exc_info=ex,
                )

        register_task = asyncio.create_task(register_current_connection())
        self._register_task = register_task
        register_task.add_done_callback(self._consume_register_task_result)

    def _enqueue_request_stream_chunk(
        self, *, queue: asyncio.Queue[Optional[Content]], chunk: Content
    ) -> None:
        if isinstance(chunk, _ControlContent):
            if chunk.method == "open":
                return
            if chunk.method == "close":
                queue.put_nowait(None)
                return

            logger.warning("ignoring unknown control chunk method %s", chunk.method)
            return

        queue.put_nowait(chunk)

    async def _tool_call_request_chunk(
        self, protocol: Protocol, message_id: int, msg_type: str, data: bytes
    ) -> None:
        del protocol
        del message_id
        del msg_type

        message, payload = unpack_message(data)
        tool_call_id = message.get("tool_call_id", None)
        if not isinstance(tool_call_id, str) or tool_call_id == "":
            logger.warning("ignoring request stream chunk without tool_call_id")
            return

        chunk_header = message.get("chunk", None)
        if not isinstance(chunk_header, dict):
            logger.warning("ignoring request stream chunk without chunk header")
            return

        try:
            chunk = unpack_content_parts(header=chunk_header, payload=payload)
        except Exception as ex:
            logger.warning("ignoring malformed request stream chunk", exc_info=ex)
            return

        queue = self._request_streams.get(tool_call_id, None)
        if queue is None:
            self._pending_request_chunks.setdefault(tool_call_id, []).append(chunk)
            return

        self._enqueue_request_stream_chunk(queue=queue, chunk=chunk)

    async def _tool_call(
        self, protocol: Protocol, message_id: int, msg_type: str, data: bytes
    ):
        async def do_call():
            # Decode and parse the message
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
                input_content = unpack_content_parts(
                    header=raw_arguments, payload=attachment
                )
            except Exception:
                if not isinstance(raw_arguments, dict):
                    raise InvalidToolDataException(
                        "'arguments' must be a content header object"
                    )
                if attachment not in (None, b""):
                    raise InvalidToolDataException(
                        "legacy binary attachment tool input is no longer supported; send a file content input instead"
                    )
                input_content = JsonContent(json=raw_arguments)

            if isinstance(input_content, _ControlContent):
                if input_content.method != "open":
                    raise InvalidToolDataException(
                        "request stream must start with an open control chunk"
                    )
                request_stream = True

            async def send_tool_call_response_chunk(chunk: Any) -> None:
                payload: bytes | None = None
                chunk_payload: Any = chunk
                if isinstance(chunk, Content):
                    chunk_payload, payload = pack_request_parts(chunk)

                await self._room.protocol.send(
                    type="room.tool_call_response_chunk",
                    message_id=message_id,
                    data=pack_message(
                        header={
                            "tool_call_id": tool_call_id,
                            "chunk": chunk_payload,
                        },
                        data=payload,
                    ),
                )

            async def send_tool_call_response(response: Content) -> None:
                try:
                    await self._room.protocol.send(
                        type="room.tool_call_response",
                        data=response.pack(),
                        message_id=message_id,
                    )
                except ChanClosed:
                    logger.debug(
                        "tool call response dropped because room channel is closed"
                    )

            request_stream_queue: asyncio.Queue[Optional[Content]] | None = None
            try:
                caller = None
                on_behalf_of = None

                for p in self.room.messaging.remote_participants:
                    if p.id == caller_id:
                        caller = p
                        break

                if on_behalf_of_id is not None:
                    for p in self.room.messaging.remote_participants:
                        if p.id == on_behalf_of_id:
                            on_behalf_of = p
                            break

                # TODO: should we pass more info?
                if caller is None:
                    caller = RemoteParticipant(
                        id=caller_id,
                    )

                if on_behalf_of is None and on_behalf_of_id is not None:
                    on_behalf_of = RemoteParticipant(
                        id=on_behalf_of_id,
                    )

                if request_stream:
                    request_stream_queue = asyncio.Queue[Optional[Content]]()
                    self._request_streams[tool_call_id] = request_stream_queue
                    self._enqueue_request_stream_chunk(
                        queue=request_stream_queue,
                        chunk=_ControlContent(method="open"),
                    )

                    buffered_chunks = self._pending_request_chunks.pop(tool_call_id, [])
                    for buffered_chunk in buffered_chunks:
                        self._enqueue_request_stream_chunk(
                            queue=request_stream_queue,
                            chunk=buffered_chunk,
                        )

                    async def attachment_stream() -> AsyncIterable[Content]:
                        while True:
                            item = await request_stream_queue.get()
                            if item is None:
                                return
                            yield item

                    execution_input: Content | AsyncIterable[Content] = (
                        attachment_stream()
                    )
                else:
                    execution_input = input_content

                await stream_tool_call(
                    toolkit=self,
                    validation_mode=self.validation_mode,
                    room=self._room,
                    caller=caller,
                    on_behalf_of=on_behalf_of,
                    name=name,
                    input=execution_input,
                    send_response=send_tool_call_response,
                    send_chunk=send_tool_call_response_chunk,
                )
            finally:
                request_stream_queue = self._request_streams.pop(tool_call_id, None)
                self._pending_request_chunks.pop(tool_call_id, None)
                if request_stream_queue is not None:
                    request_stream_queue.put_nowait(None)

        task = asyncio.create_task(do_call())

        def on_done(task: asyncio.Task):
            try:
                task.result()
            except Exception as e:
                _log_tool_call_failure(e)

        task.add_done_callback(on_done)

    async def _register(self, *, public: bool = False):
        children = {}

        for tool in self.tools:
            if tool.name in children:
                raise RoomException(f"duplicate tool name {tool.name}")
            if not isinstance(tool, (FunctionTool, ContentTool)):
                raise RoomException(
                    f"tool '{tool.name}' must extend FunctionTool or ContentTool"
                )

            children[tool.name] = {
                "title": tool.title,
                "description": tool.description,
                "input_spec": None
                if tool.input_spec is None
                else tool.input_spec.to_json(),
                "output_spec": None
                if tool.output_spec is None
                else tool.output_spec.to_json(),
                "defs": tool.defs,
                "strict": tool.strict if isinstance(tool, FunctionTool) else None,
            }

        result = await self._room.send_request(
            "room.register_toolkit",
            {
                "name": self.name,
                "description": self.description,
                "title": self.title,
                "tools": children,
                "public": public,
                "annotations": self.annotations,
            },
        )
        self._registration_id = result["id"]

    async def _unregister(self):
        room = self._room
        if room is None or self._registration_id is None:
            return
        if room.is_closed:
            self._registration_id = None
            return

        try:
            await room.send_request(
                "room.unregister_toolkit", {"id": self._registration_id}
            )
        except RoomException:
            if room.is_closed:
                self._registration_id = None
                return
            raise
        self._registration_id = None


@deprecated("use ServiceHost and the cli to connect toolkits")
async def connect_remote_toolkit(*, room_name: str, toolkit: Toolkit):
    async with RoomClient(
        protocol_factory=websocket_protocol(
            participant_name=toolkit.name, room_name=room_name, role="tool"
        ).create_factory()
    ) as room:
        remote = _RemoteToolkitWrapper(toolkit=toolkit)

        await remote.start(room=room)

        try:
            term = asyncio.Future()

            def clean_termination(signal, frame):
                term.set_result(True)

            signal.signal(signal.SIGTERM, clean_termination)
            signal.signal(signal.SIGABRT, clean_termination)

            await term

        finally:
            await remote.stop()


class RemoteToolkitServer[T: Toolkit](WebhookServer):
    def __init__(
        self,
        *,
        cls: type[T] | None = None,
        path: Optional[str] = None,
        app: Optional[web.Application] = None,
        host=None,
        port=None,
        webhook_secret=None,
        create_toolkit: Optional[Callable[[dict], T]] = None,
        validate_webhook_secret: Optional[bool] = None,
    ):
        super().__init__(
            path=path,
            app=app,
            host=host,
            port=port,
            webhook_secret=webhook_secret,
            validate_webhook_secret=validate_webhook_secret,
        )

        if create_toolkit is None:
            if cls is None:
                raise ValueError("cls or create_toolkit is required")

            def default_create_toolkit(arguments: dict) -> T:
                return cls(**arguments)

            create_toolkit = default_create_toolkit

        self._create_toolkit = create_toolkit

    async def _spawn(
        self,
        *,
        room_name: str,
        room_url: str,
        token: str,
        arguments: Optional[dict] = None,
    ):
        t = self._create_toolkit(arguments=arguments)

        toolkit = _RemoteToolkitWrapper(toolkit=t)

        async def run():
            async with RoomClient(
                protocol_factory=WebSocketClientProtocol(
                    url=room_url, token=token
                ).create_factory()
            ) as room:
                dismissed = asyncio.Future()

                def on_message(message: RoomMessage):
                    if message.type == "dismiss":
                        logger.info(f"dismissed by {message.from_participant_id}")
                        dismissed.set_result(True)

                room.messaging.on("message", on_message)

                await toolkit.start(room=room)

                done, pending = await asyncio.wait(
                    [dismissed, asyncio.create_task(room.protocol.wait_for_close())],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                await toolkit.stop()

        def on_done(task: asyncio.Task):
            try:
                task.result()
            except Exception as e:
                logger.error("agent encountered an error", exc_info=e)

        task = asyncio.create_task(run())
        task.add_done_callback(on_done)

    async def on_call(self, event: CallEvent):
        await self._spawn(
            room_name=event.room_name,
            room_url=event.room_url,
            token=event.token,
            arguments=event.arguments,
        )


async def start_hosted_toolkit(
    *,
    room: RoomClient,
    toolkit: Toolkit,
) -> _RemoteToolkitWrapper:
    hosted_toolkit = _RemoteToolkitWrapper(toolkit=toolkit)
    await hosted_toolkit.start(room=room)
    return hosted_toolkit
