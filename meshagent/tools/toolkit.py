import urllib.parse
from meshagent.api.room_server_client import RoomClient
from meshagent.api.participant import Participant
from meshagent.api.room_server_client import RoomException
from meshagent.api.messaging import ensure_response, Request, unpack_request_parts
from meshagent.api import RequiredToolkit
from jsonschema import validate
from jsonschema import Draft7Validator, RefResolutionError, RefResolver
import logging
from abc import ABC

import json

from typing import Optional, Callable, Dict, Awaitable, Any

from meshagent.api.messaging import Response

import urllib

from opentelemetry import trace

tracer = trace.get_tracer("meshagent.tools")

logger = logging.getLogger("tools")


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


def validate_openai_schema(schema: dict):
    Draft7Validator.check_schema(schema)
    _check_refs(schema)


class ToolContext:
    def __init__(
        self,
        *,
        room: RoomClient,
        caller: Participant,
        on_behalf_of: Optional[Participant] = None,
        caller_context: Optional[Dict[str, Any]] = None,
    ):
        self._room = room
        self._caller = caller
        self._on_behalf_of = on_behalf_of
        self._caller_context = caller_context

    @property
    def caller(self) -> Participant:
        return self._caller

    @property
    def on_behalf_of(self) -> Optional[Participant] | None:
        return self._on_behalf_of

    @property
    def room(self) -> RoomClient:
        return self._room

    @property
    def caller_context(self) -> Dict[str, Any]:
        return self._caller_context


class BaseTool(ABC):
    def __init__(
        self,
        *,
        name: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        rules: Optional[list[str]] = None,
        thumbnail_url: Optional[str] = None,
        supports_context: Optional[bool] = None,
    ):
        if supports_context is None:
            supports_context = False

        self.name = name

        if title is None:
            title = name
        self.title = title

        if description is None:
            description = ""

        self.description = description
        self.rules = rules
        self.thumbnail_url = thumbnail_url

        self.supports_context = supports_context


class Tool(BaseTool):
    def __init__(
        self,
        *,
        name: str,
        input_schema: dict,
        title: Optional[str] = None,
        description: Optional[str] = None,
        rules: Optional[list[str]] = None,
        thumbnail_url: Optional[str] = None,
        defs: Optional[dict[str, dict]] = None,
        supports_context: Optional[bool] = None,
    ):
        super().__init__(
            name=name,
            title=title,
            description=description,
            rules=rules,
            thumbnail_url=thumbnail_url,
            supports_context=supports_context,
        )

        if not isinstance(input_schema, dict):
            raise Exception(
                "schema must be a dict, got: {type}".format(type=type(input_schema))
            )

        self.input_schema = input_schema
        self.defs = defs

        openai_schema = {**input_schema}

        if defs is not None:
            openai_schema["$defs"] = {**defs}

        try:
            validate_openai_schema(openai_schema)

        except Exception as e:
            logger.error(f"Invalid tool schema {self.name}, {e}")
            raise RoomException(f"Invalid tool schema {self.name}: {e}")

    async def execute(self, context: ToolContext, **kwargs) -> Response:
        raise (Exception("Not implemented"))


class RequestTool(BaseTool):
    def __init__(
        self,
        *,
        name: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        rules: Optional[list[str]] = None,
        thumbnail_url: Optional[str] = None,
        supports_context: Optional[bool] = None,
    ):
        super().__init__(
            name=name,
            title=title,
            description=description,
            rules=rules,
            thumbnail_url=thumbnail_url,
            supports_context=supports_context,
        )

    async def execute(self, *, context: ToolContext, request: Request) -> Response:
        raise (Exception("Not implemented"))


class Toolkit:
    def __init__(
        self,
        *,
        name: str,
        tools: list[BaseTool],
        rules: list[str] = list[str](),
        title: Optional[str] = None,
        description: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
    ):
        self.name = name
        if title is None:
            title = name
        self.title = title
        if description is None:
            description = ""
        self.description = description
        self.tools = tools
        self.rules = rules
        self.thumbnail_url = thumbnail_url

    def get_tool(self, name: str) -> BaseTool:
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise RoomException(
            f'a tool with the name "{name}" was not found in the toolkit'
        )

    async def execute(
        self,
        *,
        context: ToolContext,
        name: str,
        arguments: dict,
        attachment: Optional[bytes] = None,
    ):
        with tracer.start_as_current_span("toolkit.execute") as span:
            span.set_attributes(
                {"toolkit": self.name, "tool": name, "arguments": json.dumps(arguments)}
            )

            tool = self.get_tool(name)

            if isinstance(tool, RequestTool):
                request = unpack_request_parts(header=arguments, payload=attachment)

                response = await tool.execute(context=context, request=request)
                response = ensure_response(response)

            else:
                schema = {
                    **tool.input_schema,
                }
                if tool.defs is not None:
                    schema["$defs"] = {**tool.defs}

                validate(arguments, schema)
                response = await tool.execute(context=context, **arguments)
                response = ensure_response(response)

            span.set_attribute("response_type", response.to_json()["type"])
            return response


# a factory creates a toolkit from a RequiredToolkit spec
_factories = dict[str, Callable[[ToolContext, RequiredToolkit], Awaitable[Toolkit]]]()


def register_toolkit_factory(
    name: str, factory: Callable[[ToolContext, RequiredToolkit], Awaitable[Toolkit]]
):
    if name in _factories:
        raise Exception(f"{name} is already registered as a toolkit factory")

    _factories[name] = factory


def toolkit_factory(name: str):
    result = urllib.parse.urlparse(name)

    return _factories.get(result.path, None)
