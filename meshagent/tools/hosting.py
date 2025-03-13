
import logging

from meshagent.tools.toolkit import Tool, Toolkit, ToolContext
from meshagent.api.messaging import ErrorResponse, ensure_response
from meshagent.api import websocket_protocol, RemoteParticipant, WebhookServer, WebSocketClientProtocol, RoomStartedEvent, RoomEndedEvent, CallEvent, RoomMessage
from meshagent.api.protocol import Protocol
from meshagent.api.room_server_client import RoomClient
from meshagent.api.room_server_client import RoomException

from aiohttp import web

import json
from typing import Optional, Callable
import asyncio
import logging

import signal

logger = logging.getLogger("hosting")
logger.setLevel(logging.INFO)


class RemoteTool(Tool):
    def __init__(self, *, name, input_schema, title = None, description = None, rules = None, thumbnail_url = None, defs = None):
        super().__init__(name=name, input_schema=input_schema, title=title, description=description, rules=rules, thumbnail_url=thumbnail_url, defs=defs)
        self._room = None

    async def start(self, *, room: RoomClient):
        if self._room != None:
            raise RoomException("room is already started")
          
        self._room = room      

    async def stop(self): 
        pass


class RemoteToolkit(Toolkit):
    def __init__(self, *, name: str, tools: list[Tool] = None, title: Optional[str] = None, description: Optional[str] = None, thumbnail_url: Optional[str] = None):
        super().__init__(
            name=name,
            description=description,
            title=title,
            tools=tools,
            thumbnail_url=thumbnail_url,
        )

        if tools == None:
            tools = list[Tool]()

        self.tools = tools
        self._registration_id = None

        self._room = None

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
                logger.error(f"Unable to start remote tool in toolkit {self.name}", exc_info=r)

        if self._room != None:
            raise RoomException("room is already started")
          
        self._room = room

        self._room.protocol.register_handler(f"agent.tool_call.{self.name}", self._tool_call)


        await self._register(public=True)
            

    async def stop(self): 

        stops = []
        for tool in self.tools:

            if isinstance(tool, RemoteTool):

                stops.append(tool.stop())

        results = await asyncio.gather(*stops, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                logger.error(f"Unable to stop remote tool in toolkit {self.name}", exc_info=r)


        await self._unregister()
        self._room.protocol.unregister_handler(f"agent.tool_call.{self.name}", self._tool_call)
            
    async def _tool_call(self, protocol:Protocol, message_id:int, msg_type:str, data:bytes):        
        
        async def do_call():
            # Decode and parse the message
            message = json.loads(data.decode('utf-8'))
            name = message["name"]
            args = message["arguments"]
            caller_id = message["caller_id"]
            on_behalf_of_id = message.get("on_behalf_of_id", None)
            try:
                caller = None
                on_behalf_of = None

                for p in self.room.messaging.remote_participants:
                    if p.id == caller_id:
                        caller = p
                        break

                if on_behalf_of_id != None:
                    for p in self.room.messaging.remote_participants:
                        if p.id == on_behalf_of_id:
                            on_behalf_of = p
                            break



                # TODO: should we pass more info?
                if caller == None:
                    caller = RemoteParticipant(
                        id = caller_id,
                    )

                if on_behalf_of == None and on_behalf_of_id != None:
                    on_behalf_of = RemoteParticipant(
                        id = on_behalf_of_id,
                    )

                context = ToolContext(room=self._room, caller=caller, on_behalf_of=on_behalf_of)
                response = await self.execute(context=context, name=name, arguments=args)
                response = ensure_response(response)
            
            except Exception as e:
                logger.error("Tool call failed", exc_info=e)
                response = ErrorResponse(text=f"{e}")
                
            await self._room.protocol.send(type="agent.tool_call_response", data=response.pack(), message_id=message_id)
        
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
            
            children[tool.name] = {    
                "title" : tool.title,
                "description" : tool.description,
                "input_schema" : tool.input_schema,
                "thumbnail_url" : tool.thumbnail_url,
                "defs" : tool.defs,
            }

        result = await self._room.send_request("agent.register_toolkit", {
            "name": self.name,
            "description": self.description,
            "title" : self.title,
            "tools" : children,
            "public" : public,
            "thumbnail_url" : self.thumbnail_url
        })
        self._registration_id = result["id"]
        

    async def _unregister(self):
      await self._room.send_request("agent.unregister_toolkit", {
        "id": self._registration_id
      })
      self._registration_id = None


async def connect_remote_toolkit(*, room_name: str, toolkit: Toolkit):

    async with RoomClient(
        protocol = websocket_protocol(participant_name=toolkit.name, room_name=room_name, role="tool")
    ) as room:            
    
        remote = RemoteToolkit(
            name=toolkit.name,
            description=toolkit.description,
            title=toolkit.title,
            tools=[
                *toolkit.tools
            ]
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


class RemoteToolkitServer[T:Toolkit](WebhookServer):
    def __init__(self, *, cls: Optional[T] = None, path: Optional[str] = None, app: Optional[web.Application] = None,  host = None, port = None, webhook_secret = None, create_toolkit: Optional[Callable[[dict], RemoteToolkit]] = None, validate_webhook_secret: Optional[bool] = None):
        super().__init__(path=path, app=app, host=host, port=port, webhook_secret=webhook_secret, validate_webhook_secret=validate_webhook_secret)
        
        if create_toolkit == None:
            def default_create_toolkit(arguments: dict) -> RemoteToolkit:
                t = cls(**arguments)
                return t
            
            create_toolkit = default_create_toolkit

        self._create_toolkit = create_toolkit
        
            
    async def _spawn(self, *, room_name: str, room_url: str, token: str, arguments: Optional[dict] = None):
         
        t = self._create_toolkit(arguments=arguments)
        
        toolkit = RemoteToolkit(name=t.name, tools=t.tools, title=t.title, description=t.description)

        async def run():
            async with RoomClient(protocol=WebSocketClientProtocol(url=room_url, token=token)) as room:
    
                dismissed = asyncio.Future()

                def on_message(message: RoomMessage):
                    if message.type == "dismiss":
                        logger.info(f"dismissed by {message.from_participant_id}")
                        dismissed.set_result(True)

                room.messaging.on("message", on_message)
        
                await toolkit.start(room=room)
                
                done, pending = await asyncio.wait([
                    dismissed,
                    asyncio.ensure_future(room.protocol.wait_for_close())
                ], return_when=asyncio.FIRST_COMPLETED)
                
                await toolkit.stop()

        def on_done(task: asyncio.Task):
            try:
                result = task.result()
            except Exception as e:
                logger.error("agent encountered an error", exc_info=e)
                
        task = asyncio.create_task(run())
        task.add_done_callback(on_done)

    async def on_call(self, event: CallEvent):

        await self._spawn(room_name=event.room_name, room_url=event.room_url, token=event.token, arguments=event.arguments)