import logging

from meshagent.tools import FunctionTool, ContentTool, Toolkit, ToolContext, BaseTool
from meshagent.api.messaging import (
    pack_request_parts,
    ErrorContent,
    Content,
    EmptyContent,
    JsonContent,
    TextContent,
    FileContent,
    LinkContent,
    ControlCloseStatus,
    _ControlContent,
    ensure_content,
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
from meshagent.api.protocol import Protocol
from meshagent.api.room_server_client import RoomClient, RoomException, ToolContentSpec
from meshagent.api.chan import ChanClosed

from aiohttp import web

from typing import Optional, Callable, Any, Literal
from collections.abc import AsyncIterable
import asyncio
from warnings import deprecated
import signal
from jsonschema import ValidationError, validate

logger = logging.getLogger("hosting")
ValidationMode = Literal["full", "content_types", "none"]


class InvalidToolDataException(RoomException):
    pass


class RemoteTool(FunctionTool):
    def __init__(
        self,
        *,
        name,
        input_schema,
        title=None,
        description=None,
        rules=None,
        thumbnail_url=None,
        defs=None,
    ):
        super().__init__(
            name=name,
            input_schema=input_schema,
            title=title,
            description=description,
            rules=rules,
            thumbnail_url=thumbnail_url,
            defs=defs,
        )
        self._room = None

    async def start(self, *, room: RoomClient):
        if self._room is not None:
            raise RoomException("room is already started")

        self._room = room

    async def stop(self):
        pass


class RemoteToolkit(Toolkit):
    """Remote toolkit host protocol contract.

    Wire protocol:
    - `agent.invoke_tool` starts a call and includes `tool_call_id`.
    - `arguments` always carries the first input content header.
      - Unary input: any content header (`json`, `text`, `file`, `link`, `empty`).
      - Stream input: `_ControlContent(method="open")`.
    - Streamed request items are sent as `agent.tool_call_request_chunk` with
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
    - Unary output: one `agent.tool_call_response`.
    - Streaming output:
      1. send `agent.tool_call_response` with `_ControlContent(method="open")`
      2. send each item via `agent.tool_call_response_chunk`
      3. send `_ControlContent(method="close")` via `agent.tool_call_response_chunk`
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
        name: str,
        tools: list[BaseTool] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
        public: bool = True,
        validation_mode: ValidationMode = "full",
    ):
        super().__init__(
            name=name,
            description=description,
            title=title,
            tools=tools,
            thumbnail_url=thumbnail_url,
        )

        if tools is None:
            tools = list[BaseTool]()

        self.tools = tools
        self._registration_id = None

        self._room = None
        self.public = public
        if validation_mode not in ("full", "content_types", "none"):
            raise ValueError(
                "validation_mode must be one of 'full', 'content_types', or 'none'"
            )
        self.validation_mode: ValidationMode = validation_mode
        self._request_streams = dict[str, asyncio.Queue[Optional[Content]]]()
        self._pending_request_chunks = dict[str, list[Content]]()

    def _should_validate_content_types(self) -> bool:
        return self.validation_mode in ("full", "content_types")

    def _should_validate_schema(self) -> bool:
        return self.validation_mode == "full"

    @staticmethod
    def _content_kind(content: Content) -> str:
        if isinstance(content, JsonContent):
            return "json"
        if isinstance(content, TextContent):
            return "text"
        if isinstance(content, FileContent):
            return "file"
        if isinstance(content, LinkContent):
            return "link"
        if isinstance(content, EmptyContent):
            return "empty"
        if isinstance(content, _ControlContent):
            return "control"
        if isinstance(content, ErrorContent):
            return "error"
        content_type = content.to_json().get("type", None)
        if isinstance(content_type, str):
            return content_type
        return "unknown"

    @staticmethod
    def _schema_value_for_content(content: Content) -> Any:
        if isinstance(content, JsonContent):
            return content.json
        if isinstance(content, TextContent):
            return content.text
        if isinstance(content, EmptyContent):
            return None
        if isinstance(content, LinkContent):
            return {"name": content.name, "url": content.url}
        if isinstance(content, FileContent):
            return {
                "name": content.name,
                "mime_type": content.mime_type,
                "size": len(content.data),
            }
        if isinstance(content, _ControlContent):
            return {"method": content.method}
        if isinstance(content, ErrorContent):
            return {"text": content.text}
        return content.to_json()

    @staticmethod
    def _schema_with_defs(
        *, schema: dict | None, defs: Optional[dict[str, dict]]
    ) -> dict | None:
        if schema is None:
            return None
        merged = {**schema}
        if defs is None:
            return merged
        existing_defs = merged.get("$defs", None)
        if isinstance(existing_defs, dict):
            merged["$defs"] = {**defs, **existing_defs}
        else:
            merged["$defs"] = {**defs}
        return merged

    def _validate_stream_mode(
        self,
        *,
        tool_name: str,
        direction: Literal["input", "output"],
        spec: ToolContentSpec | None,
        stream: bool,
    ) -> None:
        if spec is None or not self._should_validate_content_types():
            return
        if spec.stream != stream:
            expected = "streamed" if spec.stream else "single-content"
            actual = "streamed" if stream else "single-content"
            raise InvalidToolDataException(
                f"tool '{tool_name}' {direction} is {actual} but {direction}_spec requires {expected} {direction}"
            )

    def _validate_content_type(
        self,
        *,
        tool_name: str,
        direction: Literal["input", "output"],
        spec: ToolContentSpec | None,
        content: Content,
    ) -> None:
        if spec is None or not self._should_validate_content_types():
            return
        content_type = self._content_kind(content)
        if content_type not in spec.types:
            allowed = ", ".join(spec.types)
            raise InvalidToolDataException(
                f"tool '{tool_name}' {direction} content type '{content_type}' is not allowed by {direction}_spec ({allowed})"
            )

    def _validate_schema(
        self,
        *,
        tool_name: str,
        direction: Literal["input", "output"],
        content: Content,
        schema: dict | None,
        defs: Optional[dict[str, dict]],
    ) -> None:
        if not self._should_validate_schema():
            return
        resolved_schema = self._schema_with_defs(schema=schema, defs=defs)
        if resolved_schema is None:
            return
        try:
            validate(
                instance=self._schema_value_for_content(content),
                schema=resolved_schema,
            )
        except ValidationError as ex:
            raise InvalidToolDataException(
                f"tool '{tool_name}' {direction} does not match {direction}_schema: {ex.message}"
            ) from ex

    def _validate_input_content(
        self,
        *,
        tool: BaseTool,
        content: Content,
        validate_schema: bool,
    ) -> None:
        self._validate_content_type(
            tool_name=tool.name,
            direction="input",
            spec=tool.input_spec,
            content=content,
        )
        if validate_schema:
            self._validate_schema(
                tool_name=tool.name,
                direction="input",
                content=content,
                schema=tool.input_schema,
                defs=tool.defs,
            )

    def _validate_output_content(self, *, tool: BaseTool, content: Content) -> None:
        self._validate_content_type(
            tool_name=tool.name,
            direction="output",
            spec=tool.output_spec,
            content=content,
        )
        self._validate_schema(
            tool_name=tool.name,
            direction="output",
            content=content,
            schema=tool.output_schema,
            defs=tool.defs,
        )

    @property
    def room(self):
        return self._room

    async def start(self, *, room: RoomClient):
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

        if self._room is not None:
            raise RoomException("room is already started")

        self._room = room

        self._room.protocol.register_handler(
            f"agent.tool_call.{self.name}", self._tool_call
        )
        self._room.protocol.register_handler(
            f"agent.tool_call_request_chunk.{self.name}",
            self._tool_call_request_chunk,
        )

        await self._register(public=self.public)

    async def stop(self):
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

        await self._unregister()
        self._room.protocol.unregister_handler(
            f"agent.tool_call.{self.name}", self._tool_call
        )
        self._room.protocol.unregister_handler(
            f"agent.tool_call_request_chunk.{self.name}",
            self._tool_call_request_chunk,
        )

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

    def _mangle(self, name: str):
        if self.public:
            return name
        else:
            n = self.room.local_participant.get_attribute("name")
            return f"{n}_{name}"

    def _unmangle(self, name: str):
        if self.public:
            return name
        else:
            n = self.room.local_participant.get_attribute("name")
            return name.removeprefix(f"{n}_")

    async def _tool_call(
        self, protocol: Protocol, message_id: int, msg_type: str, data: bytes
    ):
        async def do_call():
            # Decode and parse the message
            message, attachment = unpack_message(data)
            name = self._unmangle(message["name"])
            raw_arguments = message["arguments"]
            caller_id = message["caller_id"]
            caller_context = message.get("caller_context", None)
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
                    type="agent.tool_call_response_chunk",
                    message_id=message_id,
                    data=pack_message(
                        header={
                            "tool_call_id": tool_call_id,
                            "chunk": chunk_payload,
                        },
                        data=payload,
                    ),
                )

            chunk_queue: asyncio.Queue[Optional[Any]] = asyncio.Queue()

            async def forward_chunks() -> None:
                while True:
                    chunk = await chunk_queue.get()
                    if chunk is None:
                        return
                    try:
                        await send_tool_call_response_chunk(chunk)
                    except Exception as e:
                        logger.error(
                            "unable to forward tool call response chunk",
                            exc_info=e,
                        )

            response_sent = False
            stream_close_emitted = False

            async def send_tool_call_response(response: Content) -> None:
                nonlocal response_sent
                if response_sent:
                    return
                try:
                    await self._room.protocol.send(
                        type="agent.tool_call_response",
                        data=response.pack(),
                        message_id=message_id,
                    )
                except ChanClosed:
                    logger.debug(
                        "tool call response dropped because room channel is closed"
                    )
                response_sent = True

            forward_chunks_task = asyncio.create_task(forward_chunks())
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

                context = ToolContext(
                    room=self._room,
                    caller=caller,
                    on_behalf_of=on_behalf_of,
                    caller_context=caller_context,
                    event_handler=lambda event: chunk_queue.put_nowait(event),
                )
                execution_result = None
                request_stream_queue = None
                response: Optional[Content] = None

                tool = self.get_tool(name)
                if request_stream:
                    if not isinstance(tool, ContentTool):
                        raise RoomException(
                            f"tool '{name}' does not accept streamed input"
                        )
                else:
                    if not isinstance(tool, (FunctionTool, ContentTool)):
                        raise RoomException(
                            "tools must extend FunctionTool or ContentTool to be invokable"
                        )
                self._validate_stream_mode(
                    tool_name=name,
                    direction="input",
                    spec=tool.input_spec,
                    stream=request_stream,
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

                    async def attachment_stream():
                        while True:
                            item = await request_stream_queue.get()
                            if item is None:
                                return
                            yield item

                    async def validated_attachment_stream():
                        async for item in attachment_stream():
                            normalized_item = ensure_content(item)
                            self._validate_input_content(
                                tool=tool,
                                content=normalized_item,
                                validate_schema=True,
                            )
                            yield normalized_item

                    execution_result = await tool.execute(
                        context=context,
                        input=validated_attachment_stream(),
                    )
                else:
                    if isinstance(tool, ContentTool):
                        normalized_input_content = ensure_content(input_content)
                        self._validate_input_content(
                            tool=tool,
                            content=normalized_input_content,
                            validate_schema=True,
                        )
                        execution_result = await tool.execute(
                            context=context,
                            input=normalized_input_content,
                        )
                    else:
                        if isinstance(input_content, EmptyContent):
                            args = {}
                        elif isinstance(input_content, JsonContent):
                            if not isinstance(input_content.json, dict):
                                raise InvalidToolDataException(
                                    "non-stream function tool input json chunk must contain an object"
                                )
                            args = input_content.json
                        else:
                            raise InvalidToolDataException(
                                f"tool '{name}' requires JSON object input"
                            )
                        # FunctionTool schemas are validated by Toolkit.execute against kwargs.
                        # Validate declared content kinds against the normalized JSON call shape.
                        self._validate_input_content(
                            tool=tool,
                            content=JsonContent(json=args),
                            validate_schema=False,
                        )

                        execution_result = await self.execute(
                            context=context,
                            name=name,
                            input=JsonContent(json=args),
                        )

                if isinstance(execution_result, AsyncIterable):
                    self._validate_stream_mode(
                        tool_name=name,
                        direction="output",
                        spec=tool.output_spec,
                        stream=True,
                    )
                    await send_tool_call_response(_ControlContent(method="open"))
                    try:
                        async for item in execution_result:
                            normalized_output = ensure_content(item)
                            self._validate_output_content(
                                tool=tool,
                                content=normalized_output,
                            )
                            chunk_queue.put_nowait(normalized_output)
                    except Exception:
                        raise
                    else:
                        chunk_queue.put_nowait(_ControlContent(method="close"))
                        stream_close_emitted = True
                    response = None
                else:
                    self._validate_stream_mode(
                        tool_name=name,
                        direction="output",
                        spec=tool.output_spec,
                        stream=False,
                    )
                    response = ensure_content(execution_result)
                    self._validate_output_content(tool=tool, content=response)

            except Exception as e:
                logger.error("tool call failed", exc_info=e)
                if response_sent:
                    if isinstance(e, InvalidToolDataException):
                        if not stream_close_emitted:
                            chunk_queue.put_nowait(
                                _ControlContent(
                                    method="close",
                                    status_code=ControlCloseStatus.INVALID_DATA,
                                    message=str(e),
                                )
                            )
                            stream_close_emitted = True
                    else:
                        chunk_queue.put_nowait(ErrorContent(text=f"{e}"))
                        if not stream_close_emitted:
                            chunk_queue.put_nowait(_ControlContent(method="close"))
                            stream_close_emitted = True
                else:
                    response = ErrorContent(text=f"{e}")
            finally:
                request_stream_queue = self._request_streams.pop(tool_call_id, None)
                self._pending_request_chunks.pop(tool_call_id, None)
                if request_stream_queue is not None:
                    request_stream_queue.put_nowait(None)
                chunk_queue.put_nowait(None)
                await forward_chunks_task

            if response is not None:
                await send_tool_call_response(response)

        task = asyncio.create_task(do_call())

        def on_done(task: asyncio.Task):
            try:
                task.result()
            except Exception as e:
                logger.error("tool call failed", exc_info=e)

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

            children[self._mangle(tool.name)] = {
                "title": tool.title,
                "description": tool.description,
                "input_spec": None
                if tool.input_spec is None
                else tool.input_spec.to_json(),
                "output_spec": None
                if tool.output_spec is None
                else tool.output_spec.to_json(),
                "thumbnail_url": tool.thumbnail_url,
                "defs": tool.defs,
                "pricing": tool.pricing,
                "supports_context": tool.supports_context,
            }

        result = await self._room.send_request(
            "agent.register_toolkit",
            {
                "name": self.name,
                "description": self.description,
                "title": self.title,
                "tools": children,
                "public": public,
                "thumbnail_url": self.thumbnail_url,
            },
        )
        self._registration_id = result["id"]

    async def _unregister(self):
        await self._room.send_request(
            "agent.unregister_toolkit", {"id": self._registration_id}
        )
        self._registration_id = None


@deprecated("use ServiceHost and the cli to connect toolkits")
async def connect_remote_toolkit(*, room_name: str, toolkit: Toolkit):
    async with RoomClient(
        protocol=websocket_protocol(
            participant_name=toolkit.name, room_name=room_name, role="tool"
        )
    ) as room:
        remote = RemoteToolkit(
            name=toolkit.name,
            description=toolkit.description,
            title=toolkit.title,
            tools=[*toolkit.tools],
        )

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
        cls: Optional[T] = None,
        path: Optional[str] = None,
        app: Optional[web.Application] = None,
        host=None,
        port=None,
        webhook_secret=None,
        create_toolkit: Optional[Callable[[dict], RemoteToolkit]] = None,
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

            def default_create_toolkit(arguments: dict) -> RemoteToolkit:
                t = cls(**arguments)
                return t

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

        toolkit = RemoteToolkit(
            name=t.name, tools=t.tools, title=t.title, description=t.description
        )

        async def run():
            async with RoomClient(
                protocol=WebSocketClientProtocol(url=room_url, token=token)
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
