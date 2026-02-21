import logging

from meshagent.tools import Tool, Toolkit, ToolContext
from meshagent.api.messaging import (
    ErrorResponse,
    ensure_response,
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
        tools: list[Tool] = None,
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
            tools = list[Tool]()

        self.tools = tools
        self._registration_id = None

        self._room = None
        self.public = public

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

            async def send_tool_call_event(event: Any) -> None:
                await self._room.protocol.send(
                    type="agent.tool_call_event",
                    message_id=message_id,
                    data=pack_message(
                        header={
                            "tool_call_id": tool_call_id,
                            "event": event,
                        }
                    ),
                )

            event_queue: asyncio.Queue[Optional[Any]] = asyncio.Queue()

            async def forward_events() -> None:
                while True:
                    event = await event_queue.get()
                    if event is None:
                        return
                    try:
                        await send_tool_call_event(event)
                    except Exception as e:
                        logger.error("unable to forward tool call event", exc_info=e)

            forward_events_task = asyncio.create_task(forward_events())
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
                    event_handler=lambda event: event_queue.put_nowait(event),
                )
                execution_result = None
                if attachment is None:
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
                    has_last_item = False
                    last_item: Any = None
                    async for item in execution_result:
                        if has_last_item:
                            event_queue.put_nowait(last_item)
                        last_item = item
                        has_last_item = True

                    if has_last_item:
                        response = ensure_response(last_item)
                    else:
                        response = ensure_response(None)
                else:
                    response = ensure_response(execution_result)

            except Exception as e:
                logger.error("Tool call failed", exc_info=e)
                response = ErrorResponse(text=f"{e}")
            finally:
                event_queue.put_nowait(None)
                await forward_events_task

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
