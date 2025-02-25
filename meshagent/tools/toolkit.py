from meshagent.api.protocol import Protocol
from meshagent.api.room_server_client import RoomClient
from meshagent.api.participant import Participant
from meshagent.api.runtime import DocumentRuntime
from meshagent.api.participant import Participant
from meshagent.api.room_server_client import RoomException
from jsonschema import validate
from jsonschema import Draft7Validator, RefResolutionError, RefResolver

from abc import ABC

from meshagent.api.messaging import pack_message, split_message_header, split_message_payload

import json
from abc import abstractmethod

from typing import Optional

from meshagent.api.messaging import Response, FileResponse, JsonResponse, TextResponse, ErrorResponse, LinkResponse, EmptyResponse

import logging

logger = logging.getLogger("tools")
logger.setLevel(logging.INFO)


class ToolContext:
    def __init__(self, *, room: RoomClient, caller: Participant, on_behalf_of: Optional[Participant] = None):
        self._room = room
        self._caller = caller
        self._on_behalf_of = on_behalf_of
    
    @property
    def caller(self):
        return self._caller    
    
    @property
    def on_behalf_of(self):
        return self._on_behalf_of    

    @property
    def room(self):
        return self._room    


def _check_refs(schema, resolver=None, seen=None):
    if seen is None:
        seen = set()
    if resolver is None:
        resolver = RefResolver.from_schema(schema)
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "$ref":
                # If we've already seen this reference, skip to avoid infinite recursion.
                if value in seen:
                    continue
                seen.add(value)
                try:
                    # Attempt to resolve the reference.
                    resolver.resolve(value)
                except RefResolutionError as e:
                    raise ValueError(f"Unresolved reference: {value}") from e
            else:
                _check_refs(value, resolver, seen)
    elif isinstance(schema, list):
        for item in schema:
            _check_refs(item, resolver, seen)

class Tool(ABC):
    def __init__(
        self,
        *,
        name: str,
        input_schema: dict,
        title: Optional[str] = None,
        description: Optional[str] = None,
        rules: Optional[list[str]] = None,
        thumbnail_url: Optional[str] = None,
        defs: Optional[dict[str,dict]] = None,
    ):

        if isinstance(input_schema, dict) == False:
            raise Exception("schema must be a dict, got: {type}".format(type=type(input_schema)))
        
        self.name = name
        if title == None:
            title = name
        self.title = title
        if description == None:
            description = ""
        self.description = description
        self.input_schema = input_schema
        self.rules = rules
        self.thumbnail_url = thumbnail_url
        self.defs = defs

        openai_schema = {
            **input_schema
        }

        if defs != None:
            openai_schema["$defs"] = {
                **defs
            }

        
        try:
            Draft7Validator.check_schema(openai_schema)
            _check_refs(openai_schema)

        except Exception as e:
            logger.error(f"Invalid tool schema {self.name}, {e}")
            raise RoomException(f"Invalid tool schema {self.name}: {e}")

        
    
    async def execute(self, context: ToolContext, **kwargs):
        raise(Exception("Not implemented"))


class Toolkit:
    def __init__(self, *, name: str, tools: list[Tool], rules:list[str]=list[str](), title: Optional[str] = None, description: Optional[str] = None, thumbnail_url: Optional[str] = None):
        self.name = name
        if title == None:
            title = name
        self.title = title
        if description == None:
            description = ""
        self.description = description
        self.tools = tools
        self.rules = rules
        self.thumbnail_url = thumbnail_url

    def get_tool(self, name: str) -> Tool:
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise RoomException(f'a tool with the name "{name}" was not found in the toolkit')

    async def execute(self, *, context: ToolContext, name: str, arguments: dict):
        tool = self.get_tool(name)

        schema = {
            **tool.input_schema,
        }
        if tool.defs != None:
            schema["$defs"] = {
                **tool.defs
            }

        validate(arguments, schema)
        return await tool.execute(context=context, **arguments)

