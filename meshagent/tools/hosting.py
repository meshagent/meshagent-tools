import logging

from meshagent.tools import Tool, StreamTool, Toolkit, ToolContext, BaseTool
from meshagent.api.messaging import (
    ErrorChunk,
    Chunk,
    EmptyChunk,
    JsonChunk,
    _ControlChunk,
    ensure_response,
    unpack_request_parts,
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
from meshagent.api.room_server_client import RoomClient
from meshagent.api.room_server_client import RoomException
from meshagent.api.chan import ChanClosed

from aiohttp import web

from typing import Optional, Callable, Any
from collections.abc import AsyncIterable
import asyncio
from warnings import deprecated
import signal

logger = logging.getLogger("hosting")


class RemoteTool(Tool):
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
    def __init__(
        self,
        *,
        name: str,
        tools: list[BaseTool] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
        public: bool = True,
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
        self._request_streams = dict[str, asyncio.Queue[Optional[Chunk]]]()
        self._pending_request_chunks = dict[str, list[Chunk]]()

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
        self, *, queue: asyncio.Queue[Optional[Chunk]], chunk: Chunk
    ) -> None:
        if isinstance(chunk, _ControlChunk):
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
            chunk = unpack_request_parts(header=chunk_header, payload=payload)
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
            args = message["arguments"]
            caller_id = message["caller_id"]
            caller_context = message.get("caller_context", None)
            on_behalf_of_id = message.get("on_behalf_of_id", None)
            tool_call_id = message.get("tool_call_id", None)
            if not isinstance(tool_call_id, str) or tool_call_id == "":
                tool_call_id = str(message_id)
            request_stream = False
            try:
                args_as_chunk = unpack_request_parts(header=args, payload=attachment)
            except Exception:
                args_as_chunk = None

            if isinstance(args_as_chunk, _ControlChunk):
                if args_as_chunk.method != "open":
                    raise RoomException(
                        "request stream must start with an open control chunk"
                    )
                request_stream = True
                args = {}
                attachment = None
            elif isinstance(args_as_chunk, JsonChunk):
                if not isinstance(args_as_chunk.json, dict):
                    raise RoomException(
                        "non-stream tool input json chunk must contain an object"
                    )
                args = args_as_chunk.json
                attachment = None
            elif isinstance(args_as_chunk, EmptyChunk):
                args = {}
                attachment = None

            async def send_tool_call_response_chunk(chunk: Any) -> None:
                payload: bytes | None = None
                chunk_payload: Any = chunk
                if isinstance(chunk, Chunk):
                    chunk_payload = chunk.to_json()
                    payload = chunk.get_data()

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

            async def send_tool_call_response(response: Chunk) -> None:
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
                response: Optional[Chunk] = None

                tool = self.get_tool(name)
                if request_stream:
                    if not isinstance(tool, StreamTool):
                        raise RoomException(
                            f"tool '{name}' does not accept streamed input"
                        )
                else:
                    if not isinstance(tool, Tool):
                        raise RoomException(f"tool '{name}' requires streamed input")

                if request_stream:
                    request_stream_queue = asyncio.Queue[Optional[Chunk]]()
                    self._request_streams[tool_call_id] = request_stream_queue
                    self._enqueue_request_stream_chunk(
                        queue=request_stream_queue,
                        chunk=_ControlChunk(method="open"),
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

                    execution_result = await self.execute(
                        context=context,
                        name=name,
                        arguments=args,
                        request_stream=attachment_stream(),
                    )
                elif attachment is None:
                    execution_result = await self.execute(
                        context=context, name=name, arguments=args
                    )
                else:
                    execution_result = await self.execute(
                        context=context,
                        name=name,
                        arguments=args,
                        attachment=attachment,
                    )

                if isinstance(execution_result, AsyncIterable):
                    await send_tool_call_response(_ControlChunk(method="open"))
                    try:
                        async for item in execution_result:
                            chunk_queue.put_nowait(ensure_response(item))
                    except Exception:
                        raise
                    else:
                        chunk_queue.put_nowait(_ControlChunk(method="close"))
                        stream_close_emitted = True
                    response = None
                else:
                    response = ensure_response(execution_result)

            except Exception as e:
                logger.error("Tool call failed", exc_info=e)
                if response_sent:
                    chunk_queue.put_nowait(ErrorChunk(text=f"{e}"))
                    if not stream_close_emitted:
                        chunk_queue.put_nowait(_ControlChunk(method="close"))
                else:
                    response = ErrorChunk(text=f"{e}")
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
                logger.error("Tool call failed", exc_info=e)

        task.add_done_callback(on_done)

    async def _register(self, *, public: bool = False):
        children = {}

        for tool in self.tools:
            if tool.name in children:
                raise RoomException(f"duplicate tool name {tool.name}")
            if not isinstance(tool, (Tool, StreamTool)):
                raise RoomException(
                    f"tool '{tool.name}' must extend Tool or StreamTool"
                )

            children[self._mangle(tool.name)] = {
                "title": tool.title,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "thumbnail_url": tool.thumbnail_url,
                "defs": tool.defs,
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
